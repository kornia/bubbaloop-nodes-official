"""Filesystem tests for the chunked MCAP writer.

These run without a Zenoh session — they exercise rotation, canonical-name +
SHA-256 finalize, the §4.5 channel metadata, and dual timestamps directly.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import List

import pytest

# Allow running tests from repo root without `pip install -e .`
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcap.reader import make_reader

from recorder import manifest
from recorder.mcap_writer import (
    META_SCHEMA_NAME,
    META_ZENOH_ENCODING,
    META_ZENOH_TOPIC,
    ChunkedMcapWriter,
    SampleEncoding,
)
from recorder.storage_layout import canonical_chunk_name, sha256_file


# ----------------------------------------------------------------------
# SampleEncoding.from_zenoh
# ----------------------------------------------------------------------


def test_encoding_cbor():
    e = SampleEncoding.from_zenoh("application/cbor")
    assert e.kind == "cbor"
    assert e.message_encoding == "cbor"
    assert e.zenoh_encoding == "application/cbor"


def test_encoding_protobuf_with_schema():
    e = SampleEncoding.from_zenoh("application/protobuf;bubbaloop.foo.v1.Bar")
    assert e.kind == "protobuf"
    assert e.schema_name == "bubbaloop.foo.v1.Bar"


def test_encoding_zenoh_bytes_is_raw():
    e = SampleEncoding.from_zenoh("zenoh/bytes")
    assert e.kind == "raw"
    assert e.message_encoding == "raw"
    assert e.zenoh_encoding == "zenoh/bytes"


# ----------------------------------------------------------------------
# ChunkedMcapWriter — finalize / canonical names / metadata
# ----------------------------------------------------------------------


def _writer(d: Path, chunk_max_bytes: int, sink: List[manifest.Chunk]) -> ChunkedMcapWriter:
    return ChunkedMcapWriter(
        chunks_dir=d,
        chunk_duration_secs=10_000,
        chunk_max_bytes=chunk_max_bytes,
        on_chunk_finalized=sink.append,
    )


def test_finalize_writes_canonical_hashed_chunk():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        chunks: List[manifest.Chunk] = []
        w = _writer(d, 1 << 30, chunks)
        w.open_chunk()
        # While writing, only the hidden `.active` temp exists.
        assert (d / ".chunk-000000.mcap.active").exists()

        w.register_channel("test/topic", SampleEncoding.from_zenoh("application/json"))
        w.write_message("test/topic", publish_time_ns=2, log_time_ns=5, data=b'{"v":42}')
        w.finish()

        # One finalized chunk, canonical name == hash-derived.
        assert len(chunks) == 1
        c = chunks[0]
        final = d / c.name
        assert final.exists()
        assert c.name == canonical_chunk_name(0, c.sha256)
        assert sha256_file(final) == c.sha256
        assert c.index == 0 and c.size_bytes == final.stat().st_size
        assert c.log_time_first_ns == 5 and c.log_time_last_ns == 5
        assert c.uploaded_at_ns is None
        # No leftover temp.
        assert not (d / ".chunk-000000.mcap.active").exists()


def test_rotation_produces_contiguous_indexed_chunks():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        chunks: List[manifest.Chunk] = []
        w = _writer(d, 100, chunks)  # 100-byte rotation threshold
        w.open_chunk()
        w.register_channel("t/a", SampleEncoding.from_zenoh("application/json"))
        w.write_message("t/a", publish_time_ns=1, log_time_ns=1, data=b"x" * 150)
        w.write_message("t/a", publish_time_ns=2, log_time_ns=2, data=b"y" * 150)
        w.finish()

        assert [c.index for c in chunks] == [0, 1, 2] or [c.index for c in chunks] == [0, 1]
        # Indices are contiguous from 0 and names are canonical.
        for i, c in enumerate(chunks):
            assert c.index == i
            assert c.name == canonical_chunk_name(c.index, c.sha256)
            assert (d / c.name).exists()


def test_empty_trailing_chunk_is_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        chunks: List[manifest.Chunk] = []
        w = _writer(d, 1 << 30, chunks)
        w.open_chunk()
        # finish without writing anything → no chunk emitted, no file left.
        w.finish()
        assert chunks == []
        assert list(d.glob("*.mcap")) == []
        assert list(d.glob("*.active")) == []


def test_write_to_unregistered_channel_fails():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = _writer(d, 1 << 30, [])
        w.open_chunk()
        with pytest.raises(RuntimeError):
            w.write_message("nope/topic", publish_time_ns=1, log_time_ns=1, data=b"hi")


def test_set_topic_schema_updates_future_registration():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        w = _writer(d, 1 << 30, [])
        w.open_chunk()
        enc = SampleEncoding.from_zenoh("application/protobuf;p.v1.M")
        w.register_channel("t/p", enc)  # registered before the schema arrives
        assert w._channel_specs["t/p"][1] is None
        # an async fetch completes → schema attached for the NEXT chunk file
        w.set_topic_schema("t/p", b"\x0a\x02fd")
        assert w._channel_specs["t/p"][1] == b"\x0a\x02fd"
        # unknown topic is a safe no-op (fetch raced ahead of first sample)
        w.set_topic_schema("never/seen", b"x")  # must not raise


def test_channels_metadata_and_readback():
    """The finalized MCAP carries §4.5 channel metadata and dual timestamps that
    storage::replay::read_recording_messages reads back."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        chunks: List[manifest.Chunk] = []
        w = _writer(d, 1 << 30, chunks)
        w.open_chunk()
        enc = SampleEncoding.from_zenoh("application/protobuf;bubbaloop.cam.v1.Frame")
        w.register_channel("bubbaloop/global/m/cam/frame", enc, schema_bytes=b"\x01\x02")
        w.write_message(
            "bubbaloop/global/m/cam/frame", publish_time_ns=111, log_time_ns=222, data=b"abc"
        )
        w.finish()

        # manifest channel projection
        chans = w.channels()
        assert len(chans) == 1
        ch = chans[0]
        assert ch.topic == "bubbaloop/global/m/cam/frame"
        assert ch.message_encoding == "protobuf"
        assert ch.zenoh_encoding == "application/protobuf;bubbaloop.cam.v1.Frame"
        assert ch.schema_name == "bubbaloop.cam.v1.Frame"
        assert ch.message_count == 1
        assert ch.publish_time_first_ns == 111 and ch.publish_time_last_ns == 111

        # read the MCAP back: metadata keys + dual timestamps survive
        with open(d / chunks[0].name, "rb") as f:
            reader = make_reader(f)
            seen = 0
            for schema, channel, message in reader.iter_messages():
                seen += 1
                assert channel.metadata[META_ZENOH_TOPIC] == "bubbaloop/global/m/cam/frame"
                assert (
                    channel.metadata[META_ZENOH_ENCODING]
                    == "application/protobuf;bubbaloop.cam.v1.Frame"
                )
                assert channel.metadata[META_SCHEMA_NAME] == "bubbaloop.cam.v1.Frame"
                assert message.publish_time == 111
                assert message.log_time == 222
            assert seen == 1
