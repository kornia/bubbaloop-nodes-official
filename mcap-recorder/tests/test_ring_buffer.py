"""Tests for ring-buffer capture mode (§3.3.5)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Allow running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder import manifest
from recorder.mcap_writer import SampleEncoding
from recorder.ring_buffer import BufferedSample, RingBuffer, seal
from recorder.storage_layout import canonical_chunk_name

SEC = 1_000_000_000


def _sample(topic: str, log_ns: int, data: bytes = b"xxxx") -> BufferedSample:
    return BufferedSample(
        topic=topic,
        encoding=SampleEncoding.from_zenoh("application/cbor"),
        publish_time_ns=log_ns,
        log_time_ns=log_ns,
        data=data,
    )


# ---------------------------------------------------------------------------
# RingBuffer eviction
# ---------------------------------------------------------------------------


def test_time_eviction_keeps_window():
    rb = RingBuffer(window_ns=5 * SEC, max_bytes=1 << 30)
    for t in range(0, 11):  # 0s..10s
        rb.push(_sample("t/a", t * SEC))
    # newest = 10s, window 5s → keep samples with log_time >= 5s (5..10 = 6).
    kept = [s.log_time_ns // SEC for s in rb.snapshot()]
    assert kept == [5, 6, 7, 8, 9, 10]


def test_byte_eviction_keeps_at_least_one():
    rb = RingBuffer(window_ns=1 << 62, max_bytes=10)
    rb.push(_sample("t/a", 1, data=b"x" * 8))
    rb.push(_sample("t/a", 2, data=b"y" * 8))  # 16 > 10 → evict first
    assert len(rb) == 1
    assert rb.byte_len == 8
    # A single oversized sample is still kept (never evict the last one).
    rb.push(_sample("t/a", 3, data=b"z" * 100))
    assert len(rb) == 1
    assert rb.snapshot()[0].log_time_ns == 3


def test_byte_len_tracks_pushes_and_evictions():
    rb = RingBuffer(window_ns=1 << 62, max_bytes=1 << 30)
    rb.push(_sample("t/a", 1, data=b"ab"))
    rb.push(_sample("t/a", 2, data=b"cde"))
    assert rb.byte_len == 5 and len(rb) == 2


def test_rejects_bad_bounds():
    for kw in ({"window_ns": 0}, {"window_ns": 1, "max_bytes": 0}):
        try:
            RingBuffer(**{"window_ns": 1, "max_bytes": 1, **kw})
            raise AssertionError("accepted bad bounds")
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# seal()
# ---------------------------------------------------------------------------


def test_seal_writes_ring_buffer_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    samples = [_sample("bubbaloop/global/m/cam/frame", t * SEC, data=b"d" * 50) for t in range(4)]
    rec = seal(
        name="window_1",
        machine_id="m",
        samples=samples,
        window_secs=5,
        selection=manifest.Selection(topics=["bubbaloop/global/**"]),
        recorder_version="0.1.0",
    )
    # manifest shape
    d = json.loads(rec.to_json())
    assert d["mode"] == "ring_buffer"
    assert d["window_secs"] == 5
    assert d["trigger"] == "manual"
    assert d["started_at_ns"] == 0 and d["ended_at_ns"] == 3 * SEC
    assert len(d["chunks"]) >= 1
    for i, c in enumerate(d["chunks"]):
        assert c["index"] == i
        assert c["name"] == canonical_chunk_name(c["index"], c["sha256"])
    # chunk files exist on disk + manifest.json written
    rec_dir = tmp_path / ".bubbaloop" / "recordings" / "window_1"
    assert (rec_dir / "manifest.json").exists()
    for c in rec.chunks:
        assert (rec_dir / "chunks" / c.name).exists()


def test_seal_empty_window_is_valid_empty_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rec = seal(
        name="empty_win",
        machine_id="m",
        samples=[],
        window_secs=5,
        selection=manifest.Selection(topics=["**"]),
    )
    assert rec.chunks == []
    assert rec.size_bytes == 0
    assert (tmp_path / ".bubbaloop" / "recordings" / "empty_win" / "manifest.json").exists()


# ---------------------------------------------------------------------------
# RingBufferSession.flush (no Zenoh — drive the buffer directly)
# ---------------------------------------------------------------------------


def test_session_flush_seals_window(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from recorder.session import RingBufferSession

    sess = RingBufferSession(
        zenoh_session=None,  # unused until start()
        machine_id="m",
        topic_patterns=["bubbaloop/global/**"],
        window_secs=10,
        ring_max_bytes=1 << 30,
        decode_timestamps=False,
        recorder_version="0.1.0",
    )
    for t in range(3):
        sess._ring.push(_sample("bubbaloop/global/m/cam/frame", t * SEC, data=b"d" * 40))

    summary = sess.flush("event_42")
    assert summary["name"] == "event_42"
    assert summary["sample_count"] == 3
    assert summary["chunk_count"] >= 1

    d = tmp_path / ".bubbaloop" / "recordings" / "event_42"
    rec = json.loads((d / "manifest.json").read_bytes())
    assert rec["mode"] == "ring_buffer"
    assert rec["window_secs"] == 10
    # buffering continues after a flush
    assert sess.status()["flushes"] == 1
    assert sess.status()["buffered_samples"] == 3


def test_session_flush_empty_buffer_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from recorder.session import RingBufferSession

    sess = RingBufferSession(
        zenoh_session=None,
        machine_id="m",
        topic_patterns=["**"],
        window_secs=5,
        ring_max_bytes=1 << 30,
        decode_timestamps=False,
    )
    with pytest.raises(ValueError, match="empty"):
        sess.flush("nothing")


def test_session_stop_clears_buffer(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from recorder.session import RingBufferSession

    sess = RingBufferSession(
        zenoh_session=None,
        machine_id="m",
        topic_patterns=["**"],
        window_secs=5,
        ring_max_bytes=1 << 30,
        decode_timestamps=False,
    )
    sess._ring.push(_sample("t/a", SEC))
    summary = sess.stop()
    assert summary["mode"] == "ring_buffer"
    assert summary["discarded_buffered"] == 1
    assert len(sess._ring) == 0
