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
from typing import Any, List, Optional, Sequence, Tuple

import cbor2
import zenoh

from . import manifest
from .mcap_writer import ChunkedMcapWriter, SampleEncoding
from .ring_buffer import BufferedSample, RingBuffer, seal
from .schema_fetch import fetch_descriptor, source_instance_from_topic
from .storage_layout import chunks_dir, recording_dir

log = logging.getLogger(__name__)

# Bounded queue caps memory if writes fall behind. On overflow, callbacks drop
# samples and log periodically (avoids log floods at sustained drop rate).
_QUEUE_MAX = 4096

# A decoded sample: (topic, encoding, payload, publish_time_ns, log_time_ns).
DecodedSample = Tuple[str, SampleEncoding, bytes, int, int]


def extract_publish_time(
    payload: bytes, encoding: SampleEncoding, log_time_ns: int, decode_timestamps: bool
) -> int:
    """Source publish-time (ns) from the message header when available and
    decoding is enabled; otherwise fall back to the recorder receipt clock.
    Shared by streaming and ring-buffer capture."""
    if not decode_timestamps:
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


def decode_sample(sample: zenoh.Sample, decode_timestamps: bool) -> DecodedSample:
    """Decode a Zenoh sample into the recorder's internal tuple. Shared by the
    streaming and ring-buffer hot paths so they can never drift."""
    topic = str(sample.key_expr)
    encoding = SampleEncoding.from_zenoh(str(sample.encoding))
    payload = bytes(sample.payload)
    log_time_ns = time.time_ns()  # recorder receipt clock
    publish_time_ns = extract_publish_time(payload, encoding, log_time_ns, decode_timestamps)
    return topic, encoding, payload, publish_time_ns, log_time_ns


