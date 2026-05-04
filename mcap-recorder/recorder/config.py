"""Config and per-session params for the mcap-recorder node.

Two install-time fields live in `config.yaml`:
  * `name`       — the Zenoh prefix the recorder declares its `command`
                   queryable under; must be known before any command.
  * `output_dir` — where MCAP chunks are written. The disk is per-machine
                   so it's an install-time decision, not a per-session one.

Per-session: `start_recording` carries `topic_patterns` (required, no sane
default) and may override the chunking knobs / `decode_timestamps`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")

DEFAULT_CHUNK_DURATION_SECS = 300
DEFAULT_CHUNK_MAX_BYTES = 1_073_741_824  # 1 GiB
DEFAULT_DECODE_TIMESTAMPS = False


@dataclass(frozen=True)
class NodeConfig:
    """Boot-time install config from `config.yaml`."""

    name: str
    output_dir: Path


@dataclass(frozen=True)
class StartParams:
    """One recording session's resolved + validated parameters."""

    topic_patterns: tuple[str, ...]
    chunk_duration_secs: int
    chunk_max_bytes: int
    decode_timestamps: bool


def load_config(cfg: Mapping[str, object]) -> NodeConfig:
    name = cfg.get("name", "mcap-recorder")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name must match {_NAME_RE.pattern} (got {name!r})")

    output_dir = cfg.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("config.output_dir is required (absolute path)")
    out_path = Path(output_dir)
    if not out_path.is_absolute():
        raise ValueError("config.output_dir must be an absolute path")
    if ".." in out_path.parts:
        raise ValueError("config.output_dir must not contain '..'")

    return NodeConfig(name=name, output_dir=out_path)


def resolve_start_params(params: Mapping[str, object]) -> StartParams:
    """Validate a `start_recording` request. Raises `ValueError` on any
    rule violation; node handlers translate that to an `E_INVALID_PARAMS`
    reply."""
    raw_patterns = params.get("topic_patterns")
    if (
        not isinstance(raw_patterns, Sequence)
        or isinstance(raw_patterns, str)
        or not raw_patterns
    ):
        raise ValueError("topic_patterns must be a non-empty list of strings")
    for p in raw_patterns:
        if not isinstance(p, str) or "\x00" in p:
            raise ValueError(f"invalid topic pattern: {p!r}")

    chunk_duration = int(params.get("chunk_duration_secs", DEFAULT_CHUNK_DURATION_SECS))
    if chunk_duration <= 0:
        raise ValueError("chunk_duration_secs must be > 0")
    chunk_max_bytes = int(params.get("chunk_max_bytes", DEFAULT_CHUNK_MAX_BYTES))
    if chunk_max_bytes <= 0:
        raise ValueError("chunk_max_bytes must be > 0")

    return StartParams(
        topic_patterns=tuple(raw_patterns),
        chunk_duration_secs=chunk_duration,
        chunk_max_bytes=chunk_max_bytes,
        decode_timestamps=bool(params.get("decode_timestamps", DEFAULT_DECODE_TIMESTAMPS)),
    )
