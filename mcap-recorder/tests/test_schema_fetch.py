"""Tests for the protobuf schema-fetch helpers (§3.3.4).

Only the pure topic→instance parsing and the best-effort contract are unit-tested
here; the live Zenoh query is covered by daemon integration tests.
"""

from __future__ import annotations

from recorder.schema_fetch import (
    fetch_descriptor,
    schema_topic,
    source_instance_from_topic,
)


def test_source_instance_from_global_topic():
    assert (
        source_instance_from_topic("bubbaloop/global/jetson1/tapo_terrace/compressed")
        == "tapo_terrace"
    )


def test_source_instance_from_local_topic():
    assert source_instance_from_topic("bubbaloop/local/m/cam_front/frames") == "cam_front"


def test_source_instance_rejects_non_bubbaloop_or_short_topics():
    for bad in ["", "foo/bar", "bubbaloop/global/m", "ros2/topic/x", "bubbaloop/weird/m/i/s"]:
        assert source_instance_from_topic(bad) is None


def test_schema_topic_format():
    assert schema_topic("m", "cam") == "bubbaloop/global/m/cam/schema"


def test_fetch_descriptor_is_best_effort_on_error():
    class _BadSession:
        def get(self, *a, **k):
            raise RuntimeError("zenoh down")

    # never raises — returns None so recording continues without a schema
    assert fetch_descriptor(_BadSession(), "m", "cam") is None


def test_fetch_descriptor_returns_first_ok_payload():
    class _Reply:
        def __init__(self, payload):
            self.ok = type("Ok", (), {"payload": payload})()

    class _Session:
        def get(self, key, timeout=None):
            return [_Reply(b"\x0a\x02fd")]

    assert fetch_descriptor(_Session(), "m", "cam") == b"\x0a\x02fd"
