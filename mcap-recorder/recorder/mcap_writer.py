"""Chunked MCAP writer with `.active` → `.mcap` atomic-rename pattern.

Port of the Rust recorder's ChunkedMcapWriter (mcap_writer.rs). The rename
discipline lets a process crash leave an identifiable incomplete chunk —
file inspectors can spot mid-write data by suffix without reading bytes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional

from mcap.writer import Writer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SampleEncoding:
    """Encoding tier extracted from a Zenoh sample's encoding string."""

    kind: str  # "cbor" | "json" | "protobuf" | "raw"
    schema_name: str = ""

    @classmethod
    def from_zenoh(cls, encoding: str) -> "SampleEncoding":
        if encoding.startswith("application/cbor"):
            return cls("cbor")
        if encoding.startswith("application/json"):
            return cls("json")
        if encoding.startswith("application/protobuf"):
            schema = encoding.split(";", 1)[1] if ";" in encoding else ""
            return cls("protobuf", schema)
        return cls("raw")

    @property
    def message_encoding(self) -> str:
        return {
            "cbor": "cbor",
            "json": "json",
            "protobuf": "protobuf",
            "raw": "",
        }[self.kind]


class ChunkedMcapWriter:
    """Writes MCAP messages with size/time-based chunk rotation.

    Each chunk file is opened as ``{name}.mcap.active`` and renamed to
    ``{name}.mcap`` on `finish()` — a crash leaves the last chunk visible
    by its `.active` suffix.
    """

    def __init__(
        self,
        output_dir: Path,
        session_id: str,
        chunk_duration_secs: int,
        chunk_max_bytes: int,
    ):
        self._output_dir = output_dir
        self._session_id = session_id
        self._chunk_duration_secs = chunk_duration_secs
        self._chunk_max_bytes = chunk_max_bytes

        self._writer: Optional[Writer] = None
        self._stream: Optional[IO[bytes]] = None
        self._current_chunk = 0
        self._chunk_start = time.monotonic()
        self._chunk_bytes = 0

        # Per-file (cleared on rotate, re-registered from _channel_specs).
        self._channels: dict[str, int] = {}
        self._schemas: dict[str, int] = {}
        self._sequences: dict[int, int] = {}

        # Persistent across files — used to re-register on rotate.
        self._channel_specs: dict[str, tuple[SampleEncoding, Optional[bytes]]] = {}

        self._total_messages = 0
        self._total_bytes = 0
        self._files_written: list[str] = []

    def open_chunk(self) -> None:
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

        channel_id = self._writer.register_channel(
            topic=topic,
            message_encoding=encoding.message_encoding,
            schema_id=schema_id,
        )
        self._channels[topic] = channel_id
        self._sequences[channel_id] = 0
        self._channel_specs[topic] = (encoding, schema_bytes)
        return channel_id

    def write_message(self, topic: str, timestamp_ns: int, data: bytes) -> None:
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
            log_time=timestamp_ns,
            publish_time=timestamp_ns,
            sequence=self._sequences[channel_id],
            data=data,
        )

        n = len(data)
        self._chunk_bytes += n
        self._total_messages += 1
        self._total_bytes += n

    def finish(self) -> None:
        if self._writer is None:
            return
        self._writer.finish()
        self._writer = None
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        self._rename_active_to_final()

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
        return (
            time.monotonic() - self._chunk_start >= self._chunk_duration_secs
            or self._chunk_bytes >= self._chunk_max_bytes
        )

    def _rotate_chunk(self) -> None:
        log.info(
            "Rotating chunk %d (bytes=%d, elapsed=%ds)",
            self._current_chunk,
            self._chunk_bytes,
            int(time.monotonic() - self._chunk_start),
        )
        self.finish()
        self._current_chunk += 1
        self._channels.clear()
        self._schemas.clear()
        self._sequences.clear()
        self._open_chunk_file()
        # MCAP channel/schema IDs are per-file — re-register everything we
        # know about so writes can resume immediately after rotation.
        for topic, (encoding, schema_bytes) in list(self._channel_specs.items()):
            self.register_channel(topic, encoding, schema_bytes)

    def _open_chunk_file(self) -> None:
        filename = f"{self._session_id}_chunk_{self._current_chunk:03d}.mcap"
        active_path = self._output_dir / f"{filename}.active"
        self._stream = active_path.open("wb")
        self._writer = Writer(self._stream)
        self._writer.start(profile="", library="bubbaloop-recorder-py")
        self._chunk_start = time.monotonic()
        self._chunk_bytes = 0
        self._files_written.append(filename)
        log.info("Opened MCAP chunk %d at %s", self._current_chunk, active_path)

    def _rename_active_to_final(self) -> None:
        filename = f"{self._session_id}_chunk_{self._current_chunk:03d}.mcap"
        active = self._output_dir / f"{filename}.active"
        final = self._output_dir / filename
        if active.exists():
            active.rename(final)
            log.info("Finalized %s", final)

    @property
    def current_chunk(self) -> int:
        return self._current_chunk

    @property
    def total_messages(self) -> int:
        return self._total_messages

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def files_written(self) -> list[str]:
        return list(self._files_written)

    @property
    def active_topics(self) -> int:
        return len(self._channel_specs)
