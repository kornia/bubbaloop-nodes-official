"""Chunked MCAP writer producing storage-layer-compatible recordings.

Each finalized chunk is written to a hidden ``.active`` temp file, hashed
(SHA-256), and renamed to the canonical ``chunk-{index:06}-{sha8}.mcap`` the
Rust storage layer expects (`Chunk::canonical_name`). On every finalize the
writer hands a fully-populated `manifest.Chunk` back to its owner (the session),
which appends it to `manifest.json` — that's what makes the daemon's sync driver
pick the chunk up for upload.

Channels carry the §4.5 metadata the replay path reads back
(`zenoh.encoding` / `zenoh.topic` / `bubbaloop.schema_name`), and every message
carries dual timestamps (`log_time` = recorder receipt clock, `publish_time` =
the sample's source time), matching `ring_buffer.rs`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Dict, List, Optional, Tuple

from mcap.writer import CompressionType, Writer

from . import manifest
from .storage_layout import canonical_chunk_name, sha256_file

log = logging.getLogger(__name__)

# rosbag2 / storage defaults (mirror McapWriteConfig::default): 786432-byte
# internal MCAP chunk records, zstd, CRCs on.
DEFAULT_MCAP_CHUNK_SIZE_BYTES = 786_432

# §4.5 channel metadata keys — MUST match storage::ring_buffer constants so
# replay can recover the original Zenoh topic + encoding.
META_ZENOH_ENCODING = "zenoh.encoding"
META_ZENOH_TOPIC = "zenoh.topic"
META_SCHEMA_NAME = "bubbaloop.schema_name"


@dataclass(frozen=True)
class SampleEncoding:
    """Encoding tier extracted from a Zenoh sample's encoding string."""

    kind: str  # "cbor" | "json" | "protobuf" | "raw"
    schema_name: str = ""
    # The full original Zenoh encoding string (e.g. "application/cbor"), kept
    # verbatim for the §4.5 zenoh.encoding metadata + manifest channel record.
    zenoh_encoding: str = ""

    @classmethod
    def from_zenoh(cls, encoding: str) -> "SampleEncoding":
        if encoding.startswith("application/cbor"):
            return cls("cbor", zenoh_encoding=encoding)
        if encoding.startswith("application/json"):
            return cls("json", zenoh_encoding=encoding)
        if encoding.startswith("application/protobuf"):
            schema = encoding.split(";", 1)[1] if ";" in encoding else ""
            return cls("protobuf", schema_name=schema, zenoh_encoding=encoding)
        return cls("raw", zenoh_encoding=encoding)

    @property
    def message_encoding(self) -> str:
        # MCAP message_encoding string (manifest Channel.message_encoding).
        return {"cbor": "cbor", "json": "json", "protobuf": "protobuf", "raw": "raw"}[
            self.kind
        ]


class _ChannelStat:
    """Accumulated per-topic stats for the manifest `channels` array (persists
    across chunk-file rotations, which re-assign per-file MCAP channel ids)."""

    __slots__ = (
        "stable_id",
        "message_encoding",
        "zenoh_encoding",
        "schema_name",
        "message_count",
        "publish_first_ns",
        "publish_last_ns",
    )

    def __init__(self, stable_id: int, encoding: SampleEncoding):
        self.stable_id = stable_id
        self.message_encoding = encoding.message_encoding
        self.zenoh_encoding = encoding.zenoh_encoding
        self.schema_name = encoding.schema_name or None
        self.message_count = 0
        self.publish_first_ns: Optional[int] = None
        self.publish_last_ns: Optional[int] = None

    def observe(self, publish_time_ns: int) -> None:
        self.message_count += 1
        if self.publish_first_ns is None or publish_time_ns < self.publish_first_ns:
            self.publish_first_ns = publish_time_ns
        if self.publish_last_ns is None or publish_time_ns > self.publish_last_ns:
            self.publish_last_ns = publish_time_ns


