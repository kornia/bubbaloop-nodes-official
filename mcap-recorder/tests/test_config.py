"""Unit tests for recorder.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from recorder.config import (
    DEFAULT_CHUNK_DURATION_SECS,
    DEFAULT_CHUNK_MAX_BYTES,
    DEFAULT_DECODE_TIMESTAMPS,
    load_config,
    resolve_start_params,
)


# ── load_config ────────────────────────────────────────────────────


def test_load_config_uses_default_name():
    cfg = load_config({})
    assert cfg.name == "mcap-recorder"


def test_load_config_accepts_explicit_name():
    cfg = load_config({"name": "rec/garage"})
    assert cfg.name == "rec/garage"


def test_load_config_rejects_bad_name():
    with pytest.raises(ValueError, match="config.name"):
        load_config({"name": "bad name with spaces!"})


# ── resolve_start_params ───────────────────────────────────────────


def _ok_request(**overrides):
    base = {
        "topic_patterns": ["bubbaloop/global/**"],
        "output_dir": "/data/sess",
    }
    base.update(overrides)
    return base


def test_resolve_start_params_minimal_request_uses_code_defaults():
    p = resolve_start_params(_ok_request())
    assert p.topic_patterns == ("bubbaloop/global/**",)
    assert p.output_dir == Path("/data/sess")
    assert p.chunk_duration_secs == DEFAULT_CHUNK_DURATION_SECS
    assert p.chunk_max_bytes == DEFAULT_CHUNK_MAX_BYTES
    assert p.decode_timestamps == DEFAULT_DECODE_TIMESTAMPS


def test_resolve_start_params_full_request_overrides_all_defaults():
    p = resolve_start_params(
        _ok_request(
            chunk_duration_secs=60,
            chunk_max_bytes=1024,
            decode_timestamps=True,
        )
    )
    assert p.chunk_duration_secs == 60
    assert p.chunk_max_bytes == 1024
    assert p.decode_timestamps is True


def test_resolve_start_params_requires_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"output_dir": "/data"})


def test_resolve_start_params_rejects_string_for_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"topic_patterns": "not-a-list", "output_dir": "/data"})


def test_resolve_start_params_rejects_empty_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"topic_patterns": [], "output_dir": "/data"})


def test_resolve_start_params_rejects_null_byte_in_pattern():
    with pytest.raises(ValueError, match="invalid topic pattern"):
        resolve_start_params(_ok_request(topic_patterns=["good", "bad\x00here"]))


def test_resolve_start_params_requires_output_dir():
    with pytest.raises(ValueError, match="output_dir"):
        resolve_start_params({"topic_patterns": ["a"]})


def test_resolve_start_params_rejects_relative_output_dir():
    with pytest.raises(ValueError, match="absolute path"):
        resolve_start_params(_ok_request(output_dir="relative/path"))


def test_resolve_start_params_rejects_traversal_in_output_dir():
    with pytest.raises(ValueError, match=r"\.\."):
        resolve_start_params(_ok_request(output_dir="/tmp/../evil"))


def test_resolve_start_params_rejects_zero_chunk_duration():
    with pytest.raises(ValueError, match="chunk_duration_secs"):
        resolve_start_params(_ok_request(chunk_duration_secs=0))


def test_resolve_start_params_rejects_zero_chunk_max_bytes():
    with pytest.raises(ValueError, match="chunk_max_bytes"):
        resolve_start_params(_ok_request(chunk_max_bytes=0))
