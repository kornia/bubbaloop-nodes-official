"""Tests for the storage-layout + manifest contract shared with the Rust
storage layer. These guard the exact formats `storage::manifest::validate`
and the sync driver depend on; a drift here silently breaks interop."""

from __future__ import annotations

import json

import pytest

from recorder import manifest as m
from recorder import storage_layout as sl


# ---------------------------------------------------------------------------
# recording-name validation (mirrors storage::validate_recording_name)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a b", "x" * 129, "a\x00b", "ré"])
def test_invalid_names_rejected(bad):
    with pytest.raises(ValueError):
        sl.validate_recording_name(bad)


@pytest.mark.parametrize("ok", ["outdoor_test_3", "rec-1.2", "A.B_c-9", "x" * 128])
def test_valid_names_accepted(ok):
    sl.validate_recording_name(ok)  # no raise


def test_recording_dir_under_recordings_root():
    d = sl.recording_dir("rec_a")
    assert d.parent == sl.recordings_dir()
    assert sl.chunks_dir("rec_a") == d / "chunks"


# ---------------------------------------------------------------------------
# canonical chunk name + sha256 (mirror Chunk::canonical_name / integrity)
# ---------------------------------------------------------------------------


def test_sha256_is_lowercase_64_hex():
    sha = sl.sha256_bytes(b"hello mcap chunk")
    assert len(sha) == sl.SHA256_HEX_LEN
    assert sha == sha.lower()
    assert all(c in "0123456789abcdef" for c in sha)


def test_canonical_chunk_name_format():
    sha = sl.sha256_bytes(b"data")
    assert sl.canonical_chunk_name(7, sha) == f"chunk-000007-{sha[:8]}.mcap"
    assert sl.canonical_chunk_name(0, sha) == f"chunk-000000-{sha[:8]}.mcap"


def test_canonical_chunk_name_rejects_bad_sha():
    with pytest.raises(ValueError):
        sl.canonical_chunk_name(0, "tooshort")


def test_object_keys():
    sha = sl.sha256_bytes(b"x")
    assert sl.object_key_chunk("m", "rec", 0, sha) == f"m/rec/chunks/chunk-000000-{sha[:8]}.mcap"
    assert sl.object_key_manifest("m", "rec") == "m/rec/manifest.json"


def test_sha256_file_matches_bytes(tmp_path):
    p = tmp_path / "blob"
    p.write_bytes(b"some bytes here")
    assert sl.sha256_file(p) == sl.sha256_bytes(b"some bytes here")


# ---------------------------------------------------------------------------
# manifest.json shape (mirrors storage::recording serde + validate)
# ---------------------------------------------------------------------------


def _sample_recording() -> m.Recording:
    sha = sl.sha256_bytes(b"chunk0")
    rec = m.Recording(
        name="rec_a",
        machine_id="jetson_alpha",
        started_at_ns=1_748_275_200_000_000_000,
        selection=m.Selection(topics=["bubbaloop/global/*/cam_*/compressed"], exclude=["**/health"]),
    )
    rec.channels.append(
        m.Channel(
            topic="bubbaloop/global/jetson_alpha/cam_front/compressed",
            channel_id=0,
            message_encoding="cbor",
            zenoh_encoding="application/cbor",
            message_count=3,
            schema_name="bubbaloop.camera.v1.CompressedImage",
            publish_time_first_ns=1,
            publish_time_last_ns=9,
        )
    )
    rec.chunks.append(m.Chunk.finalized(0, 786432, sha, 1, 9))
    return rec


def test_manifest_required_fields_present():
    d = json.loads(_sample_recording().to_json())
    assert d["schema_version"] == m.MANIFEST_SCHEMA_VERSION
    assert d["name"] == "rec_a"
    assert d["machine_id"] == "jetson_alpha"
    assert d["mode"] == "streaming"
    assert d["selection"]["topics"] == ["bubbaloop/global/*/cam_*/compressed"]
    assert d["selection"]["exclude"] == ["**/health"]


def test_manifest_omits_unset_optionals():
    d = json.loads(_sample_recording().to_json())
    # false/none optionals are omitted (Rust fills serde defaults)
    assert "include_local" not in d["selection"]
    assert "ended_at_ns" not in d
    assert "uploaded_at_ns" not in d["chunks"][0]  # pending chunk
    assert "corrupt" not in d


def test_manifest_chunk_name_is_canonical():
    d = json.loads(_sample_recording().to_json())
    chunk = d["chunks"][0]
    assert chunk["name"] == sl.canonical_chunk_name(chunk["index"], chunk["sha256"])
    assert len(chunk["sha256"]) == sl.SHA256_HEX_LEN


def test_manifest_save_atomic_roundtrip(tmp_path):
    rec = _sample_recording()
    rec.ended_at_ns = rec.started_at_ns + 100
    m.save_atomic(tmp_path, rec)
    on_disk = json.loads((tmp_path / m.MANIFEST_FILE).read_bytes())
    assert on_disk["ended_at_ns"] == rec.started_at_ns + 100
    assert not (tmp_path / (m.MANIFEST_FILE + ".tmp")).exists()  # tmp cleaned up
