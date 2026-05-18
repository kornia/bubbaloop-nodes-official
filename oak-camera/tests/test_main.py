"""Unit tests for oak-camera config validation and wire-format helpers."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# main.py imports depthai/cv2/bubbaloop_sdk at module scope for the runtime pipeline.
# Stub them out when they aren't installed (CI image doesn't carry hardware deps).
for _mod in ("depthai", "cv2", "cbor2"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

from main import _ns_to_iso8601, _rgbd_body, _validate


# ── _validate ─────────────────────────────────────────────────────────────────

def test_validate_defaults():
    cfg = _validate({"name": "oak_primary"})
    assert cfg["name"] == "oak_primary"
    assert cfg["width"] == 1280
    assert cfg["height"] == 720
    assert cfg["fps"] == 30.0
    assert cfg["jpeg_every_n"] == 3
    assert cfg["jpeg_quality"] == 80
    assert cfg["enable_depth"] is True
    # new fields
    assert cfg["enable_imu"] is False
    assert cfg["imu_hz"] == 50
    assert cfg["enable_rgbd_compressed"] is False
    assert cfg["enable_grab_frame"] is True


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


def test_validate_imu_hz_out_of_range():
    with pytest.raises(ValueError, match="imu_hz"):
        _validate({"name": "oak", "imu_hz": 0})
    with pytest.raises(ValueError, match="imu_hz"):
        _validate({"name": "oak", "imu_hz": 501})


def test_validate_imu_flags():
    cfg = _validate({"name": "oak", "enable_imu": True, "imu_hz": 200})
    assert cfg["enable_imu"] is True
    assert cfg["imu_hz"] == 200


def test_validate_rgbd_compressed_flag():
    cfg = _validate({"name": "oak", "enable_rgbd_compressed": True})
    assert cfg["enable_rgbd_compressed"] is True


def test_validate_grab_frame_disabled():
    cfg = _validate({"name": "oak", "enable_grab_frame": False})
    assert cfg["enable_grab_frame"] is False


# ── _rgbd_body ────────────────────────────────────────────────────────────────

def test_rgbd_body_rgb_only():
    body = _rgbd_body(b"\x00\x00\x00\x00", 1, 1, "oak_primary", "host1", 42)
    inner = body["header"]
    assert inner["sequence"] == 42
    assert inner["frame_id"] == "oak_primary"
    assert inner["machine_id"] == "host1"
    assert {"acq_time", "pub_time"} <= inner.keys()
    assert body["rgb"] == {
        "width": 1, "height": 1, "encoding": "rgba8", "step": 4,
        "data": b"\x00\x00\x00\x00",
    }
    # Depth is absent (not None) so consumers can use `"depth" in body`.
    assert "depth" not in body


def test_rgbd_body_device_timestamp():
    # acq_time_ns from the device clock is preserved; pub_time is host-clock.
    device_ts = 1_234_567_890_000
    body = _rgbd_body(b"\x00\x00\x00\x00", 1, 1, "oak", "host1", 0, acq_time_ns=device_ts)
    assert body["header"]["acq_time"] == device_ts
    # pub_time is a recent host-clock timestamp (much larger than device_ts which
    # is near epoch; just check it's a positive integer)
    assert body["header"]["pub_time"] > 0


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
        "width": 2, "height": 2, "encoding": "depth16_mm", "step": 4,
        "data": depth_bytes,
    }


# ── _ns_to_iso8601 ────────────────────────────────────────────────────────────

def test_ns_to_iso8601_known_value():
    # 1_000_000_000 ns = Unix epoch + 1s → 1970-01-01T00:00:01+00:00
    iso = _ns_to_iso8601(1_000_000_000)
    assert iso.startswith("1970-01-01T00:00:01")
    assert "+00:00" in iso or "Z" in iso or iso.endswith("UTC")


def test_ns_to_iso8601_non_negative():
    iso = _ns_to_iso8601(0)
    assert "1970" in iso
