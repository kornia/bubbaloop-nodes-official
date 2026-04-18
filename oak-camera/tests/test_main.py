"""Unit tests for oak-camera config validation and wire-format helpers."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Imports from main.py work without the OAK device because _validate,
# _envelope, and _raw_body are pure functions.
from main import _envelope, _raw_body, _validate


def test_validate_defaults():
    cfg = _validate({"name": "oak_primary"})
    assert cfg["name"] == "oak_primary"
    assert cfg["width"] == 1280
    assert cfg["height"] == 720
    assert cfg["fps"] == 30.0
    assert cfg["jpeg_every_n"] == 3
    assert cfg["jpeg_quality"] == 80
    assert cfg["enable_depth"] is True
    assert cfg["max_depth_mm"] == 10000


def test_validate_missing_name():
    with pytest.raises(ValueError, match="name"):
        _validate({})


def test_validate_bad_name_regex():
    with pytest.raises(ValueError, match="name"):
        _validate({"name": "oak primary"})  # space not allowed


def test_validate_width_not_multiple_of_16():
    with pytest.raises(ValueError, match="multiples of 16"):
        _validate({"name": "oak", "width": 1281})


def test_validate_fps_out_of_range():
    with pytest.raises(ValueError, match="fps"):
        _validate({"name": "oak", "fps": 90})


def test_validate_jpeg_every_n_below_min():
    with pytest.raises(ValueError, match="jpeg_every_n"):
        _validate({"name": "oak", "jpeg_every_n": 0})


def test_validate_jpeg_every_n_above_max():
    with pytest.raises(ValueError, match="jpeg_every_n"):
        _validate({"name": "oak", "jpeg_every_n": 61})


def test_validate_jpeg_quality_out_of_range():
    with pytest.raises(ValueError, match="jpeg_quality"):
        _validate({"name": "oak", "jpeg_quality": 200})


def test_validate_max_depth_mm_non_positive():
    with pytest.raises(ValueError, match="max_depth_mm"):
        _validate({"name": "oak", "max_depth_mm": 0})


def test_envelope_shape():
    env = _envelope({"width": 1, "height": 2}, "oak_primary", "raw", 7)
    assert env["header"]["source_instance"] == "oak_primary"
    assert env["header"]["monotonic_seq"] == 7
    assert env["header"]["schema_uri"].startswith("bubbaloop://oak_primary/raw")
    assert env["body"] == {"width": 1, "height": 2}


def test_raw_body_matches_rtsp_camera_shape():
    body = _raw_body(b"\x00\x00\x00\x00", 1, 1, "oak_primary", "host1", 42)
    assert body["width"] == 1
    assert body["height"] == 1
    assert body["encoding"] == "rgba8"
    assert body["step"] == 4
    assert body["data"] == b"\x00\x00\x00\x00"
    # inner HeaderCbor fields (matches rtsp-camera/src/cbor_wire.rs::HeaderCbor)
    inner = body["header"]
    assert inner["sequence"] == 42
    assert inner["frame_id"] == "oak_primary"
    assert inner["machine_id"] == "host1"
    assert {"acq_time", "pub_time"} <= inner.keys()
