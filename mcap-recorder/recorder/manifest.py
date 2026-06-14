"""The `manifest.json` model — the storage layer's source of truth.

Every recording is a directory holding `manifest.json` plus `chunks/*.mcap`.
The manifest is what `storage list/info/upload/reconcile/replay`, the sync
driver, and the MCP `storage_*` tools read; a chunk is only ever uploaded
because its manifest entry has `uploaded_at_ns == null`. This module produces
JSON that deserializes cleanly into the Rust `Recording`/`Chunk`/`Channel`
serde types and passes `manifest::validate` (schema_version ≥ 1; contiguous
chunk indices from 0; canonical chunk names; 64-hex sha256).

Field names mirror `storage/recording.rs`. Optional fields are omitted when
unset — Rust fills them from serde defaults, and omitting keeps the file close
to what the Rust writer produces. Written atomically (tmp + fsync + rename),
matching `storage::atomic_write`.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .storage_layout import canonical_chunk_name

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_FILE = "manifest.json"


@dataclass
class Selection:
    """Resolved topic selection (`storage::recording::Selection`)."""

    topics: List[str] = field(default_factory=list)
    regex: Optional[str] = None
    exclude: List[str] = field(default_factory=list)
    include_local: bool = False

    def to_dict(self) -> dict:
        out: dict = {"topics": list(self.topics)}
        if self.regex is not None:
            out["regex"] = self.regex
        if self.exclude:
            out["exclude"] = list(self.exclude)
        if self.include_local:
            out["include_local"] = True
        return out


@dataclass
class Channel:
    """One recorded topic/channel (`storage::recording::Channel`)."""

    topic: str
    channel_id: int
    message_encoding: str
    zenoh_encoding: str
    message_count: int = 0
    schema_name: Optional[str] = None
    publish_time_first_ns: Optional[int] = None
    publish_time_last_ns: Optional[int] = None

    def to_dict(self) -> dict:
        out: dict = {
            "topic": self.topic,
            "channel_id": self.channel_id,
            "message_encoding": self.message_encoding,
            "zenoh_encoding": self.zenoh_encoding,
        }
        if self.message_count:
            out["message_count"] = self.message_count
        if self.schema_name is not None:
            out["schema_name"] = self.schema_name
        if self.publish_time_first_ns is not None:
            out["publish_time_first_ns"] = self.publish_time_first_ns
        if self.publish_time_last_ns is not None:
            out["publish_time_last_ns"] = self.publish_time_last_ns
        return out


@dataclass
class Chunk:
    """A finalized chunk (`storage::recording::Chunk`).

    `name` must be canonical for `index`+`sha256`; build via
    [`Chunk.finalized`] so it always is.
    """

    name: str
    index: int
    size_bytes: int
    sha256: str
    log_time_first_ns: Optional[int] = None
    log_time_last_ns: Optional[int] = None
    uploaded_at_ns: Optional[int] = None
    remote_etag: Optional[str] = None

    @classmethod
    def finalized(
        cls,
        index: int,
        size_bytes: int,
        sha256: str,
        log_time_first_ns: Optional[int],
        log_time_last_ns: Optional[int],
    ) -> "Chunk":
        return cls(
            name=canonical_chunk_name(index, sha256),
            index=index,
            size_bytes=size_bytes,
            sha256=sha256,
            log_time_first_ns=log_time_first_ns,
            log_time_last_ns=log_time_last_ns,
        )

    def to_dict(self) -> dict:
        out: dict = {
            "name": self.name,
            "index": self.index,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }
        if self.log_time_first_ns is not None:
            out["log_time_first_ns"] = self.log_time_first_ns
        if self.log_time_last_ns is not None:
            out["log_time_last_ns"] = self.log_time_last_ns
        if self.uploaded_at_ns is not None:
            out["uploaded_at_ns"] = self.uploaded_at_ns
        if self.remote_etag is not None:
            out["remote_etag"] = self.remote_etag
        return out


@dataclass
class Recording:
    """A recording manifest (`storage::recording::Recording`)."""

    name: str
    machine_id: str
    started_at_ns: int
    mode: str = "streaming"  # "streaming" | "ring_buffer"
    selection: Selection = field(default_factory=Selection)
    fleet_router: Optional[str] = None
    ended_at_ns: Optional[int] = None
    duration_ns: Optional[int] = None
    size_bytes: int = 0
    window_secs: Optional[int] = None
    trigger: Optional[str] = None  # "manual" | "on-event"
    channels: List[Channel] = field(default_factory=list)
    chunks: List[Chunk] = field(default_factory=list)
    profile_name: Optional[str] = None
    profile_sha256: Optional[str] = None
    recorder_version: str = ""
    tags: List[str] = field(default_factory=list)
    corrupt: bool = False

    def to_dict(self) -> dict:
        out: dict = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "name": self.name,
            "machine_id": self.machine_id,
            "started_at_ns": self.started_at_ns,
            "mode": self.mode,
            "selection": self.selection.to_dict(),
        }
        if self.fleet_router is not None:
            out["fleet_router"] = self.fleet_router
        if self.ended_at_ns is not None:
            out["ended_at_ns"] = self.ended_at_ns
        if self.duration_ns is not None:
            out["duration_ns"] = self.duration_ns
        if self.size_bytes:
            out["size_bytes"] = self.size_bytes
        if self.window_secs is not None:
            out["window_secs"] = self.window_secs
        if self.trigger is not None:
            out["trigger"] = self.trigger
        if self.channels:
            out["channels"] = [c.to_dict() for c in self.channels]
        if self.chunks:
            out["chunks"] = [c.to_dict() for c in self.chunks]
        if self.profile_name is not None:
            out["profile_name"] = self.profile_name
        if self.profile_sha256 is not None:
            out["profile_sha256"] = self.profile_sha256
        if self.recorder_version:
            out["recorder_version"] = self.recorder_version
        if self.tags:
            out["tags"] = list(self.tags)
        if self.corrupt:
            out["corrupt"] = True
        return out

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), indent=2).encode("utf-8")


def manifest_path(recording_dir: Path) -> Path:
    return recording_dir / MANIFEST_FILE


def save_atomic(recording_dir: Path, recording: Recording) -> None:
    """Write `manifest.json` atomically (tmp + fsync + rename), so a crash
    mid-write never leaves a torn manifest (`storage::atomic_write`)."""
    recording_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path(recording_dir)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = recording.to_json()
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
