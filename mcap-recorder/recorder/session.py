"""RecordingSession — wires Zenoh subscribers into a writer thread + MCAP writer.

Subscriber callbacks run on Zenoh's internal threads; the `mcap` Python writer
is single-threaded by design. We bridge with a bounded `queue.Queue`: callbacks
enqueue (cheap, non-blocking), a single writer thread drains and writes.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import cbor2
import zenoh

from .mcap_writer import ChunkedMcapWriter, SampleEncoding

log = logging.getLogger(__name__)

# Bounded queue caps memory if writes fall behind. On overflow, callbacks
# drop samples and log periodically (avoids log floods at sustained drop rate).
_QUEUE_MAX = 4096


class RecordingSession:
    """Single recording session: Zenoh subscribers → queue → writer → MCAP."""

    def __init__(
        self,
        zenoh_session: zenoh.Session,
        topic_patterns: list[str],
        output_dir: Path,
        chunk_duration_secs: int,
        chunk_max_bytes: int,
        decode_timestamps: bool,
    ):
        self._session = zenoh_session
        self._topic_patterns = list(topic_patterns)
        self._output_dir = output_dir
        self._decode_timestamps = decode_timestamps

        self.session_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        self._started_at = time.monotonic()

        self._writer = ChunkedMcapWriter(
            output_dir=output_dir,
            session_id=self.session_id,
            chunk_duration_secs=chunk_duration_secs,
            chunk_max_bytes=chunk_max_bytes,
        )
        self._writer.open_chunk()

        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop_event = threading.Event()
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="mcap-writer", daemon=True
        )
        # Guards `self._writer` so status() can read counters concurrently with writes.
        self._writer_lock = threading.Lock()

        self._dropped = 0
        self._subscribers: list[Any] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        log.info("Starting recording session %s", self.session_id)
        self._writer_thread.start()
        for pattern in self._topic_patterns:
            sub = self._session.declare_subscriber(pattern, self._on_sample)
            self._subscribers.append(sub)
            log.info("Subscribed to '%s'", pattern)

    def stop(self) -> dict:
        log.info("Stopping recording session %s", self.session_id)
        for sub in self._subscribers:
            try:
                sub.undeclare()
            except Exception as exc:
                log.warning("Failed to undeclare subscriber: %s", exc)
        self._subscribers.clear()
        self._stop_event.set()
        self._writer_thread.join(timeout=10.0)
        with self._writer_lock:
            self._writer.finish()
        summary = {
            "session_id": self.session_id,
            "files_written": self._writer.files_written,
            "total_messages": self._writer.total_messages,
            "total_bytes": self._writer.total_bytes,
            "dropped": self._dropped,
        }
        log.info(
            "Session stopped: files=%d messages=%d bytes=%d dropped=%d",
            len(summary["files_written"]),
            summary["total_messages"],
            summary["total_bytes"],
            summary["dropped"],
        )
        return summary

    def status(self) -> dict:
        with self._writer_lock:
            return {
                "session_id": self.session_id,
                "topic_patterns": list(self._topic_patterns),
                "output_dir": str(self._output_dir),
                "active_topics": self._writer.active_topics,
                "current_chunk": self._writer.current_chunk,
                "messages_recorded": self._writer.total_messages,
                "bytes_written": self._writer.total_bytes,
                "elapsed_secs": int(time.monotonic() - self._started_at),
                "dropped": self._dropped,
            }

    # ------------------------------------------------------------------
    # Hot path — runs on Zenoh's threads
    # ------------------------------------------------------------------

    def _on_sample(self, sample: zenoh.Sample) -> None:
        try:
            topic = str(sample.key_expr)
            encoding = SampleEncoding.from_zenoh(str(sample.encoding))
            payload = bytes(sample.payload)
            ts_ns = self._extract_timestamp(payload, encoding)
            self._queue.put_nowait((topic, encoding, payload, ts_ns))
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                log.warning("Writer queue full — dropped %d samples total", self._dropped)
        except Exception as exc:
            log.warning("Sample handler error: %s", exc)

    def _extract_timestamp(self, payload: bytes, encoding: SampleEncoding) -> int:
        """Return ns timestamp for this sample. Wall-clock unless decode is enabled."""
        if not self._decode_timestamps:
            return time.time_ns()
        try:
            if encoding.kind == "cbor":
                obj = cbor2.loads(payload)
            elif encoding.kind == "json":
                obj = json.loads(payload)
            else:
                return time.time_ns()
            if isinstance(obj, dict):
                header = obj.get("header")
                if isinstance(header, dict):
                    ts = header.get("ts_ns")
                    if isinstance(ts, int):
                        return ts
        except Exception:
            pass
        return time.time_ns()

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

        # Drain anything queued after stop signal so we don't lose tail samples.
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            self._write_one(item)

    def _write_one(self, item: tuple[str, SampleEncoding, bytes, int]) -> None:
        topic, encoding, payload, ts_ns = item
        try:
            with self._writer_lock:
                self._writer.register_channel(topic, encoding, schema_bytes=None)
                self._writer.write_message(topic, ts_ns, payload)
        except Exception as exc:
            log.warning("Failed to write sample (%s): %s", topic, exc)
