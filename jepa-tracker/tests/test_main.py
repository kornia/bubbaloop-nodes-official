"""Unit tests for jepa-tracker (no model load, no GPU)."""

import os
import sys
import types

import numpy as np
import pytest
import torch

# Stub the SDK so importing main.py doesn't need it.
_sdk_stub = types.ModuleType("bubbaloop_sdk")
_sdk_stub.NodeContext = object
_sdk_stub.run_node = lambda cls: None
sys.modules.setdefault("bubbaloop_sdk", _sdk_stub)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import (  # noqa: E402
    TrackStore,
    _extract_rgba,
    _validate,
    build_blob_descriptors,
    feature_aware_cc,
    hsv_palette,
    preprocess_frame,
    temporal_variance_map,
    token_flow_argmax,
)


# -------- _validate --------

def test_validate_defaults():
    cfg = _validate({"name": "t", "input_topic": "x/raw"})
    assert cfg["clip_frames"] == 16
    assert cfg["precision"] == "fp16"
    assert cfg["publish_overlay"] is True
    assert cfg["sim_threshold"] == 0.6


def test_validate_missing_input_topic():
    with pytest.raises(ValueError, match="input_topic"):
        _validate({"name": "t"})


def test_validate_bad_precision():
    with pytest.raises(ValueError, match="precision"):
        _validate({"name": "t", "input_topic": "x/raw", "precision": "bf16"})


# -------- _extract_rgba --------

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_extract_rgba_oak_format():
    rgb = _Obj(data=b"\x00" * 16, width=2, height=2)
    assert _extract_rgba(_Obj(rgb=rgb)) == (b"\x00" * 16, 2, 2)


def test_extract_rgba_legacy():
    body = _Obj(data=b"\x01" * 16, width=2, height=2)
    assert _extract_rgba(body) == (b"\x01" * 16, 2, 2)


# -------- preprocess_frame --------

def test_preprocess_returns_aligned_raw_and_norm():
    rgba = bytes([128, 128, 128, 255] * 4)  # 2x2 mid-gray
    raw, norm = preprocess_frame(rgba, 2, 2)
    assert raw.shape == (3, 384, 384)
    assert raw.dtype == torch.uint8
    assert norm.shape == (3, 384, 384)
    assert norm.dtype == torch.float32


# -------- temporal_variance_map --------

def test_variance_static_clip_is_zero():
    # Same features at every timestep → std = 0 → norm = 0
    tokens = torch.zeros(8, 4, 4, 16)
    var = temporal_variance_map(tokens)
    assert torch.allclose(var, torch.zeros(4, 4))


def test_variance_picks_up_changing_token():
    tokens = torch.zeros(8, 4, 4, 16)
    # Make the (1, 1) token oscillate
    tokens[:, 1, 1] = torch.linspace(-1, 1, 8).view(8, 1).expand(8, 16)
    var = temporal_variance_map(tokens)
    # The oscillating token should have the highest variance
    assert var[1, 1] > var[0, 0]


# -------- feature_aware_cc --------

def test_cc_two_disjoint_blobs():
    H, W, D = 4, 4, 8
    feats = torch.zeros(H, W, D)
    # Blob A in (0,0)-(0,1): "A"-like vector
    feats[0, 0] = torch.tensor([1.0, 0, 0, 0, 0, 0, 0, 0])
    feats[0, 1] = torch.tensor([1.0, 0, 0, 0, 0, 0, 0, 0])
    # Blob B in (3,3): different vector
    feats[3, 3] = torch.tensor([0, 1.0, 0, 0, 0, 0, 0, 0])
    mask = np.zeros((H, W), dtype=bool)
    mask[0, 0] = mask[0, 1] = mask[3, 3] = True
    labels = feature_aware_cc(feats, mask, sim_threshold=0.5)
    assert labels[0, 0] == labels[0, 1]
    assert labels[0, 0] != labels[3, 3]
    assert labels[3, 3] != 0


def test_cc_separates_dissimilar_neighbors():
    """Two adjacent moving tokens with different features should be different blobs."""
    H, W, D = 1, 2, 4
    feats = torch.zeros(H, W, D)
    feats[0, 0] = torch.tensor([1.0, 0, 0, 0])
    feats[0, 1] = torch.tensor([0, 1.0, 0, 0])
    mask = np.ones((H, W), dtype=bool)
    labels = feature_aware_cc(feats, mask, sim_threshold=0.5)
    assert labels[0, 0] != labels[0, 1]


# -------- build_blob_descriptors --------

def test_descriptors_filter_small_blobs():
    H, W, D = 4, 4, 8
    tokens = torch.zeros(2, H, W, D)
    labels = np.zeros((H, W), dtype=np.int32)
    labels[0, 0] = 1   # tiny blob
    labels[1:3, 1:3] = 2
    descriptors = build_blob_descriptors(tokens, labels, flow=None, min_blob_tokens=2)
    # The 1-token blob is filtered, the 4-token blob survives.
    assert len(descriptors) == 1
    assert descriptors[0]["area"] == 4


# -------- token_flow_argmax --------

def test_flow_zero_when_static():
    tokens_t = torch.randn(4, 4, 8)
    flow = token_flow_argmax(tokens_t, tokens_t.clone(), window=3)
    assert torch.allclose(flow, torch.zeros(4, 4, 2))


def test_flow_nonzero_when_token_moves():
    tokens_t = torch.zeros(4, 4, 8)
    tokens_tp1 = torch.zeros(4, 4, 8)
    feature = torch.randn(8)
    feature = feature / feature.norm()
    tokens_t[1, 1] = feature
    tokens_tp1[2, 2] = feature  # same feature appears at (2,2)
    flow = token_flow_argmax(tokens_t, tokens_tp1, window=3)
    assert flow[1, 1, 0] == 1   # moved down by 1
    assert flow[1, 1, 1] == 1   # moved right by 1


# -------- hsv_palette --------

def test_palette_shape_and_dtype():
    p = hsv_palette(5)
    assert p.shape == (5, 3)
    assert p.dtype == torch.uint8


# -------- TrackStore --------

def _blob(sig_vec):
    sig = torch.tensor(sig_vec, dtype=torch.float32)
    sig = sig / sig.norm()
    return {"signature": sig}


def test_tracker_assigns_new_ids_first_clip():
    ts = TrackStore(sig_match_threshold=0.5, max_age=2)
    ids = ts.step([_blob([1, 0, 0]), _blob([0, 1, 0])])
    assert sorted(ids) == [1, 2]


def test_tracker_preserves_id_on_match():
    ts = TrackStore(sig_match_threshold=0.5, max_age=2)
    ids1 = ts.step([_blob([1, 0, 0])])
    ids2 = ts.step([_blob([0.95, 0.05, 0])])  # very similar
    assert ids2 == ids1


def test_tracker_assigns_new_id_on_low_similarity():
    ts = TrackStore(sig_match_threshold=0.95, max_age=2)
    ids1 = ts.step([_blob([1, 0, 0])])
    ids2 = ts.step([_blob([0.6, 0.8, 0])])  # similar but not above 0.95
    assert ids2 != ids1


def test_tracker_drops_track_after_max_age():
    ts = TrackStore(sig_match_threshold=0.5, max_age=1)
    ts.step([_blob([1, 0, 0])])
    ts.step([_blob([0, 1, 0])])    # different blob; original ages by 1
    ts.step([_blob([0, 0, 1])])    # original now > max_age, should be gone
    # The first signature is no longer in the tracker; reusing it spawns a new id
    ids = ts.step([_blob([1, 0, 0])])
    assert ids[0] not in (1,)
