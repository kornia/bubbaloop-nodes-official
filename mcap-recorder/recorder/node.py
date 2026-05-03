"""RecorderNode — bubbaloop-sdk Node that runs a single RecordingSession."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .session import RecordingSession

log = logging.getLogger(__name__)

# Same identifier rule the rest of the bubbaloop nodes use (CLAUDE.md).
_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


def _validate_config(cfg: dict) -> dict:
    name = cfg.get("name", "recorder-py")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name must match {_NAME_RE.pattern} (got {name!r})")

    patterns = cfg.get("topic_patterns") or []
    if not isinstance(patterns, list) or not patterns:
        raise ValueError("config.topic_patterns must be a non-empty list of Zenoh key expressions")
    for p in patterns:
        if not isinstance(p, str) or "\x00" in p:
            raise ValueError(f"Invalid topic pattern: {p!r}")

    output_dir = cfg.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("config.output_dir is required (absolute path)")
    out_path = Path(output_dir)
    if not out_path.is_absolute():
        raise ValueError("config.output_dir must be an absolute path")
    if ".." in out_path.parts:
        raise ValueError("config.output_dir must not contain '..'")

    chunk_duration = int(cfg.get("chunk_duration_secs", 300))
    if chunk_duration <= 0:
        raise ValueError("chunk_duration_secs must be > 0")
    chunk_max_bytes = int(cfg.get("chunk_max_bytes", 1_073_741_824))
    if chunk_max_bytes <= 0:
        raise ValueError("chunk_max_bytes must be > 0")

    return {
        "name": name,
        "topic_patterns": patterns,
        "output_dir": out_path,
        "chunk_duration_secs": chunk_duration,
        "chunk_max_bytes": chunk_max_bytes,
        "decode_timestamps": bool(cfg.get("decode_timestamps", False)),
    }


class RecorderNode:
    """Subscribes to Zenoh patterns and writes MCAP files to disk."""

    name = "recorder-py"

    def __init__(self, ctx, config: dict):
        self._ctx = ctx
        self._cfg = _validate_config(config)
        self._cfg["output_dir"].mkdir(parents=True, exist_ok=True)
        log.info(
            "Recorder configured: patterns=%s output=%s chunk=%ds/%dB decode_ts=%s",
            self._cfg["topic_patterns"],
            self._cfg["output_dir"],
            self._cfg["chunk_duration_secs"],
            self._cfg["chunk_max_bytes"],
            self._cfg["decode_timestamps"],
        )

    def run(self) -> None:
        session = RecordingSession(
            zenoh_session=self._ctx.session,
            topic_patterns=self._cfg["topic_patterns"],
            output_dir=self._cfg["output_dir"],
            chunk_duration_secs=self._cfg["chunk_duration_secs"],
            chunk_max_bytes=self._cfg["chunk_max_bytes"],
            decode_timestamps=self._cfg["decode_timestamps"],
        )
        session.start()
        log.info("Recording started — session_id=%s", session.session_id)
        try:
            self._ctx.wait_shutdown()
        finally:
            session.stop()
