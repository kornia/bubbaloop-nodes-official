"""RecorderNode — command-driven MCAP recorder.

The process starts clean (no recording). It declares a Zenoh `command`
queryable and serves three commands sent via the bubbaloop MCP plugin's
`node_command_send` tool (or directly via Zenoh):

  start_recording { topic_patterns,
                    chunk_duration_secs?, chunk_max_bytes?, decode_timestamps? }
      Begins a new session. `topic_patterns` is required; chunking knobs
      fall back to code-level defaults (see config.py). `output_dir` is
      install-time, lives in config.yaml — not overridable per session.
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
from .config import NodeConfig, StartParams, load_config, resolve_start_params
from .session import RecordingSession

log = logging.getLogger(__name__)


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
        # Active session state — guarded by _lock so commands and the
        # shutdown path don't race.
        self._lock = threading.Lock()
        self._active: Optional[RecordingSession] = None
        log.info(
            "mcap-recorder ready (command-driven), name=%s output_dir=%s",
            self._config.name,
            self._config.output_dir,
        )

    def run(self) -> None:
        machine_id = _resolve_machine_id(self._ctx)
        instance = self._config.name
        command_key = f"bubbaloop/global/{machine_id}/{instance}/command"
        log.info("Declaring command queryable: %s", command_key)

        queryable = self._ctx.session.declare_queryable(command_key, self._on_query)
        log.info("mcap-recorder running, waiting for commands…")
        try:
            self._ctx.wait_shutdown()
        finally:
            log.info("Shutdown — finalising any active session")
            try:
                queryable.undeclare()
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
            output_dir = self._config.output_dir
            output_dir.mkdir(parents=True, exist_ok=True)
            session = RecordingSession(
                zenoh_session=self._ctx.session,
                topic_patterns=list(params.topic_patterns),
                output_dir=output_dir,
                chunk_duration_secs=params.chunk_duration_secs,
                chunk_max_bytes=params.chunk_max_bytes,
                decode_timestamps=params.decode_timestamps,
            )
            session.start()
            self._active = session
        self._reply_ok(
            query,
            {
                "status": "started",
                "session_id": session.session_id,
                "topic_patterns": list(params.topic_patterns),
                "output_dir": str(output_dir),
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