class _DropCounter:
    """Thread-safe drop counter shared by the multiple Zenoh callback threads."""

    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def record(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    @property
    def count(self) -> int:
        return self._n


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
        self._machine_id = machine_id
        self._dir = recording_dir(name)
        self._chunks_dir = chunks_dir(name)
        self._started_mono = time.monotonic()
        # Best-effort protobuf descriptor cache (topic → bytes|None). Fetched
        # asynchronously so the writer thread never blocks on a Zenoh round-trip;
        # a fetched schema attaches from the next chunk-file rotation onward.
        self._schema_cache: dict = {}
        self._schema_lock = threading.Lock()

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

        self._drops = _DropCounter()
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
        # The writer thread is the SOLE owner of self._writer: it drains the
        # queue and calls finish() itself on exit, so we never race it here.
        self._stop_event.set()
        self._writer_thread.join(timeout=15.0)
        alive = self._writer_thread.is_alive()
        if alive:
            log.error("writer thread did not stop within 15s for '%s'", self.name)

        ended = time.time_ns()
        with self._manifest_lock:
            self._recording.ended_at_ns = ended
            self._recording.duration_ns = max(0, ended - self._recording.started_at_ns)
            # Only read writer state if the thread is truly done (else it's a race).
            if not alive:
                self._recording.channels = self._writer.channels()
            self._save_manifest_locked()

        summary = {
            "name": self.name,
            "recording_dir": str(self._dir),
            "chunk_count": len(self._recording.chunks),
            "total_messages": self._writer.total_messages,
            "size_bytes": self._recording.size_bytes,
            "dropped": self._drops.count,
        }
        log.info(
            "Recording '%s' stopped: chunks=%d messages=%d bytes=%d dropped=%d",
            self.name,
            summary["chunk_count"],
            summary["total_messages"],
            summary["size_bytes"],
            summary["dropped"],
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
            "current_chunk_index": current_chunk,
            "finalized_chunks": finalized,
            "messages_recorded": messages,
            "bytes_finalized": size,
            "elapsed_secs": int(time.monotonic() - self._started_mono),
            "dropped": self._drops.count,
        }

    # ------------------------------------------------------------------
    # Manifest persistence (called under the relevant lock)
    # ------------------------------------------------------------------

    def _on_chunk_finalized(self, chunk: manifest.Chunk) -> None:
        # Called from the writer thread (rotate + final finish). Append the chunk
        # + refresh channels, then persist so the sync driver sees a new
        # un-uploaded chunk.
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
            item = decode_sample(sample, self._decode_timestamps)
            self._queue.put_nowait(item)
        except queue.Full:
            n = self._drops.record()
            if n == 1 or n % 100 == 0:
                log.warning("Writer queue full — dropped %d samples total", n)
        except Exception as exc:
            log.warning("Sample handler error: %s", exc)

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

        # Finalize the last chunk here (sole owner of self._writer) so stop()
        # never races a still-running writer thread.
        try:
            with self._writer_lock:
                self._writer.finish()
        except Exception as exc:
            log.error("Final chunk finalize failed for '%s': %s", self.name, exc)

    def _write_one(self, item: DecodedSample) -> None:
        topic, encoding, payload, publish_time_ns, log_time_ns = item
        try:
            schema_bytes = self._schema_for(topic, encoding)
            with self._writer_lock:
                self._writer.register_channel(topic, encoding, schema_bytes=schema_bytes)
                self._writer.write_message(topic, publish_time_ns, log_time_ns, payload)
        except Exception as exc:
            log.warning("Failed to write sample (%s): %s", topic, exc)

    def _schema_for(self, topic: str, encoding: SampleEncoding):
        """Best-effort protobuf FileDescriptorSet for `topic`. Non-blocking: the
        first sighting kicks off a background fetch and returns ``None`` now; a
        fetched schema is applied from the next chunk-file rotation. Never stalls
        the writer thread on the Zenoh round-trip."""
        if encoding.kind != "protobuf":
            return None
        with self._schema_lock:
            if topic in self._schema_cache:
                return self._schema_cache[topic]
            self._schema_cache[topic] = None  # pending → only one fetch per topic
        instance = source_instance_from_topic(topic)
        if instance is not None and self._session is not None:
            threading.Thread(
                target=self._fetch_schema_async,
                args=(topic, instance),
                name="schema-fetch",
                daemon=True,
            ).start()
        return None

    def _fetch_schema_async(self, topic: str, instance: str) -> None:
        data = fetch_descriptor(self._session, self._machine_id, instance)
        if data is None:
            log.debug("no protobuf schema for %s (instance %s)", topic, instance)
            return
        with self._schema_lock:
            self._schema_cache[topic] = data
        # Apply to future chunk files (MCAP can't add a schema to an open channel).
        with self._writer_lock:
            self._writer.set_topic_schema(topic, data)
        log.debug("fetched protobuf schema for %s (%d bytes)", topic, len(data))


class RingBufferSession:
    """Ring-buffer capture (§3.3.5): keep a bounded in-memory window; a `flush`
    command seals the current window into a new recording, then buffering
    continues. Nothing touches disk until a flush."""

    mode = "ring_buffer"

    def __init__(
        self,
        zenoh_session: zenoh.Session,
        machine_id: str,
        topic_patterns: Sequence[str],
        window_secs: int,
        ring_max_bytes: int,
        decode_timestamps: bool,
        exclude: Optional[Sequence[str]] = None,
        recorder_version: str = "",
    ):
        self._session = zenoh_session
        self._machine_id = machine_id
        self._topic_patterns = list(topic_patterns)
        self._exclude = list(exclude or [])
        self._decode_timestamps = decode_timestamps
        self._recorder_version = recorder_version
        self._window_secs = window_secs

        self.name = "ring_buffer"  # not a recording name; for status display
        self.session_id = "ring_buffer"
        self._started_mono = time.monotonic()

        self._ring = RingBuffer(window_ns=window_secs * 1_000_000_000, max_bytes=ring_max_bytes)
        self._lock = threading.Lock()
        # Serializes seals so two concurrent flushes can't interleave disk writes.
        self._flush_lock = threading.Lock()
        self._subscribers: List[Any] = []
        self._flush_count = 0
        self._drops = _DropCounter()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        log.info(
            "Starting ring-buffer capture (window=%ds, cap=%d bytes)",
            self._window_secs,
            self._ring.max_bytes,
        )
        for pattern in self._topic_patterns:
            sub = self._session.declare_subscriber(pattern, self._on_sample)
            self._subscribers.append(sub)
            log.info("Subscribed to '%s'", pattern)

    def stop(self) -> dict:
        for sub in self._subscribers:
            try:
                sub.undeclare()
            except Exception as exc:
                log.warning("Failed to undeclare subscriber: %s", exc)
        self._subscribers.clear()
        with self._lock:
            buffered = len(self._ring)
            self._ring.clear()
            flushes = self._flush_count
        log.info(
            "Ring-buffer capture stopped: flushes=%d, discarded %d buffered sample(s)",
            flushes,
            buffered,
        )
        return {
            "mode": "ring_buffer",
            "flushes": flushes,
            "discarded_buffered": buffered,
        }

    def status(self) -> dict:
        with self._lock:
            buffered = len(self._ring)
            buffered_bytes = self._ring.byte_len
            flushes = self._flush_count
        return {
            "mode": "ring_buffer",
            "window_secs": self._window_secs,
            "buffered_samples": buffered,
            "buffered_bytes": buffered_bytes,
            "flushes": flushes,
            "elapsed_secs": int(time.monotonic() - self._started_mono),
            "dropped": self._drops.count,
        }

    def flush(self, name: str) -> dict:
        """Seal the current window into a new recording `name`. Raises
        ``ValueError`` if the buffer is empty. Seals are serialized so two
        flushes can't interleave disk writes."""
        with self._flush_lock:
            with self._lock:
                snapshot = self._ring.snapshot()
            if not snapshot:
                raise ValueError("ring buffer is empty — nothing to flush")
            rec = seal(
                name=name,
                machine_id=self._machine_id,
                samples=snapshot,
                window_secs=self._window_secs,
                selection=manifest.Selection(
                    topics=list(self._topic_patterns), exclude=list(self._exclude)
                ),
                recorder_version=self._recorder_version,
            )
            with self._lock:
                self._flush_count += 1
        log.info(
            "Flushed ring buffer → '%s' (%d sample(s), %d chunk(s), %d bytes)",
            name,
            len(snapshot),
            len(rec.chunks),
            rec.size_bytes,
        )
        return {
            "name": name,
            "sample_count": len(snapshot),
            "chunk_count": len(rec.chunks),
            "size_bytes": rec.size_bytes,
        }

    # ------------------------------------------------------------------
    # Hot path — runs on Zenoh's threads
    # ------------------------------------------------------------------

    def _on_sample(self, sample: zenoh.Sample) -> None:
        try:
            topic, encoding, payload, publish_time_ns, log_time_ns = decode_sample(
                sample, self._decode_timestamps
            )
            with self._lock:
                self._ring.push(
                    BufferedSample(topic, encoding, publish_time_ns, log_time_ns, payload)
                )
        except Exception as exc:
            log.warning("Ring-buffer sample handler error: %s", exc)
