"""Unit tests for video-embedder config validation, preprocessing, ring buffer.

Stubs out the bubbaloop_sdk so importing main.py doesn't require the SDK at
test time. Does NOT load the V-JEPA 2.1 model (which needs internet + CUDA).
"""

import os
import sys
import types

import numpy as np
import pytest
import torch

# Stub the SDK at import time so `from bubbaloop_sdk import ...` works in CI.
_sdk_stub = types.ModuleType("bubbaloop_sdk")
_sdk_stub.NodeContext = object
_sdk_stub.run_node = lambda cls: None  # no-op
sys.modules.setdefault("bubbaloop_sdk", _sdk_stub)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import FrameRing, _extract_rgba, _validate, preprocess_frame  # noqa: E402


# -------- config validation --------

def test_validate_defaults():
    cfg = _validate({"name": "video_embedder", "input_topic": "cam/raw"})
    assert cfg["model"] == "vjepa2_1_vit_base_384"
    assert cfg["clip_frames"] == 16
    assert cfg["target_hz"] == 0.5
    assert cfg["device"] == "cuda"


def test_validate_missing_name():
    with pytest.raises(ValueError, match="name"):
        _validate({"input_topic": "cam/raw"})


def test_validate_bad_name_regex():
    with pytest.raises(ValueError, match="name"):
        _validate({"name": "has space", "input_topic": "cam/raw"})


def test_validate_missing_input_topic():
    with pytest.raises(ValueError, match="input_topic"):
        _validate({"name": "video_embedder"})


def test_validate_clip_frames_bounds():
    base = {"name": "ve", "input_topic": "t"}
    with pytest.raises(ValueError, match="clip_frames"):
        _validate({**base, "clip_frames": 1})
    with pytest.raises(ValueError, match="clip_frames"):
        _validate({**base, "clip_frames": 100})


def test_validate_target_hz_bounds():
    base = {"name": "ve", "input_topic": "t"}
    with pytest.raises(ValueError, match="target_hz"):
        _validate({**base, "target_hz": 0})
    with pytest.raises(ValueError, match="target_hz"):
        _validate({**base, "target_hz": 100})


# -------- _extract_rgba --------

class _Obj:
    """Tiny object to impersonate SimpleNamespace payloads from the SDK."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_extract_rgba_oak_format():
    rgb = _Obj(data=b"\x00" * 16, width=2, height=2)
    body = _Obj(rgb=rgb)
    result = _extract_rgba(body)
    assert result == (b"\x00" * 16, 2, 2)


def test_extract_rgba_legacy_format():
    body = _Obj(data=b"\x01" * 16, width=2, height=2)
    result = _extract_rgba(body)
    assert result == (b"\x01" * 16, 2, 2)


def test_extract_rgba_unrecognized_returns_none():
    body = _Obj(some_other_field=42)
    assert _extract_rgba(body) is None


# -------- preprocess_frame --------

def test_preprocess_frame_shape_and_dtype():
    # tiny 2×2 RGBA input → should resize to 384×384 and normalize
    rgba = bytes([128, 128, 128, 255] * 4)  # 2x2, mid-gray
    tensor = preprocess_frame(rgba, 2, 2)
    assert tensor.shape == (3, 384, 384)
    assert tensor.dtype == torch.float32
    # Mid-gray (~0.5) minus mean divided by std should land near zero.
    assert tensor.abs().mean().item() < 1.0


def test_preprocess_frame_deterministic():
    rgba = bytes(np.random.default_rng(42).integers(0, 256, size=2 * 2 * 4, dtype=np.uint8))
    a = preprocess_frame(rgba, 2, 2)
    b = preprocess_frame(rgba, 2, 2)
    assert torch.allclose(a, b)


# -------- FrameRing --------

def test_frame_ring_not_full_returns_none():
    ring = FrameRing(capacity=4)
    ring.push(torch.zeros(3, 384, 384))
    ring.push(torch.zeros(3, 384, 384))
    assert ring.snapshot() is None


def test_frame_ring_full_returns_clip_tensor():
    ring = FrameRing(capacity=4)
    for _ in range(4):
        ring.push(torch.zeros(3, 384, 384))
    clip = ring.snapshot()
    assert clip is not None
    assert clip.shape == (1, 3, 4, 384, 384)


def test_frame_ring_evicts_oldest_on_overflow():
    ring = FrameRing(capacity=2)
    first = torch.full((3, 384, 384), 1.0)
    second = torch.full((3, 384, 384), 2.0)
    third = torch.full((3, 384, 384), 3.0)
    ring.push(first)
    ring.push(second)
    ring.push(third)  # evicts `first`
    clip = ring.snapshot()
    assert clip is not None
    # Expect frames [second, third] stacked along T axis.
    assert torch.all(clip[0, :, 0] == 2.0)
    assert torch.all(clip[0, :, 1] == 3.0)
