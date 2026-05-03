"""Unit tests for recorder.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from recorder.config import Defaults, load_defaults, resolve_start_params


def test_load_defaults_minimal():
    d = load_defaults({})
    assert d.name == "mcap-recorder"
    assert d.topic_patterns == ()
    assert d.output_dir is None
    assert d.chunk_duration_secs == 300
    assert d.chunk_max_bytes == 1_073_741_824
    assert d.decode_timestamps is False


def test_load_defaults_full():
    d = load_defaults(
        {
            "name": "rec",
            "topic_patterns": ["a", "b"],
            "output_dir": "/data/recordings",
            "chunk_duration_secs": 60,
            "chunk_max_bytes": 1024,
            "decode_timestamps": True,
        }
    )
    assert d.name == "rec"
    assert d.topic_patterns == ("a", "b")
    assert d.output_dir == "/data/recordings"
    assert d.chunk_duration_secs == 60
    assert d.chunk_max_bytes == 1024
    assert d.decode_timestamps is True


def test_load_defaults_rejects_bad_name():
    with pytest.raises(ValueError, match="config.name"):
        load_defaults({"name": "bad name with spaces!"})


def test_load_defaults_rejects_string_for_topic_patterns():
    with pytest.raises(ValueError, match="must be a list"):
        load_defaults({"topic_patterns": "bubbaloop/**"})


def test_resolve_start_params_uses_request_over_defaults():
    d = Defaults(name="rec", topic_patterns=("default-pat",), output_dir="/tmp/d")
    p = resolve_start_params(
        {"topic_patterns": ["override"], "output_dir": "/data/sess"}, d
    )
    assert p.topic_patterns == ("override",)
    assert p.output_dir == Path("/data/sess")


def test_resolve_start_params_falls_back_to_defaults():
    d = Defaults(
        name="rec",
        topic_patterns=("default-pat",),
        output_dir="/tmp/d",
        chunk_duration_secs=42,
    )
    p = resolve_start_params({}, d)
    assert p.topic_patterns == ("default-pat",)
    assert p.output_dir == Path("/tmp/d")
    assert p.chunk_duration_secs == 42


def test_resolve_start_params_requires_topic_patterns():
    d = Defaults(name="rec", output_dir="/tmp/d")
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({}, d)


def test_resolve_start_params_requires_output_dir():
    d = Defaults(name="rec", topic_patterns=("a",))
    with pytest.raises(ValueError, match="output_dir"):
        resolve_start_params({}, d)


def test_resolve_start_params_rejects_relative_output_dir():
    d = Defaults(name="rec", topic_patterns=("a",))
    with pytest.raises(ValueError, match="absolute path"):
        resolve_start_params({"output_dir": "relative/path"}, d)


def test_resolve_start_params_rejects_traversal_in_output_dir():
    d = Defaults(name="rec", topic_patterns=("a",))
    with pytest.raises(ValueError, match=r"\.\."):
        resolve_start_params({"output_dir": "/tmp/../evil"}, d)


def test_resolve_start_params_rejects_null_byte_in_pattern():
    d = Defaults(name="rec", output_dir="/tmp/d")
    with pytest.raises(ValueError, match="invalid topic pattern"):
        resolve_start_params({"topic_patterns": ["good", "bad\x00here"]}, d)


def test_resolve_start_params_rejects_zero_chunk_duration():
    d = Defaults(name="rec", topic_patterns=("a",), output_dir="/tmp/d")
    with pytest.raises(ValueError, match="chunk_duration_secs"):
        resolve_start_params({"chunk_duration_secs": 0}, d)


def test_resolve_start_params_rejects_zero_chunk_max_bytes():
    d = Defaults(name="rec", topic_patterns=("a",), output_dir="/tmp/d")
    with pytest.raises(ValueError, match="chunk_max_bytes"):
        resolve_start_params({"chunk_max_bytes": 0}, d)
