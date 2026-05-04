"""Unit tests for recorder.commands.parse_envelope."""

from __future__ import annotations

import json

from recorder.commands import parse_envelope


def test_flat_envelope_passes_through():
    env, err = parse_envelope(
        json.dumps({"command": "start_recording", "output_dir": "/data"}).encode()
    )
    assert err is None
    assert env == {"command": "start_recording", "output_dir": "/data"}


def test_nested_envelope_is_flattened():
    env, err = parse_envelope(
        json.dumps(
            {"command": "start_recording", "params": {"output_dir": "/data", "n": 3}}
        ).encode()
    )
    assert err is None
    assert env == {"command": "start_recording", "output_dir": "/data", "n": 3}


def test_empty_payload_returns_E_EMPTY():
    env, err = parse_envelope(b"")
    assert env is None
    assert err is not None and err.code == "E_EMPTY"


def test_invalid_json_returns_E_BAD_JSON():
    env, err = parse_envelope(b"{not json")
    assert env is None
    assert err is not None and err.code == "E_BAD_JSON"


def test_non_object_returns_E_BAD_SHAPE():
    env, err = parse_envelope(b'["array", "not", "object"]')
    assert env is None
    assert err is not None and err.code == "E_BAD_SHAPE"


def test_nested_with_no_params_treated_as_flat():
    """`params` key absent or non-dict means we use the envelope as-is."""
    env, err = parse_envelope(json.dumps({"command": "stop_recording"}).encode())
    assert err is None
    assert env == {"command": "stop_recording"}


def test_nested_params_loses_top_level_only_to_command():
    """When flattening, top-level keys other than `command` are discarded
    (the daemon is the source of truth via `params`)."""
    raw = json.dumps(
        {
            "command": "start_recording",
            "extra_top_level": "ignored",
            "params": {"output_dir": "/data"},
        }
    ).encode()
    env, err = parse_envelope(raw)
    assert err is None
    assert env == {"command": "start_recording", "output_dir": "/data"}
    assert "extra_top_level" not in env
