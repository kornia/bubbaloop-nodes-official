"""Unit tests for config validation and detection payload builder."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from main import build_payload, load_config


# --- Config validation ---

def test_load_config_valid(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace\n"
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: rf-detr-detector/tapo_terrace/detections\n"
        "confidence_threshold: 0.5\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg["name"] == "tapo_terrace"
    assert cfg["subscribe_topic"] == "camera/tapo_terrace/compressed"
    assert cfg["publish_topic"] == "rf-detr-detector/tapo_terrace/detections"
    assert cfg["confidence_threshold"] == 0.5


def test_load_config_missing_name(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: rf-detr-detector/tapo_terrace/detections\n"
    )
    with pytest.raises(ValueError, match="name"):
        load_config(str(cfg_file))


def test_load_config_invalid_topic(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace\n"
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: bad topic with spaces\n"
        "confidence_threshold: 0.5\n"
    )
    with pytest.raises(ValueError, match="publish_topic"):
        load_config(str(cfg_file))


def test_load_config_threshold_bounds(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace\n"
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: rf-detr-detector/tapo_terrace/detections\n"
        "confidence_threshold: 1.5\n"
    )
    with pytest.raises(ValueError, match="confidence_threshold"):
        load_config(str(cfg_file))


# --- Payload builder ---

def test_build_payload_with_detections():
    detections = [
        {
            "class_id": 0,
            "class_name": "person",
            "confidence": 0.91,
            "bbox": {"x1": 10, "y1": 20, "x2": 100, "y2": 200},
        }
    ]
    payload = build_payload(
        frame_id="tapo_terrace",
        machine_id="nvidia_orin00",
        scope="local",
        sequence=42,
        detections=detections,
    )
    assert payload["frame_id"] == "tapo_terrace"
    assert payload["machine_id"] == "nvidia_orin00"
    assert payload["scope"] == "local"
    assert payload["sequence"] == 42
    assert len(payload["detections"]) == 1
    assert payload["detections"][0]["class_name"] == "person"
    assert "timestamp" in payload


def test_build_payload_empty_detections():
    payload = build_payload(
        frame_id="tapo_terrace",
        machine_id="nvidia_orin00",
        scope="local",
        sequence=0,
        detections=[],
    )
    assert payload["detections"] == []
