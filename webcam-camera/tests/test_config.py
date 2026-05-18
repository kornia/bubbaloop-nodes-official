"""Unit tests for webcam-camera config validation.

These run without a real webcam — cv2 is stubbed out at the module level so
CI doesn't require OpenCV headers or a V4L2 device.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Stub hardware/SDK modules so the import doesn't fail in CI.
for _mod in ("cv2", "cbor2", "bubbaloop_sdk"):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))

from main import _validate


def test_validate_defaults():
    cfg = _validate({"name": "webcam_primary"})
    assert cfg["name"] == "webcam_primary"
    assert cfg["device"] == 0
    assert cfg["width"] == 1280
    assert cfg["height"] == 720
    assert cfg["fps"] == 30.0
    assert cfg["jpeg_quality"] == 80
    assert cfg["warn_on_param_mismatch"] is True


def test_validate_missing_name():
    with pytest.raises(ValueError, match="name"):
        _validate({})


def test_validate_bad_name_regex():
    with pytest.raises(ValueError, match="name"):
        _validate({"name": "my webcam"})  # space is not allowed


def test_validate_name_with_hyphen_and_dot():
    cfg = _validate({"name": "webcam-primary.0"})
    assert cfg["name"] == "webcam-primary.0"


def test_validate_device_int():
    cfg = _validate({"name": "w", "device": 2})
    assert cfg["device"] == 2


def test_validate_device_string_path():
    cfg = _validate({"name": "w", "device": "/dev/video0"})
    assert cfg["device"] == "/dev/video0"


def test_validate_device_path_traversal():
    with pytest.raises(ValueError, match="'\\.\\.'"):
        _validate({"name": "w", "device": "../../etc/passwd"})


def test_validate_device_negative_int():
    with pytest.raises(ValueError, match=">= 0"):
        _validate({"name": "w", "device": -1})


def test_validate_width_not_multiple_of_16():
    with pytest.raises(ValueError, match="multiple of 16"):
        _validate({"name": "w", "width": 1281})


def test_validate_width_out_of_range():
    with pytest.raises(ValueError, match="multiple of 16"):
        _validate({"name": "w", "width": 8})


def test_validate_height_not_multiple_of_16():
    with pytest.raises(ValueError, match="multiple of 16"):
        _validate({"name": "w", "height": 719})


def test_validate_fps_below_min():
    with pytest.raises(ValueError, match="fps"):
        _validate({"name": "w", "fps": 0})


def test_validate_fps_above_max():
    with pytest.raises(ValueError, match="fps"):
        _validate({"name": "w", "fps": 121})


def test_validate_jpeg_quality_below_min():
    with pytest.raises(ValueError, match="jpeg_quality"):
        _validate({"name": "w", "jpeg_quality": 0})


def test_validate_jpeg_quality_above_max():
    with pytest.raises(ValueError, match="jpeg_quality"):
        _validate({"name": "w", "jpeg_quality": 101})


def test_validate_unknown_keys_ignored():
    # serde-style: extra keys are tolerated for forward-compat
    cfg = _validate({"name": "w", "future_field": "x"})
    assert cfg["name"] == "w"
