"""Ring-buffer capture mode (spec §3.3.5).

In ring-buffer mode the recorder keeps a bounded, in-memory sliding window of the
most recent samples instead of streaming straight to disk. A `flush` command
seals the *current* window into a brand-new recording on disk (manifest + MCAP
chunks), then buffering continues — so you can capture "the last N seconds"
around an event after the fact.

The window is bounded two ways (mirrors `storage::ring_buffer::RingBuffer`):
a time window (`window_secs`) and a hard byte cap (default 256 MiB). Eviction is
FIFO and always keeps at least one sample. The buffer is pure + clock-free
(callers pass `log_time_ns`), so it unit-tests without Zenoh; [`seal`] is the
side-effecting part that writes the recording, reusing the same
[`ChunkedMcapWriter`] + manifest path as streaming mode.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Sequence

from . import manifest
from .mcap_writer import ChunkedMcapWriter, SampleEncoding
from .storage_layout import chunks_dir, recording_dir

# Hard byte cap on the in-memory window (matches storage DEFAULT_RING_MAX_BYTES).
DEFAULT_RING_MAX_BYTES = 256 * 1024 * 1024
# File-roll threshold when sealing a window to disk.
SEAL_FILE_ROLL_BYTES = 256 * 1024 * 1024


@dataclass
class BufferedSample:
    topic: str
    encoding: SampleEncoding
    publish_time_ns: int
    log_time_ns: int
    data: bytes


class RingBuffer:
    """A bounded, FIFO sliding window of samples (time + byte bounded)."""

    def __init__(self, window_ns: int, max_bytes: int = DEFAULT_RING_MAX_BYTES):
        if window_ns <= 0:
            raise ValueError("window_ns must be > 0")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        self._window_ns = window_ns
        self._max_bytes = max_bytes
        self._samples: "deque[BufferedSample]" = deque()
        self._bytes = 0

    def push(self, sample: BufferedSample) -> None:
        self._samples.append(sample)
        self._bytes += len(sample.data)
        self._evict(sample.log_time_ns)

    def _evict(self, newest_ns: int) -> None:
        # Time-based: drop anything older than the window, but always keep ≥1.
        cutoff = newest_ns - self._window_ns
        while len(self._samples) > 1 and self._samples[0].log_time_ns < cutoff:
            self._bytes -= len(self._samples.popleft().data)
        # Byte-based: drop oldest while over the cap, but always keep ≥1.
        while len(self._samples) > 1 and self._bytes > self._max_bytes:
            self._bytes -= len(self._samples.popleft().data)

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def snapshot(self) -> List[BufferedSample]:
        return list(self._samples)

    def clear(self) -> None:
        self._samples.clear()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._samples)

    @property
    def byte_len(self) -> int:
        return self._bytes


def seal(
    name: str,
    machine_id: str,
    samples: Sequence[BufferedSample],
    window_secs: int,
    selection: manifest.Selection,
    *,
    recorder_version: str = "",
    sealed_at_ns: Optional[int] = None,
    file_roll_bytes: int = SEAL_FILE_ROLL_BYTES,
) -> manifest.Recording:
    """Seal `samples` (a window snapshot) into a new ring-buffer recording on
    disk and return its manifest. Samples are written in log-time order through
    the canonical chunk writer, so the output is identical in shape to a
    streaming recording (just `mode=ring_buffer`)."""
    d = recording_dir(name)
    cd = chunks_dir(name)
    cd.mkdir(parents=True, exist_ok=True)

    ordered = sorted(samples, key=lambda s: s.log_time_ns)
    started_at_ns = ordered[0].log_time_ns if ordered else (sealed_at_ns or time.time_ns())
    ended_at_ns = ordered[-1].log_time_ns if ordered else started_at_ns

    rec = manifest.Recording(
        name=name,
        machine_id=machine_id,
        started_at_ns=started_at_ns,
        mode="ring_buffer",
        selection=selection,
        window_secs=window_secs,
        trigger="manual",
        recorder_version=recorder_version,
        ended_at_ns=ended_at_ns,
        duration_ns=max(0, ended_at_ns - started_at_ns),
    )

    chunks: List[manifest.Chunk] = []
    writer = ChunkedMcapWriter(
        chunks_dir=cd,
        chunk_duration_secs=1 << 62,  # never roll on time; only on size
        chunk_max_bytes=file_roll_bytes,
        on_chunk_finalized=chunks.append,
    )
    writer.open_chunk()
    for s in ordered:
        writer.register_channel(s.topic, s.encoding)
        writer.write_message(s.topic, s.publish_time_ns, s.log_time_ns, s.data)
    writer.finish()

    rec.chunks = chunks
    rec.size_bytes = sum(c.size_bytes for c in chunks)
    rec.channels = writer.channels()
    manifest.save_atomic(d, rec)
    return rec
