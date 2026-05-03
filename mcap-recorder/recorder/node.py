"""RecorderNode — command-driven MCAP recorder.

The process starts clean (no recording). It declares a Zenoh `command`
queryable and serves three commands sent via the bubbaloop MCP plugin's
`node_command_send` tool (or directly via Zenoh):

  start_recording { topic_patterns?, output_dir?, chunk_duration_secs?,
                    chunk_max_bytes?, decode_timestamps? }
      Begins a new session. Any field omitted falls back to config.yaml.
      Errors `E_ALREADY_RECORDING` if a session is already active.

  stop_recording {}
      Ends the active session, finalises chunks, returns summary.
      No-op (returns idle) if no session is active.

  get_status {}
      Returns "idle" or "recording" with counters (messages, bytes,
      elapsed_secs, current_chunk).

Wire format is the FLAT envelope established by bubbaloop PR #80:
`{"command": "...", ...top-level params}`. The bubbaloop daemon's
`node_command_send` tool produces exactly this shape from
`(node_name, command, params)`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
from pathlib import Path
from typing import Optional

import zenoh

from .session import RecordingSession

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


def _validate_defaults(cfg: dict) -> dict:
    """config.yaml is now DEFAULTS — topic_patterns + output_dir are
    optional here and only required at start_recording-time."""
    name = cfg.get("name", "mcap-recorder")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name must match {_NAME_RE.pattern} (got {name!r})")

    return {
        "name": name,
        "topic_patterns": cfg.get("topic_patterns") or [],
        "output_dir": cfg.get("output_dir"),
        "chunk_duration_secs": int(cfg.get("chunk_duration_secs", 300)),
        "chunk_max_bytes": int(cfg.get("chunk_max_bytes", 1_073_741_824)),
        "decode_timestamps": bool(cfg.get("decode_timestamps", False)),
    }


def _resolve_start_params(params: dict, defaults: dict) -> dict:
    """Merge start_recording params on top of defaults, validate, return
    the concrete RecordingSession arguments."""
    patterns = params.get("topic_patterns") or defaults.get("topic_patterns") or []
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("topic_patterns must be a non-empty list (set in command or config.yaml)")
    for p in patterns:
        if not isinstance(p, str) or "\x00" in p:
            raise ValueError(f"invalid topic pattern: {p!r}")

    output_dir = params.get("output_dir") or defaults.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output_dir is required (absolute path) — set in command or config.yaml")
    out_path = Path(output_dir)
    if not out_path.is_absolute():
        raise ValueError("output_dir must be an absolute path")
    if ".." in out_path.parts:
        raise ValueError("output_dir must not contain '..'")

    chunk_duration = int(params.get("chunk_duration_secs", defaults["chunk_duration_secs"]))
    if chunk_duration <= 0:
        raise ValueError("chunk_duration_secs must be > 0")
    chunk_max_bytes = int(params.get("chunk_max_bytes", defaults["chunk_max_bytes"]))
    if chunk_max_bytes <= 0:
        raise ValueError("chunk_max_bytes must be > 0")

    return {
        "topic_patterns": patterns,
        "output_dir": out_path,
        "chunk_duration_secs": chunk_duration,
        "chunk_max_bytes": chunk_max_bytes,
        "decode_timestamps": bool(params.get("decode_timestamps", defaults["decode_timestamps"])),
    }


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
        self._defaults = _validate_defaults(config)
        # Active session state — guarded by _lock so commands and the
        # shutdown path don't race.
        self._lock = threading.Lock()
        self._active: Optional[RecordingSession] = None
        log.info(
            "mcap-recorder ready (command-driven). Defaults: patterns=%s output=%s",
            self._defaults["topic_patterns"] or "<unset>",
            self._defaults["output_dir"] or "<unset>",
        )

    def run(self) -> None:
        machine_id = _resolve_machine_id(self._ctx)
        instance = self._defaults["name"]
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
        envelope = self._parse_envelope(query)
        if envelope is None:
            return  # already replied with an error
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

    def _parse_envelope(self, query: zenoh.Query) -> Optional[dict]:
        try:
            payload = query.payload
            raw = bytes(payload) if payload is not None else b""
        except Exception as exc:
            self._reply_error(query, "E_NO_PAYLOAD", str(exc))
            return None
        if not raw:
            self._reply_error(query, "E_EMPTY", "empty command payload")
            return None
        try:
            envelope = json.loads(raw)
        except Exception as exc:
            self._reply_error(query, "E_BAD_JSON", f"invalid JSON: {exc}")
            return None
        if not isinstance(envelope, dict):
            self._reply_error(query, "E_BAD_SHAPE", "envelope must be a JSON object")
            return None
        # Accept both wire formats:
        #   flat   (bubbaloop daemon ≥ PR #80): {"command": "...", ...top-level params}
        #   nested (older daemons / direct callers): {"command": "...", "params": {...}}
        # Robust to deployment drift — same recorder works against any daemon version.
        nested = envelope.get("params")
        if isinstance(nested, dict):
            cmd = envelope.get("command")
            envelope = {**nested, "command": cmd}
        return envelope

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
                params = _resolve_start_params(envelope, self._defaults)
            except ValueError as exc:
                self._reply_error(query, "E_INVALID_PARAMS", str(exc))
                return
            params["output_dir"].mkdir(parents=True, exist_ok=True)
            session = RecordingSession(
                zenoh_session=self._ctx.session,
                topic_patterns=params["topic_patterns"],
                output_dir=params["output_dir"],
                chunk_duration_secs=params["chunk_duration_secs"],
                chunk_max_bytes=params["chunk_max_bytes"],
                decode_timestamps=params["decode_timestamps"],
            )
            session.start()
            self._active = session
        self._reply_ok(
            query,
            {
                "status": "started",
                "session_id": session.session_id,
                "topic_patterns": params["topic_patterns"],
                "output_dir": str(params["output_dir"]),
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
