"""Config validation for the mcap-recorder node.

Two layers:

* `Defaults` — `name` plus the fallback values the recorder uses when a
  `start_recording` command omits a field. Loaded from `config.yaml` once
  at process start.

* `StartParams` — the concrete parameters for one recording session,
  produced by merging `start_recording` request fields on top of `Defaults`
  and validating the result.

Both are frozen dataclasses so misuse (mutating after construction) is a
TypeError at runtime, not a silent state bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


@dataclass(frozen=True)
class Defaults:
    name: str
    topic_patterns: tuple[str, ...] = ()
    output_dir: str | None = None
    chunk_duration_secs: int = 300
    chunk_max_bytes: int = 1_073_741_824
    decode_timestamps: bool = False


@dataclass(frozen=True)
class StartParams:
    topic_patterns: tuple[str, ...]
    output_dir: Path
    chunk_duration_secs: int
    chunk_max_bytes: int
    decode_timestamps: bool


def load_defaults(cfg: Mapping[str, object]) -> Defaults:
    """Validate and freeze `config.yaml`. Topic patterns and output dir are
    optional here — they may be supplied at start_recording time instead."""
    name = cfg.get("name", "mcap-recorder")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name must match {_NAME_RE.pattern} (got {name!r})")

    raw_patterns = cfg.get("topic_patterns") or []
    if not isinstance(raw_patterns, Sequence) or isinstance(raw_patterns, str):
        raise ValueError("config.topic_patterns must be a list of strings")

    output_dir = cfg.get("output_dir")
    if output_dir is not None and not isinstance(output_dir, str):
        raise ValueError("config.output_dir must be a string")

    return Defaults(
        name=name,
        topic_patterns=tuple(raw_patterns),
        output_dir=output_dir,
        chunk_duration_secs=int(cfg.get("chunk_duration_secs", 300)),
        chunk_max_bytes=int(cfg.get("chunk_max_bytes", 1_073_741_824)),
        decode_timestamps=bool(cfg.get("decode_timestamps", False)),
    )


def resolve_start_params(params: Mapping[str, object], defaults: Defaults) -> StartParams:
    """Merge a `start_recording` request on top of defaults and validate.

    Raises `ValueError` with an `E_INVALID_PARAMS`-friendly message; node
    handlers translate that into the wire-format error reply.
    """
    raw_patterns = params.get("topic_patterns") or list(defaults.topic_patterns)
    if (
        not isinstance(raw_patterns, Sequence)
        or isinstance(raw_patterns, str)
        or not raw_patterns
    ):
        raise ValueError(
            "topic_patterns must be a non-empty list (set in command or config.yaml)"
        )
    for p in raw_patterns:
        if not isinstance(p, str) or "\x00" in p:
            raise ValueError(f"invalid topic pattern: {p!r}")

    output_dir = params.get("output_dir") or defaults.output_dir
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError(
            "output_dir is required (absolute path) — set in command or config.yaml"
        )
    out_path = Path(output_dir)
    if not out_path.is_absolute():
        raise ValueError("output_dir must be an absolute path")
    if ".." in out_path.parts:
        raise ValueError("output_dir must not contain '..'")

    chunk_duration = int(params.get("chunk_duration_secs", defaults.chunk_duration_secs))
    if chunk_duration <= 0:
        raise ValueError("chunk_duration_secs must be > 0")
    chunk_max_bytes = int(params.get("chunk_max_bytes", defaults.chunk_max_bytes))
    if chunk_max_bytes <= 0:
        raise ValueError("chunk_max_bytes must be > 0")

    return StartParams(
        topic_patterns=tuple(raw_patterns),
        output_dir=out_path,
        chunk_duration_secs=chunk_duration,
        chunk_max_bytes=chunk_max_bytes,
        decode_timestamps=bool(params.get("decode_timestamps", defaults.decode_timestamps)),
    )
