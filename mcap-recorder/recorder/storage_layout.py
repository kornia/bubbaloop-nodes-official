"""On-disk storage layout shared with the bubbaloop Rust storage layer.

The recorder writes recordings that the daemon's storage subsystem
(`crates/bubbaloop/src/storage`) reads back: `bubbaloop storage list/info/
upload/download/reconcile/replay`, the background sync driver, and the MCP
`storage_*` tools. For that to work, the on-disk format must match the Rust
contract **exactly**. This module is the single source of truth for the parts
that must agree byte-for-byte:

  * recordings root + per-recording directory layout,
  * recording-name validation (path-traversal guard),
  * the canonical chunk filename (`chunk-{index:06}-{sha8}.mcap`),
  * SHA-256 (lowercase hex, over the finalized chunk bytes).

Cross-checked against `storage/mod.rs` (`recordings_dir`, `recording_dir`,
`validate_recording_name`, `chunks_path`), `storage/recording.rs`
(`Chunk::canonical_name`), and `storage/integrity.rs` (`sha256`, `to_hex`).
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

# Recording names: 1–128 chars from [A-Za-z0-9._-], never "." or ".." — mirrors
# storage::validate_recording_name so a name can never escape the recordings dir.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_NAME_LEN = 128

# Hash hex length (SHA-256 → 32 bytes → 64 lowercase hex chars).
SHA256_HEX_LEN = 64
# Chars of the sha256 prefix embedded in the canonical chunk filename.
_CHUNK_NAME_SHA_PREFIX = 8


def bubbaloop_dir() -> Path:
    """`~/.bubbaloop` (honoring $HOME), matching `storage::bubbaloop_dir`.

    Falls back to the password-db home if $HOME is unset or not absolute, so the
    recordings root can never be a relative/traversal path (the old config used
    to guard `output_dir` for this; the location is now fixed)."""
    home = os.environ.get("HOME") or ""
    base = Path(home)
    if not home or not base.is_absolute():
        base = Path.home()
    return base / ".bubbaloop"


def recordings_dir() -> Path:
    """`~/.bubbaloop/recordings` — the recordings root the storage layer scans."""
    return bubbaloop_dir() / "recordings"


def validate_recording_name(name: str) -> None:
    """Raise ``ValueError`` unless `name` is a safe recording name.

    Identical rules to `storage::validate_recording_name`: 1–128 chars from
    ``[A-Za-z0-9._-]`` and never ``.`` or ``..`` (so it can't traverse out of
    the recordings directory or collide with the current/parent dir).
    """
    if not name or len(name) > _MAX_NAME_LEN:
        raise ValueError(
            f"recording name must be 1–{_MAX_NAME_LEN} chars (got {len(name)})"
        )
    if name in (".", ".."):
        raise ValueError("recording name must not be '.' or '..'")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"recording name must match {_NAME_RE.pattern} (got {name!r})"
        )


def recording_dir(name: str) -> Path:
    """`~/.bubbaloop/recordings/{name}` after validating `name`."""
    validate_recording_name(name)
    return recordings_dir() / name


def chunks_dir(name: str) -> Path:
    """`~/.bubbaloop/recordings/{name}/chunks` — where finalized MCAP files live."""
    return recording_dir(name) / "chunks"


def canonical_chunk_name(index: int, sha256_hex: str) -> str:
    """The canonical chunk filename `chunk-{index:06}-{sha8}.mcap`.

    Mirrors `Chunk::canonical_name`: a 6-digit zero-padded index and the first
    8 chars of the lowercase-hex SHA-256. `manifest::validate` rejects any chunk
    whose `name` doesn't match this exactly, so the recorder MUST use it.
    """
    if index < 0:
        raise ValueError(f"chunk index must be non-negative (got {index})")
    if len(sha256_hex) != SHA256_HEX_LEN:
        raise ValueError(
            f"sha256 must be {SHA256_HEX_LEN} hex chars (got {len(sha256_hex)})"
        )
    return f"chunk-{index:06d}-{sha256_hex[:_CHUNK_NAME_SHA_PREFIX]}.mcap"


def sha256_bytes(data: bytes) -> str:
    """Lowercase-hex SHA-256 of `data` (matches `integrity::to_hex`)."""
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Streaming lowercase-hex SHA-256 of the file at `path`."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk_size), b""):
            h.update(block)
    return h.hexdigest()


def object_key_chunk(machine_id: str, recording_name: str, index: int, sha256_hex: str) -> str:
    """Deterministic remote object key for a chunk (matches `object_key_chunk`)."""
    return (
        f"{machine_id}/{recording_name}/chunks/"
        f"{canonical_chunk_name(index, sha256_hex)}"
    )


def object_key_manifest(machine_id: str, recording_name: str) -> str:
    """Deterministic remote object key for the manifest (`object_key_manifest`)."""
    return f"{machine_id}/{recording_name}/manifest.json"


def sweep_incomplete_temps() -> int:
    """Remove orphaned write-temp files left by a crash (§3.3.9 crash
    resilience): half-written chunk temps (``chunks/.chunk-*.active``) and torn
    manifest temps (``manifest.json.tmp``) under the recordings root. The
    per-chunk manifest persistence means a recording's finalized chunks survive a
    crash; this just clears the never-finalized leftovers so they don't linger or
    confuse `storage list`. Returns the number of files removed.
    """
    root = recordings_dir()
    if not root.is_dir():
        return 0
    removed = 0
    for rec_dir in root.iterdir():
        if not rec_dir.is_dir():
            continue
        for tmp in rec_dir.glob("manifest.json.tmp"):
            tmp.unlink(missing_ok=True)
            removed += 1
        chunks = rec_dir / "chunks"
        if chunks.is_dir():
            for active in chunks.glob(".chunk-*.active"):
                active.unlink(missing_ok=True)
                removed += 1
    return removed
