"""Unit tests for recorder.config."""

from __future__ import annotations

import pytest

from recorder.config import (
    DEFAULT_CHUNK_DURATION_SECS,
    DEFAULT_CHUNK_MAX_BYTES,
    DEFAULT_DECODE_TIMESTAMPS,
    load_config,
    resolve_start_params,
)


# ── load_config (instance name only; recordings go to the storage root) ──────


def test_load_config_defaults_name_when_omitted():
    assert load_config({}).name == "mcap-recorder"


def test_load_config_accepts_explicit_name():
    assert load_config({"name": "rec/garage"}).name == "rec/garage"


def test_load_config_rejects_bad_name():
    with pytest.raises(ValueError, match="config.name"):
        load_config({"name": "bad name with spaces!"})


def test_load_config_ignores_legacy_output_dir():
    # output_dir is obsolete (recordings always land under the storage root);
    # passing it must not error.
    assert load_config({"name": "rec", "output_dir": "/whatever"}).name == "rec"


# ── resolve_start_params ───────────────────────────────────────────


def _ok_request(**overrides):
    base = {"topic_patterns": ["bubbaloop/global/**"]}
    base.update(overrides)
    return base


def test_minimal_request_uses_code_defaults_and_generates_name():
    p = resolve_start_params(_ok_request())
    assert p.topic_patterns == ("bubbaloop/global/**",)
    assert p.exclude == ()
    assert p.chunk_duration_secs == DEFAULT_CHUNK_DURATION_SECS
    assert p.chunk_max_bytes == DEFAULT_CHUNK_MAX_BYTES
    assert p.decode_timestamps == DEFAULT_DECODE_TIMESTAMPS
    # generated name is non-empty and storage-valid (rec_<timestamp>)
    assert p.name.startswith("rec_")


def test_full_request_overrides_all_defaults():
    p = resolve_start_params(
        _ok_request(
            name="garage_run_1",
            exclude=["**/health"],
            chunk_duration_secs=60,
            chunk_max_bytes=1024,
            decode_timestamps=True,
        )
    )
    assert p.name == "garage_run_1"
    assert p.exclude == ("**/health",)
    assert p.chunk_duration_secs == 60
    assert p.chunk_max_bytes == 1024
    assert p.decode_timestamps is True


def test_requires_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({})


def test_rejects_string_for_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"topic_patterns": "not-a-list"})


def test_rejects_empty_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"topic_patterns": []})


def test_rejects_null_byte_in_pattern():
    with pytest.raises(ValueError, match="invalid topic pattern"):
        resolve_start_params(_ok_request(topic_patterns=["good", "bad\x00here"]))


def test_rejects_invalid_recording_name():
    # name flows through the same path-traversal guard as the Rust layer.
    with pytest.raises(ValueError):
        resolve_start_params(_ok_request(name="../escape"))


def test_rejects_non_list_exclude():
    with pytest.raises(ValueError, match="exclude"):
        resolve_start_params(_ok_request(exclude="**/health"))


def test_rejects_zero_chunk_duration():
    with pytest.raises(ValueError, match="chunk_duration_secs"):
        resolve_start_params(_ok_request(chunk_duration_secs=0))


def test_rejects_zero_chunk_max_bytes():
    with pytest.raises(ValueError, match="chunk_max_bytes"):
        resolve_start_params(_ok_request(chunk_max_bytes=0))
