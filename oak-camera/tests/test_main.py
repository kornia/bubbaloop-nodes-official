"""Unit tests for oak-camera config validation and wire-format helpers."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# main.py imports depthai/cv2 at module scope for the runtime pipeline. The
# pure functions we actually test don't touch those libs, so stub them out
# when they aren't installed (CI image doesn't carry hardware deps).
for _mod in ("depthai", "cv2", "cbor2"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

from main import _rgbd_body, _validate


def test_validate_defaults():
    cfg = _validate({"name": "oak_primary"})
    assert cfg["name"] == "oak_primary"
    assert cfg["width"] == 1280
    assert cfg["height"] == 720
    assert cfg["fps"] == 30.0
    assert cfg["jpeg_every_n"] == 3
    assert cfg["jpeg_quality"] == 80
    assert cfg["enable_depth"] is True


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


def test_rgbd_body_rgb_only():
    body = _rgbd_body(b"\x00\x00\x00\x00", 1, 1, "oak_primary", "host1", 42)
    # Inner capture header.
    inner = body["header"]
    assert inner["sequence"] == 42
    assert inner["frame_id"] == "oak_primary"
    assert inner["machine_id"] == "host1"
    assert {"acq_time", "pub_time"} <= inner.keys()
    # RGB plane.
    assert body["rgb"] == {
        "width": 1,
        "height": 1,
        "encoding": "rgba8",
        "step": 4,
        "data": b"\x00\x00\x00\x00",
    }
    # Depth is absent (not None) so consumers can use `"depth" in body`.
    assert "depth" not in body


def test_rgbd_body_with_depth():
    depth_bytes = b"\x10\x27" * 4  # four uint16 pixels = 10000 mm each
    body = _rgbd_body(
        b"\x00" * 16, 2, 2, "oak_primary", "host1", 99,
        depth=depth_bytes, depth_width=2, depth_height=2,
    )
    assert body["rgb"]["encoding"] == "rgba8"
    assert body["rgb"]["width"] == 2
    assert body["rgb"]["step"] == 8
    assert body["depth"] == {
        "width": 2,
        "height": 2,
        "encoding": "depth16_mm",
        "step": 4,  # 2 pixels * 2 bytes
        "data": depth_bytes,
    }
