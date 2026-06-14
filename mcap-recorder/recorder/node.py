"""RecorderNode — command-driven MCAP recorder.

The process starts clean (no recording). It declares a Zenoh `command`
queryable and serves three commands sent via the bubbaloop MCP plugin's
`node_command_send` tool (or directly via Zenoh):

  start_recording { topic_patterns, name?, exclude?,
                    chunk_duration_secs?, chunk_max_bytes?, decode_timestamps? }
      Begins a new session. `topic_patterns` is required; `name` defaults to a
      generated `rec_<timestamp>` (validated like a Rust recording name); the
      chunking knobs fall back to code-level defaults (see config.py). The
      recording always lands under `~/.bubbaloop/recordings/<name>/` so the
      daemon's storage layer can see it — not a per-session path.
      Errors `E_ALREADY_RECORDING` if a session is already active.

  stop_recording {}
      Ends the active session, finalises chunks, returns summary.
      No-op (returns idle) if no session is active.

  get_status {}
      Returns "idle" or "recording" with counters (messages, bytes,
      elapsed_secs, current_chunk).

Wire format is the FLAT envelope established by bubbaloop PR #80; nested
`{params: {...}}` is also accepted for older daemons. See `commands.py`.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Optional

import zenoh

from .commands import parse_envelope
from .config import (
    NodeConfig,
    StartParams,
    generate_recording_name,
    load_config,
    resolve_start_params,
)
from .session import RecordingSession, RingBufferSession
from .storage_layout import sweep_incomplete_temps, validate_recording_name

log = logging.getLogger(__name__)

# Stamped into every manifest's `recorder_version` for provenance.
RECORDER_VERSION = "0.1.0"


def _resolve_machine_id(ctx) -> str:
    """Match the bubbaloop daemon's machine-id resolution: prefer
    BUBBALOOP_MACHINE_ID env, fall back to ctx attribute, then hostname."""
    return (
        os.environ.get("BUBBALOOP_MACHINE_ID")
        or getattr(ctx, "machine_id", None)
        or socket.gethostname().replace(".", "_").replace("-", "_")
    )


class RecorderNode:
    """Command-driven MCAP recorder.

    Process is always running; recording sessions begin/end on
    `start_recording`/`stop_recording` commands sent to the node's
    `command` queryable.
    """

    name = "mcap-recorder"

    def __init__(self, ctx, config: dict):
        self._ctx = ctx
        self._config: NodeConfig = load_config(config)
        self._machine_id = _resolve_machine_id(ctx)
        # Active session state — guarded by _lock so commands and the
        # shutdown path don't race.
        self._lock = threading.Lock()
        # Either a RecordingSession (streaming) or a RingBufferSession.
        self._active: Optional[object] = None
        log.info(
            "mcap-recorder ready (command-driven), name=%s machine_id=%s",
            self._config.name,
            self._machine_id,
        )

    def run(self) -> None:
        # Crash resilience (§3.3.9): clear write-temps a previous crash left behind.
        try:
            swept = sweep_incomplete_temps()
            if swept:
                log.info("Swept %d incomplete write-temp file(s) from prior run", swept)
        except Exception as exc:
            log.warning("temp sweep failed: %s", exc)

        machine_id = self._machine_id
        instance = self._config.name
        command_key = f"bubbaloop/global/{machine_id}/{instance}/command"
        status_key = f"bubbaloop/global/{machine_id}/{instance}/status"
        log.info("Declaring command queryable: %s", command_key)
        log.info("Declaring status queryable: %s", status_key)

        queryable = self._ctx.session.declare_queryable(command_key, self._on_query)
        status_queryable = self._ctx.session.declare_queryable(status_key, self._on_status_query)
        log.info("mcap-recorder running, waiting for commands…")
        try:
            self._ctx.wait_shutdown()
        finally:
            log.info("Shutdown — finalising any active session")
            for q in (queryable, status_queryable):
                try:
                    q.undeclare()
                except Exception as exc:
                    log.warning("queryable.undeclare failed: %s", exc)
            with self._lock:
                if self._active is not None:
                    summary = self._active.stop()
                    log.info("Final session summary: %s", summary)
                    self._active = None

    # ------------------------------------------------------------------
    # Command dispatch
    # ------------------------------------------------------------------

    def _on_query(self, query: zenoh.Query) -> None:
        try:
            payload = query.payload
            raw = bytes(payload) if payload is not None else b""
        except Exception as exc:
            self._reply_error(query, "E_NO_PAYLOAD", str(exc))
            return

        envelope, err = parse_envelope(raw)
        if err is not None:
            self._reply_error(query, err.code, err.message)
            return
        assert envelope is not None  # parse_envelope contract: one or the other

        cmd = envelope.get("command")
        handlers = {
            "start_recording": self._handle_start,
            "stop_recording": self._handle_stop,
            "flush_recording": self._handle_flush,
            "get_status": self._handle_status,
        }
        handler = handlers.get(cmd)
        if handler is None:
            self._reply_error(
                query,
                "E_UNKNOWN_CMD",
                f"unknown command {cmd!r}; supported: {sorted(handlers.keys())}",
            )
            return
        try:
            handler(query, envelope)
        except Exception as exc:
            log.exception("handler for %s raised", cmd)
            self._reply_error(query, "E_HANDLER", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_start(self, query: zenoh.Query, envelope: dict) -> None:
        with self._lock:
            if self._active is not None:
                self._reply_error(
                    query,
                    "E_ALREADY_RECORDING",
                    f"session {self._active.session_id} already active — stop_recording first",
                )
                return
            try:
                params: StartParams = resolve_start_params(envelope)
            except ValueError as exc:
                self._reply_error(query, "E_INVALID_PARAMS", str(exc))
                return
            try:
                if params.mode == "ring_buffer":
                    session = RingBufferSession(
                        zenoh_session=self._ctx.session,
                        machine_id=self._machine_id,
                        topic_patterns=list(params.topic_patterns),
                        window_secs=params.window_secs,
                        ring_max_bytes=params.ring_max_bytes,
                        decode_timestamps=params.decode_timestamps,
                        exclude=list(params.exclude),
                        recorder_version=RECORDER_VERSION,
                    )
                else:
                    session = RecordingSession(
                        zenoh_session=self._ctx.session,
                        name=params.name,
                        machine_id=self._machine_id,
                        topic_patterns=list(params.topic_patterns),
                        exclude=list(params.exclude),
                        chunk_duration_secs=params.chunk_duration_secs,
                        chunk_max_bytes=params.chunk_max_bytes,
                        decode_timestamps=params.decode_timestamps,
                        recorder_version=RECORDER_VERSION,
                    )
                session.start()
            except Exception as exc:
                self._reply_error(query, "E_START_FAILED", f"{type(exc).__name__}: {exc}")
                return
            self._active = session

        if params.mode == "ring_buffer":
            self._reply_ok(
                query,
                {
                    "status": "started",
                    "mode": "ring_buffer",
                    "window_secs": params.window_secs,
                    "topic_patterns": list(params.topic_patterns),
                },
            )
        else:
            self._reply_ok(
                query,
                {
                    "status": "started",
                    "mode": "streaming",
                    "name": session.name,
                    "recording_dir": str(session._dir),
                    "topic_patterns": list(params.topic_patterns),
                },
            )

    def _handle_stop(self, query: zenoh.Query, _envelope: dict) -> None:
        with self._lock:
            if self._active is None:
                self._reply_ok(query, {"status": "idle", "note": "no active session to stop"})
                return
            summary = self._active.stop()
            self._active = None
        self._reply_ok(query, {"status": "stopped", **summary})

    def _handle_flush(self, query: zenoh.Query, envelope: dict) -> None:
        # Resolve the target recording name (default to a generated one) outside
        # the lock; validate before touching the session.
        name = envelope.get("name")
        if name is None:
            name = generate_recording_name()
        elif not isinstance(name, str):
            self._reply_error(query, "E_INVALID_PARAMS", "name must be a string")
            return
        try:
            validate_recording_name(name)
        except ValueError as exc:
            self._reply_error(query, "E_INVALID_PARAMS", str(exc))
            return

        with self._lock:
            active = self._active
            if active is None or getattr(active, "mode", "streaming") != "ring_buffer":
                self._reply_error(
                    query,
                    "E_NOT_RING_BUFFER",
                    "flush_recording requires an active ring_buffer session",
                )
                return
        # seal() does disk I/O (sha256 + fsync) — run it WITHOUT holding the node
        # lock so a concurrent get_status / status-queryable / stop isn't blocked
        # for the seal duration. flush() is internally serialized + snapshots the
        # ring under its own lock.
        try:
            summary = active.flush(name)
        except ValueError as exc:
            self._reply_error(query, "E_EMPTY_BUFFER", str(exc))
            return
        self._reply_ok(query, {"status": "flushed", **summary})

    def _on_status_query(self, query: zenoh.Query) -> None:
        """Serve the `{instance}/status` queryable (§3.3.8) — lets the daemon /
        dashboard poll recorder state without sending a command."""
        with self._lock:
            if self._active is None:
                body = {"status": "idle"}
            else:
                body = {"status": "recording", **self._active.status()}
        self._reply_ok(query, body)

    def _handle_status(self, query: zenoh.Query, _envelope: dict) -> None:
        with self._lock:
            if self._active is None:
                self._reply_ok(query, {"status": "idle"})
                return
            self._reply_ok(query, {"status": "recording", **self._active.status()})

    # ------------------------------------------------------------------
    # Reply helpers
    # ------------------------------------------------------------------

    def _reply_ok(self, query: zenoh.Query, body: dict) -> None:
        try:
            payload = json.dumps(body, default=str).encode()
            query.reply(query.key_expr, payload)
        except Exception as exc:
            log.warning("reply_ok failed: %s", exc)

    def _reply_error(self, query: zenoh.Query, code: str, message: str) -> None:
        body = {"status": "error", "code": code, "message": message}
        try:
            payload = json.dumps(body).encode()
            query.reply(query.key_expr, payload)
        except Exception as exc:
            log.warning("reply_error failed: %s", exc)
