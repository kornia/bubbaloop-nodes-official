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


def _ok_config(**overrides):
    base = {"name": "mcap-recorder", "output_dir": "/data/recordings"}
    base.update(overrides)
    return base


def test_load_config_defaults_name_when_omitted():
    cfg = load_config({"output_dir": "/data/recordings"})
    assert cfg.name == "mcap-recorder"


def test_load_config_accepts_explicit_name_and_output_dir():
    cfg = load_config(_ok_config(name="rec/garage", output_dir="/mnt/data"))
    assert cfg.name == "rec/garage"
    assert cfg.output_dir == Path("/mnt/data")


def test_load_config_rejects_bad_name():
    with pytest.raises(ValueError, match="config.name"):
        load_config(_ok_config(name="bad name with spaces!"))


def test_load_config_requires_output_dir():
    with pytest.raises(ValueError, match="output_dir is required"):
        load_config({"name": "rec"})


def test_load_config_rejects_relative_output_dir():
    with pytest.raises(ValueError, match="absolute path"):
        load_config(_ok_config(output_dir="relative/path"))


def test_load_config_rejects_traversal_in_output_dir():
    with pytest.raises(ValueError, match=r"\.\."):
        load_config(_ok_config(output_dir="/tmp/../evil"))


def test_load_config_expands_tilde_in_output_dir(monkeypatch):
    monkeypatch.setenv("HOME", "/home/test-user")
    cfg = load_config(_ok_config(output_dir="~/.bubbaloop/recordings"))
    assert cfg.output_dir == Path("/home/test-user/.bubbaloop/recordings")


def test_load_config_expands_tilde_user_in_output_dir(monkeypatch):
    """`~user` form also expands."""
    # `~/path` expands via $HOME; `~someuser` expands via getpwnam.
    # We only assert the simpler $HOME form here — the more complex form
    # is exercised by Path.expanduser itself, not our code.
    monkeypatch.setenv("HOME", "/home/x")
    cfg = load_config(_ok_config(output_dir="~/recordings"))
    assert cfg.output_dir == Path("/home/x/recordings")


# ── resolve_start_params ───────────────────────────────────────────


def _ok_request(**overrides):
    base = {"topic_patterns": ["bubbaloop/global/**"]}
    base.update(overrides)
    return base


def test_resolve_start_params_minimal_request_uses_code_defaults():
    p = resolve_start_params(_ok_request())
    assert p.topic_patterns == ("bubbaloop/global/**",)
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
        resolve_start_params({})


def test_resolve_start_params_rejects_string_for_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"topic_patterns": "not-a-list"})


def test_resolve_start_params_rejects_empty_topic_patterns():
    with pytest.raises(ValueError, match="topic_patterns"):
        resolve_start_params({"topic_patterns": []})


def test_resolve_start_params_rejects_null_byte_in_pattern():
    with pytest.raises(ValueError, match="invalid topic pattern"):
        resolve_start_params(_ok_request(topic_patterns=["good", "bad\x00here"]))


def test_resolve_start_params_rejects_zero_chunk_duration():
    with pytest.raises(ValueError, match="chunk_duration_secs"):
        resolve_start_params(_ok_request(chunk_duration_secs=0))


def test_resolve_start_params_rejects_zero_chunk_max_bytes():
    with pytest.raises(ValueError, match="chunk_max_bytes"):
        resolve_start_params(_ok_request(chunk_max_bytes=0))


def test_resolve_start_params_does_not_accept_output_dir():
    """`output_dir` belongs in config.yaml — silently ignored from commands."""
    p = resolve_start_params(_ok_request(output_dir="/data/should-be-ignored"))
    # StartParams has no output_dir field at all
    assert not hasattr(p, "output_dir")