class ChunkedMcapWriter:
    """Writes MCAP messages with size/time-based file rotation, emitting a
    canonical, hashed chunk + a `manifest.Chunk` on each finalize."""

    def __init__(
        self,
        chunks_dir: Path,
        chunk_duration_secs: int,
        chunk_max_bytes: int,
        on_chunk_finalized: Callable[[manifest.Chunk], None],
        mcap_chunk_size_bytes: int = DEFAULT_MCAP_CHUNK_SIZE_BYTES,
    ):
        self._chunks_dir = chunks_dir
        self._chunk_duration_secs = chunk_duration_secs
        self._chunk_max_bytes = chunk_max_bytes
        self._on_chunk = on_chunk_finalized
        self._mcap_chunk_size = mcap_chunk_size_bytes

        self._writer: Optional[Writer] = None
        self._stream: Optional[IO[bytes]] = None
        self._active_path: Optional[Path] = None
        self._index = 0

        # Per-file (cleared on rotate).
        self._channels: Dict[str, int] = {}
        self._schemas: Dict[str, int] = {}
        self._sequences: Dict[int, int] = {}

        # Per-chunk-file accumulators (reset on rotate).
        self._chunk_start = time.monotonic()
        self._chunk_bytes = 0
        self._chunk_msgs = 0
        self._chunk_log_first: Optional[int] = None
        self._chunk_log_last: Optional[int] = None

        # Persistent across files.
        self._channel_specs: Dict[str, Tuple[SampleEncoding, Optional[bytes]]] = {}
        self._stats: Dict[str, _ChannelStat] = {}
        self._next_stable_id = 0

        self._total_messages = 0
        self._total_bytes = 0
        self._chunks_finalized = 0

    def open_chunk(self) -> None:
        self._chunks_dir.mkdir(parents=True, exist_ok=True)
        self._open_chunk_file()

    def register_channel(
        self,
        topic: str,
        encoding: SampleEncoding,
        schema_bytes: Optional[bytes] = None,
    ) -> int:
        existing = self._channels.get(topic)
        if existing is not None:
            return existing
        if self._writer is None:
            raise RuntimeError("no open chunk")

        schema_id = 0
        if encoding.kind == "protobuf" and schema_bytes:
            schema_id = self._register_proto_schema(encoding.schema_name, schema_bytes)

        # §4.5 channel metadata — recover the original topic + encoding on replay.
        metadata: Dict[str, str] = {
            META_ZENOH_TOPIC: topic,
            META_ZENOH_ENCODING: encoding.zenoh_encoding,
        }
        if encoding.schema_name:
            metadata[META_SCHEMA_NAME] = encoding.schema_name

        channel_id = self._writer.register_channel(
            topic=topic,
            message_encoding=encoding.message_encoding,
            schema_id=schema_id,
            metadata=metadata,
        )
        self._channels[topic] = channel_id
        self._sequences[channel_id] = 0
        self._channel_specs[topic] = (encoding, schema_bytes)
        if topic not in self._stats:
            self._stats[topic] = _ChannelStat(self._next_stable_id, encoding)
            self._next_stable_id += 1
        return channel_id

    def write_message(
        self, topic: str, publish_time_ns: int, log_time_ns: int, data: bytes
    ) -> None:
        if self._should_rotate():
            self._rotate_chunk()
        channel_id = self._channels.get(topic)
        if channel_id is None:
            raise RuntimeError(f"channel not registered: {topic}")
        if self._writer is None:
            raise RuntimeError("no open chunk")

        self._sequences[channel_id] += 1
        self._writer.add_message(
            channel_id=channel_id,
            log_time=log_time_ns,
            publish_time=publish_time_ns,
            sequence=self._sequences[channel_id],
            data=data,
        )

        n = len(data)
        self._chunk_bytes += n
        self._chunk_msgs += 1
        self._total_messages += 1
        if self._chunk_log_first is None or log_time_ns < self._chunk_log_first:
            self._chunk_log_first = log_time_ns
        if self._chunk_log_last is None or log_time_ns > self._chunk_log_last:
            self._chunk_log_last = log_time_ns
        self._stats[topic].observe(publish_time_ns)

    def finish(self) -> None:
        """Finalize the current (last) chunk, emitting it if non-empty."""
        self._finalize_chunk()

    # ------------------------------------------------------------------
    # Manifest projection
    # ------------------------------------------------------------------

    def channels(self) -> List[manifest.Channel]:
        """Current per-topic channel records for the manifest, id-ordered."""
        out = [
            manifest.Channel(
                topic=topic,
                channel_id=stat.stable_id,
                message_encoding=stat.message_encoding,
                zenoh_encoding=stat.zenoh_encoding,
                message_count=stat.message_count,
                schema_name=stat.schema_name,
                publish_time_first_ns=stat.publish_first_ns,
                publish_time_last_ns=stat.publish_last_ns,
            )
            for topic, stat in self._stats.items()
        ]
        out.sort(key=lambda c: c.channel_id)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _register_proto_schema(self, name: str, data: bytes) -> int:
        existing = self._schemas.get(name)
        if existing is not None:
            return existing
        if self._writer is None:
            raise RuntimeError("no open chunk")
        sid = self._writer.register_schema(name=name, encoding="protobuf", data=data)
        self._schemas[name] = sid
        return sid

    def _should_rotate(self) -> bool:
        # Only rotate a chunk that actually has data, so we never emit empties.
        if self._chunk_msgs == 0:
            return False
        return (
            time.monotonic() - self._chunk_start >= self._chunk_duration_secs
            or self._chunk_bytes >= self._chunk_max_bytes
        )

    def _rotate_chunk(self) -> None:
        log.info(
            "Rotating chunk %d (bytes=%d, msgs=%d)",
            self._index,
            self._chunk_bytes,
            self._chunk_msgs,
        )
        self._finalize_chunk()
        self._channels.clear()
        self._schemas.clear()
        self._sequences.clear()
        self._open_chunk_file()
        # MCAP channel/schema ids are per-file — re-register everything we know
        # so writes resume immediately after rotation.
        for topic, (encoding, schema_bytes) in list(self._channel_specs.items()):
            self.register_channel(topic, encoding, schema_bytes)

    def _open_chunk_file(self) -> None:
        # Hidden temp name (no sha yet) — renamed to canonical on finalize. The
        # dot-prefix keeps it from being mistaken for a finalized chunk.
        self._active_path = self._chunks_dir / f".chunk-{self._index:06d}.mcap.active"
        self._stream = self._active_path.open("wb")
        self._writer = Writer(
            self._stream,
            chunk_size=self._mcap_chunk_size,
            compression=CompressionType.ZSTD,
            enable_crcs=True,
        )
        self._writer.start(profile="", library="bubbaloop-recorder-py")
        self._chunk_start = time.monotonic()
        self._chunk_bytes = 0
        self._chunk_msgs = 0
        self._chunk_log_first = None
        self._chunk_log_last = None
        log.info("Opened MCAP chunk %d at %s", self._index, self._active_path)

    def _finalize_chunk(self) -> None:
        if self._writer is None or self._active_path is None:
            return
        active = self._active_path
        had_messages = self._chunk_msgs > 0

        self._writer.finish()
        self._writer = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None

        if not had_messages:
            # Drop an empty trailing/rotated chunk rather than recording it.
            active.unlink(missing_ok=True)
            self._active_path = None
            return

        sha = sha256_file(active)
        size = active.stat().st_size
        final = self._chunks_dir / canonical_chunk_name(self._index, sha)
        active.rename(final)
        self._active_path = None
        self._total_bytes += size
        self._chunks_finalized += 1
        log.info("Finalized %s (%d bytes)", final.name, size)

        chunk = manifest.Chunk.finalized(
            index=self._index,
            size_bytes=size,
            sha256=sha,
            log_time_first_ns=self._chunk_log_first,
            log_time_last_ns=self._chunk_log_last,
        )
        self._index += 1
        # Notify the session AFTER incrementing so a re-entrant open keeps the
        # next index correct.
        self._on_chunk(chunk)

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    @property
    def current_chunk(self) -> int:
        return self._index

    @property
    def total_messages(self) -> int:
        return self._total_messages

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def chunks_finalized(self) -> int:
        return self._chunks_finalized

    @property
    def active_topics(self) -> int:
        return len(self._channel_specs)
