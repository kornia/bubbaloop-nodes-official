"""Config and per-session params for the mcap-recorder node.

Install-time (`config.yaml`):
  * `name` — the Zenoh prefix the recorder declares its `command` queryable
    under; must be known before any command.

Recordings always land under the storage root (`~/.bubbaloop/recordings/<name>/`)
so the daemon's storage layer can see them — the location is no longer a config
knob. A legacy `output_dir` field is accepted but ignored.

Per-session: `start_recording` carries `topic_patterns` (required) and may set
`name` (the recording name; generated if omitted), `exclude`, the chunking
knobs, and `decode_timestamps`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence, Tuple

from .storage_layout import validate_recording_name

_INSTANCE_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")

DEFAULT_CHUNK_DURATION_SECS = 300
DEFAULT_CHUNK_MAX_BYTES = 1_073_741_824  # 1 GiB
DEFAULT_DECODE_TIMESTAMPS = False


@dataclass(frozen=True)
class NodeConfig:
    """Boot-time install config from `config.yaml`."""

    name: str


@dataclass(frozen=True)
class StartParams:
    """One recording session's resolved + validated parameters."""

    name: str
    topic_patterns: Tuple[str, ...]
    exclude: Tuple[str, ...]
    chunk_duration_secs: int
    chunk_max_bytes: int
    decode_timestamps: bool


def load_config(cfg: Mapping[str, object]) -> NodeConfig:
    name = cfg.get("name", "mcap-recorder")
    if not isinstance(name, str) or not _INSTANCE_NAME_RE.match(name):
        raise ValueError(f"config.name must match {_INSTANCE_NAME_RE.pattern} (got {name!r})")
    return NodeConfig(name=name)


def _generate_recording_name() -> str:
    """A default recording name like `rec_20260615_031400` (valid + sortable)."""
    return "rec_" + datetime.now().strftime("%Y%m%d_%H%M%S")


def resolve_start_params(params: Mapping[str, object]) -> StartParams:
    """Validate a `start_recording` request. Raises `ValueError` on any rule
    violation; node handlers translate that to an `E_INVALID_PARAMS` reply."""
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

    raw_exclude = params.get("exclude", [])
    if isinstance(raw_exclude, str) or not isinstance(raw_exclude, Sequence):
        raise ValueError("exclude must be a list of strings")
    for p in raw_exclude:
        if not isinstance(p, str) or "\x00" in p:
            raise ValueError(f"invalid exclude pattern: {p!r}")

    name = params.get("name")
    if name is None:
        name = _generate_recording_name()
    elif not isinstance(name, str):
        raise ValueError("name must be a string")
    # Same guard as the Rust storage layer — a bad name can't escape the
    # recordings directory.
    validate_recording_name(name)

    chunk_duration = int(params.get("chunk_duration_secs", DEFAULT_CHUNK_DURATION_SECS))
    if chunk_duration <= 0:
        raise ValueError("chunk_duration_secs must be > 0")
    chunk_max_bytes = int(params.get("chunk_max_bytes", DEFAULT_CHUNK_MAX_BYTES))
    if chunk_max_bytes <= 0:
        raise ValueError("chunk_max_bytes must be > 0")

    return StartParams(
        name=name,
        topic_patterns=tuple(raw_patterns),
        exclude=tuple(raw_exclude),
        chunk_duration_secs=chunk_duration,
        chunk_max_bytes=chunk_max_bytes,
        decode_timestamps=bool(params.get("decode_timestamps", DEFAULT_DECODE_TIMESTAMPS)),
    )
