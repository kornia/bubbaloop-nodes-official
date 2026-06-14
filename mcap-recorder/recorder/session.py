"""RecordingSession — Zenoh subscribers → writer thread → MCAP + manifest.

A session owns one recording directory under ``~/.bubbaloop/recordings/<name>/``:
``manifest.json`` plus ``chunks/*.mcap``. Subscriber callbacks (on Zenoh's
threads) enqueue samples; a single writer thread drains the queue into the
chunked MCAP writer. Each finalized chunk is appended to the manifest and the
manifest is re-saved atomically — that running manifest is exactly what the
daemon's storage layer reads (`storage list/info/upload/reconcile/replay`) and
what the sync driver scans to upload un-uploaded chunks.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, List, Optional, Sequence

import cbor2
import zenoh

from . import manifest
from .mcap_writer import ChunkedMcapWriter, SampleEncoding
from .storage_layout import chunks_dir, recording_dir

log = logging.getLogger(__name__)

# Bounded queue caps memory if writes fall behind. On overflow, callbacks drop
# samples and log periodically (avoids log floods at sustained drop rate).
_QUEUE_MAX = 4096


class RecordingSession:
    """Single recording session writing a storage-layer-compatible recording."""

    def __init__(
        self,
        zenoh_session: zenoh.Session,
        name: str,
        machine_id: str,
        topic_patterns: Sequence[str],
        chunk_duration_secs: int,
        chunk_max_bytes: int,
        decode_timestamps: bool,
        exclude: Optional[Sequence[str]] = None,
        recorder_version: str = "",
        fleet_router: Optional[str] = None,
        profile_name: Optional[str] = None,
    ):
        self._session = zenoh_session
        self._topic_patterns = list(topic_patterns)
        self._decode_timestamps = decode_timestamps

        # recording_dir validates `name` (path-traversal guard) and pins the
        # output under the recordings root the storage layer scans.
        self.name = name
        self.session_id = name  # kept for status compatibility
        self._dir = recording_dir(name)
        self._chunks_dir = chunks_dir(name)
        self._started_mono = time.monotonic()

        self._recording = manifest.Recording(
            name=name,
            machine_id=machine_id,
            started_at_ns=time.time_ns(),
            mode="streaming",
            selection=manifest.Selection(
                topics=list(topic_patterns),
                exclude=list(exclude or []),
            ),
            fleet_router=fleet_router,
            profile_name=profile_name,
            recorder_version=recorder_version,
        )
        # Guards the manifest model — touched by the writer thread (chunk
        # finalize) and the control thread (start/stop).
        self._manifest_lock = threading.Lock()

        self._writer = ChunkedMcapWriter(
            chunks_dir=self._chunks_dir,
            chunk_duration_secs=chunk_duration_secs,
            chunk_max_bytes=chunk_max_bytes,
            on_chunk_finalized=self._on_chunk_finalized,
        )

        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop_event = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="mcap-writer", daemon=True
        )
        # Guards `self._writer` so status() can read counters concurrently.
        self._writer_lock = threading.Lock()

        self._dropped = 0
        self._subscribers: List[Any] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        log.info("Starting recording '%s' at %s", self.name, self._dir)
        self._chunks_dir.mkdir(parents=True, exist_ok=True)
        # Persist an "open" manifest (no chunks, ended_at_ns null) immediately so
        # `storage list` shows the recording while it's being written, and a
        # crash still leaves a discoverable directory.
        self._save_manifest()
        self._writer.open_chunk()
        self._writer_thread.start()
        for pattern in self._topic_patterns:
            sub = self._session.declare_subscriber(pattern, self._on_sample)
            self._subscribers.append(sub)
            log.info("Subscribed to '%s'", pattern)

    def stop(self) -> dict:
        log.info("Stopping recording '%s'", self.name)
        for sub in self._subscribers:
            try:
                sub.undeclare()
            except Exception as exc:
                log.warning("Failed to undeclare subscriber: %s", exc)
        self._subscribers.clear()
        self._stop_event.set()
        self._writer_thread.join(timeout=10.0)
        with self._writer_lock:
            self._writer.finish()  # emits the last chunk via _on_chunk_finalized

        ended = time.time_ns()
        with self._manifest_lock:
            self._recording.ended_at_ns = ended
            self._recording.duration_ns = max(0, ended - self._recording.started_at_ns)
            self._recording.channels = self._writer.channels()
            self._save_manifest_locked()

        summary = {
            "name": self.name,
            "recording_dir": str(self._dir),
            "chunk_count": len(self._recording.chunks),
            "total_messages": self._writer.total_messages,
            "size_bytes": self._recording.size_bytes,
            "dropped": self._dropped,
        }
        log.info(
            "Recording '%s' stopped: chunks=%d messages=%d bytes=%d dropped=%d",
            self.name,
            summary["chunk_count"],
            summary["total_messages"],
            summary["size_bytes"],
            self._dropped,
        )
        return summary

    def status(self) -> dict:
        with self._writer_lock:
            current_chunk = self._writer.current_chunk
            messages = self._writer.total_messages
            size = self._writer.total_bytes
            topics = self._writer.active_topics
        with self._manifest_lock:
            finalized = len(self._recording.chunks)
        return {
            "name": self.name,
            "recording_dir": str(self._dir),
            "topic_patterns": list(self._topic_patterns),
            "active_topics": topics,
            "current_chunk": current_chunk,
            "finalized_chunks": finalized,
            "messages_recorded": messages,
            "bytes_finalized": size,
            "elapsed_secs": int(time.monotonic() - self._started_mono),
            "dropped": self._dropped,
        }

    # ------------------------------------------------------------------
    # Manifest persistence (called under the relevant lock)
    # ------------------------------------------------------------------

    def _on_chunk_finalized(self, chunk: manifest.Chunk) -> None:
        # Called from the writer thread (rotate) or the control thread (stop's
        # finish()). Append the chunk + refresh channels, then persist so the
        # sync driver sees a new un-uploaded chunk.
        with self._manifest_lock:
            self._recording.chunks.append(chunk)
            self._recording.size_bytes += chunk.size_bytes
            self._recording.channels = self._writer.channels()
            self._save_manifest_locked()

    def _save_manifest(self) -> None:
        with self._manifest_lock:
            self._save_manifest_locked()

    def _save_manifest_locked(self) -> None:
        try:
            manifest.save_atomic(self._dir, self._recording)
        except Exception as exc:
            log.error("Failed to persist manifest for '%s': %s", self.name, exc)

    # ------------------------------------------------------------------
    # Hot path — runs on Zenoh's threads
    # ------------------------------------------------------------------

    def _on_sample(self, sample: zenoh.Sample) -> None:
        try:
            topic = str(sample.key_expr)
            encoding = SampleEncoding.from_zenoh(str(sample.encoding))
            payload = bytes(sample.payload)
            log_time_ns = time.time_ns()  # recorder receipt clock
            publish_time_ns = self._publish_time(payload, encoding, log_time_ns)
            self._queue.put_nowait((topic, encoding, payload, publish_time_ns, log_time_ns))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                log.warning("Writer queue full — dropped %d samples total", self._dropped)
        except Exception as exc:
            log.warning("Sample handler error: %s", exc)

    def _publish_time(self, payload: bytes, encoding: SampleEncoding, log_time_ns: int) -> int:
        """Source publish-time (ns) from the message header when available and
        decoding is enabled; otherwise fall back to the receipt clock."""
        if not self._decode_timestamps:
            return log_time_ns
        try:
            if encoding.kind == "cbor":
                obj = cbor2.loads(payload)
            elif encoding.kind == "json":
                obj = json.loads(payload)
            else:
                return log_time_ns
            if isinstance(obj, dict):
                header = obj.get("header")
                if isinstance(header, dict):
                    ts = header.get("ts_ns")
                    if isinstance(ts, int):
                        return ts
        except Exception:
            pass
        return log_time_ns

    # ------------------------------------------------------------------
    # Writer thread
    # ------------------------------------------------------------------

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._write_one(item)

        # Drain anything queued after the stop signal so tail samples survive.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._write_one(item)

    def _write_one(self, item) -> None:
        topic, encoding, payload, publish_time_ns, log_time_ns = item
        try:
            with self._writer_lock:
                self._writer.register_channel(topic, encoding, schema_bytes=None)
                self._writer.write_message(topic, publish_time_ns, log_time_ns, payload)
        except Exception as exc:
            log.warning("Failed to write sample (%s): %s", topic, exc)
