"""Command envelope parsing for the recorder's Zenoh queryable.

The bubbaloop daemon and direct callers (CLI, tests) speak two slightly
different envelope shapes; both are accepted so the recorder works against
old and new daemons without coordination:

  flat   (bubbaloop ≥ PR #80): {"command": "...", ...top-level params}
  nested (older / direct):     {"command": "...", "params": {...}}

`parse_envelope` returns a flat dict either way, with `command` always at
the top level. Errors come back as `(None, ParseError)` so handlers can
turn them into structured `E_*` replies without exception plumbing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ParseError:
    code: str
    message: str


def parse_envelope(raw: bytes) -> tuple[dict | None, ParseError | None]:
    """Decode a query payload into a flat command envelope.

    Returns `(envelope, None)` on success or `(None, ParseError)` on any
    problem so callers can dispatch error replies without try/except.
    """
    if not raw:
        return None, ParseError("E_EMPTY", "empty command payload")
    try:
        envelope = json.loads(raw)
    except Exception as exc:
        return None, ParseError("E_BAD_JSON", f"invalid JSON: {exc}")
    if not isinstance(envelope, dict):
        return None, ParseError("E_BAD_SHAPE", "envelope must be a JSON object")

    nested = envelope.get("params")
    if isinstance(nested, dict):
        cmd = envelope.get("command")
        envelope = {**nested, "command": cmd}
    return envelope, None
