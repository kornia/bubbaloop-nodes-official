"""Best-effort protobuf schema fetch (spec §3.3.4).

For protobuf topics the recorder enriches the MCAP by registering the publishing
node's ``FileDescriptorSet`` as the channel schema, so downstream MCAP tooling
(Foxglove, the `mcap` CLI) can decode messages without out-of-band `.proto`
files. Every SDK node serves its descriptor at ``{instance}/schema`` (a Zenoh
queryable). We derive the source instance from the topic, query that key once
per topic, and cache the result.

This is strictly best-effort: a missing/late schema never blocks or fails
recording — the raw bytes plus the §4.5 `zenoh.encoding` metadata are always
enough for `storage replay`. The topic→instance parsing is pure and unit-tested;
the Zenoh fetch is exercised in daemon integration tests.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 2.0


def source_instance_from_topic(topic: str) -> Optional[str]:
    """Extract the publishing instance from a scoped topic, or ``None``.

    Topics are ``bubbaloop/{global|local}/{machine}/{instance}/{suffix...}``;
    the schema queryable lives at ``{instance}/schema``.
    """
    parts = topic.split("/")
    if len(parts) >= 5 and parts[0] == "bubbaloop" and parts[1] in ("global", "local"):
        instance = parts[3]
        return instance or None
    return None


def schema_topic(machine_id: str, instance: str) -> str:
    """The Zenoh key serving `instance`'s FileDescriptorSet."""
    return f"bubbaloop/global/{machine_id}/{instance}/schema"


def fetch_descriptor(
    zenoh_session,
    machine_id: str,
    instance: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Optional[bytes]:
    """Query `{instance}/schema` and return the FileDescriptorSet bytes, or
    ``None`` on any failure/timeout. Never raises — best-effort by contract."""
    key = schema_topic(machine_id, instance)
    try:
        replies = zenoh_session.get(key, timeout=timeout_s)
        for reply in replies:
            ok = getattr(reply, "ok", None)
            if ok is not None and ok.payload is not None:
                data = bytes(ok.payload)
                if data:
                    return data
    except Exception as exc:  # pragma: no cover - integration path
        log.debug("schema fetch for %s failed: %s", key, exc)
    return None
