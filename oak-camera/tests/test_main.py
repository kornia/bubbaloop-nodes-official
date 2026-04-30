"""Unit tests for oak-camera config validation and wire-format helpers."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# main.py imports depthai/kornia_rs/PIL at module scope for the runtime
# pipeline. The pure functions we actually test don't touch those libs, so
# stub them out when they aren't installed (CI image doesn't carry hardware
# deps). PIL needs the submodule stubbed too so `from PIL import Image` works.
for _mod in ("depthai", "kornia_rs", "cbor2"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
if "PIL" not in sys.modules:
    sys.modules["PIL"] = types.ModuleType("PIL")
    sys.modules["PIL.Image"] = types.ModuleType("PIL.Image")
    sys.modules["PIL"].Image = sys.modules["PIL.Image"]

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
    # Default sync threshold = half-frame interval, e.g. 16ms at 30fps.
    assert cfg["sync_threshold_ms"] == 16
    # -1 = drop unsynced frames (strictest pairing).
    assert cfg["sync_attempts"] == -1
    assert cfg["calibration_publish_interval_secs"] == 1.0


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


def test_rgbd_body_no_calibration_field():
    """Calibration travels on its own topic — body never carries it."""
    body = _rgbd_body(b"\x00" * 4, 1, 1, "oak_primary", "host1", 1)
    assert "calibration" not in body


def test_rgbd_body_acq_time_passthrough():
    """When acq_time_ns is provided (synced path), the body uses it as acq_time
    instead of falling back to wall-clock."""
    body = _rgbd_body(
        b"\x00" * 4, 1, 1, "oak_primary", "host1", 7,
        acq_time_ns=1_700_000_000_123_456_789,
        sync_interval_ns=4_500_000,  # 4.5 ms RGB↔depth gap
    )
    assert body["header"]["acq_time"] == 1_700_000_000_123_456_789
    assert body["header"]["sync_interval_ns"] == 4_500_000
    # pub_time is independent (wall-clock at body-build time)
    assert body["header"]["pub_time"] != body["header"]["acq_time"]


def test_rgbd_body_no_acq_time_falls_back_to_wallclock():
    """No acq_time_ns provided (unsynced path) → acq_time == pub_time."""
    body = _rgbd_body(b"\x00" * 4, 1, 1, "oak", "h1", 0)
    # Both timestamps stamped in the same call — equal within ~1us.
    assert body["header"]["acq_time"] == body["header"]["pub_time"]
    assert "sync_interval_ns" not in body["header"]


def test_validate_depth_png_compression_default():
    cfg = _validate({"name": "oak"})
    assert cfg["depth_png_compression"] == 1


def test_validate_depth_png_compression_out_of_range():
    with pytest.raises(ValueError, match="depth_png_compression"):
        _validate({"name": "oak", "depth_png_compression": 10})
    with pytest.raises(ValueError, match="depth_png_compression"):
        _validate({"name": "oak", "depth_png_compression": -1})


def test_validate_sync_threshold_default_scales_with_fps():
    """At 60 fps the default should be ~8ms (half-interval); at 10 fps, ~50ms."""
    cfg_60 = _validate({"name": "oak", "fps": 60})
    assert cfg_60["sync_threshold_ms"] == 8
    cfg_10 = _validate({"name": "oak", "fps": 10})
    assert cfg_10["sync_threshold_ms"] == 50


def test_validate_sync_threshold_override():
    cfg = _validate({"name": "oak", "sync_threshold_ms": 33})
    assert cfg["sync_threshold_ms"] == 33


def test_validate_sync_threshold_out_of_range():
    with pytest.raises(ValueError, match="sync_threshold_ms"):
        _validate({"name": "oak", "sync_threshold_ms": 0})
    with pytest.raises(ValueError, match="sync_threshold_ms"):
        _validate({"name": "oak", "sync_threshold_ms": 1001})


def test_validate_sync_attempts_out_of_range():
    with pytest.raises(ValueError, match="sync_attempts"):
        _validate({"name": "oak", "sync_attempts": -2})


def test_validate_calibration_publish_interval_out_of_range():
    with pytest.raises(ValueError, match="calibration_publish_interval"):
        _validate({"name": "oak", "calibration_publish_interval_secs": 0.05})
    with pytest.raises(ValueError, match="calibration_publish_interval"):
        _validate({"name": "oak", "calibration_publish_interval_secs": 100})
