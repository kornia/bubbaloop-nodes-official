"""Config and per-session params for the mcap-recorder node.

Only `name` lives in `config.yaml` — it's the Zenoh prefix the recorder
declares its `command` queryable under, and that has to be known at boot
before any command can be received.

Everything else is per-session and comes from the `start_recording`
command. `topic_patterns` and `output_dir` are required (no sane default
possible). Chunking knobs and `decode_timestamps` have code-level defaults
the command can override.
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
    """Boot-time identity loaded from `config.yaml`."""

    name: str


@dataclass(frozen=True)
class StartParams:
    """One recording session's resolved + validated parameters."""

    topic_patterns: tuple[str, ...]
    output_dir: Path
    chunk_duration_secs: int
    chunk_max_bytes: int
    decode_timestamps: bool


def load_config(cfg: Mapping[str, object]) -> NodeConfig:
    name = cfg.get("name", "mcap-recorder")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name must match {_NAME_RE.pattern} (got {name!r})")
    return NodeConfig(name=name)


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

    output_dir = params.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output_dir is required (absolute path)")
    out_path = Path(output_dir)
    if not out_path.is_absolute():
        raise ValueError("output_dir must be an absolute path")
    if ".." in out_path.parts:
        raise ValueError("output_dir must not contain '..'")

    chunk_duration = int(params.get("chunk_duration_secs", DEFAULT_CHUNK_DURATION_SECS))
    if chunk_duration <= 0:
        raise ValueError("chunk_duration_secs must be > 0")
    chunk_max_bytes = int(params.get("chunk_max_bytes", DEFAULT_CHUNK_MAX_BYTES))
    if chunk_max_bytes <= 0:
        raise ValueError("chunk_max_bytes must be > 0")

    return StartParams(
        topic_patterns=tuple(raw_patterns),
        output_dir=out_path,
        chunk_duration_secs=chunk_duration,
        chunk_max_bytes=chunk_max_bytes,
        decode_timestamps=bool(params.get("decode_timestamps", DEFAULT_DECODE_TIMESTAMPS)),
    )
