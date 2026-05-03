"""Filesystem tests for the chunked MCAP writer.

These run without a Zenoh session — they exercise the rotation, atomic
rename, and channel-registration paths directly.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# Allow running tests from repo root without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder.mcap_writer import ChunkedMcapWriter, SampleEncoding


# ----------------------------------------------------------------------
# SampleEncoding.from_zenoh
# ----------------------------------------------------------------------

def test_encoding_cbor():
    e = SampleEncoding.from_zenoh("application/cbor")
    assert e.kind == "cbor"
    assert e.message_encoding == "cbor"


def test_encoding_json():
    e = SampleEncoding.from_zenoh("application/json")
    assert e.kind == "json"
    assert e.message_encoding == "json"


def test_encoding_protobuf_with_schema():
    e = SampleEncoding.from_zenoh("application/protobuf;bubbaloop.foo.v1.Bar")
    assert e.kind == "protobuf"
    assert e.schema_name == "bubbaloop.foo.v1.Bar"


def test_encoding_protobuf_without_schema():
    e = SampleEncoding.from_zenoh("application/protobuf")
    assert e.kind == "protobuf"
    assert e.schema_name == ""


def test_encoding_zenoh_bytes_is_raw():
    e = SampleEncoding.from_zenoh("zenoh/bytes")
    assert e.kind == "raw"
    assert e.message_encoding == ""


def test_encoding_empty_is_raw():
    assert SampleEncoding.from_zenoh("").kind == "raw"


# ----------------------------------------------------------------------
# ChunkedMcapWriter
# ----------------------------------------------------------------------

def test_open_write_finish():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = ChunkedMcapWriter(d, "sess1", 300, 1_073_741_824)
        w.open_chunk()
        # While writing, only `.active` exists.
        assert (d / "sess1_chunk_000.mcap.active").exists()
        assert not (d / "sess1_chunk_000.mcap").exists()

        w.register_channel("test/topic", SampleEncoding("json"))
        data = b'{"value": 42}'
        w.write_message("test/topic", 1_000_000_000, data)
        assert w.total_messages == 1
        assert w.total_bytes == len(data)

        w.finish()
        # After finish: `.active` gone, `.mcap` exists.
        assert not (d / "sess1_chunk_000.mcap.active").exists()
        path = d / "sess1_chunk_000.mcap"
        assert path.exists()
        assert path.stat().st_size > 0


def test_chunk_rotation_on_size():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = ChunkedMcapWriter(d, "sess2", 300, 100)  # 100-byte chunk threshold
        w.open_chunk()
        w.register_channel("test/topic", SampleEncoding("json"))

        big = b"x" * 150
        w.write_message("test/topic", 1_000_000_000, big)
        w.write_message("test/topic", 2_000_000_000, big)

        assert w.current_chunk == 1
        assert w.total_messages == 2

        # Chunk 0 was rotated → renamed to `.mcap`.
        assert (d / "sess2_chunk_000.mcap").exists()
        assert not (d / "sess2_chunk_000.mcap.active").exists()
        # Chunk 1 is mid-write.
        assert (d / "sess2_chunk_001.mcap.active").exists()

        w.finish()
        assert (d / "sess2_chunk_001.mcap").exists()
        assert not (d / "sess2_chunk_001.mcap.active").exists()


def test_register_channel_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = ChunkedMcapWriter(d, "sess3", 300, 1_073_741_824)
        w.open_chunk()
        a = w.register_channel("test/topic", SampleEncoding("cbor"))
        b = w.register_channel("test/topic", SampleEncoding("cbor"))
        assert a == b
        w.finish()


def test_write_to_unregistered_channel_fails():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = ChunkedMcapWriter(d, "sess4", 300, 1_073_741_824)
        w.open_chunk()
        with pytest.raises(RuntimeError):
            w.write_message("nonexistent/topic", 1_000_000_000, b"hi")
        w.finish()


def test_channels_persist_across_rotation():
    """After rotation, previously-registered channels are re-registered in the new file."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = ChunkedMcapWriter(d, "sess5", 300, 100)
        w.open_chunk()
        w.register_channel("topic/a", SampleEncoding("cbor"))
        w.register_channel("topic/b", SampleEncoding("json"))
        # Force a rotation.
        w.write_message("topic/a", 1, b"x" * 150)
        # `topic/b` should still be writable in the new chunk without a fresh
        # register_channel call, because rotation re-registers from specs.
        w.write_message("topic/b", 2, b"y")
        assert w.current_chunk == 1
        w.finish()
