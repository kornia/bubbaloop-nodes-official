"""Unit tests for config validation and payload builder."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import build_payload, load_config


# --- Config validation ---

def test_load_config_valid(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace_vlm\n"
        "model: google/gemma-4-E2B-it\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg["name"] == "tapo_terrace_vlm"
    assert cfg["model"] == "google/gemma-4-E2B-it"


def test_load_config_missing_name(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("model: google/gemma-4-E2B-it\n")
    with pytest.raises(ValueError, match="name"):
        load_config(str(cfg_file))


def test_load_config_defaults(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("name: test_vlm\n")
    cfg = load_config(str(cfg_file))
    assert cfg["target_fps"] == 0.1
    assert cfg["device"] == "cuda"
    assert cfg["model"] == "Qwen/Qwen2.5-VL-3B-Instruct"
    assert cfg["max_tokens"] == 128
    assert cfg["prompt"] == "Describe this scene in one or two sentences."


def test_load_config_invalid_device(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("name: test_vlm\ndevice: tpu\n")
    with pytest.raises(ValueError, match="device"):
        load_config(str(cfg_file))


def test_load_config_target_fps_bounds(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("name: test_vlm\ntarget_fps: 5.0\n")
    with pytest.raises(ValueError, match="target_fps"):
        load_config(str(cfg_file))


def test_load_config_max_tokens_bounds(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("name: test_vlm\nmax_tokens: 1000\n")
    with pytest.raises(ValueError, match="max_tokens"):
        load_config(str(cfg_file))


# --- Payload builder ---

def test_build_payload():
    payload = build_payload(
        frame_id="tapo_terrace",
        machine_id="nvidia_orin00",
        sequence=1,
        description="A terrace with outdoor furniture.",
        inference_ms=5432.1,
    )
    assert payload["frame_id"] == "tapo_terrace"
    assert payload["description"] == "A terrace with outdoor furniture."
    assert payload["inference_ms"] == 5432.1
    assert payload["sequence"] == 1
    assert "timestamp" in payload


def test_build_payload_empty_description():
    payload = build_payload(
        frame_id="test",
        machine_id="test",
        sequence=0,
        description="",
        inference_ms=0.0,
    )
    assert payload["description"] == ""
