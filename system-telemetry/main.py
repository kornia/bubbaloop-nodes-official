#!/usr/bin/env python3
"""system-telemetry — System metrics publisher (CPU, memory, disk, network, load).

Collects system metrics via psutil and publishes them as JSON.
"""

import logging
import re
import time
from datetime import datetime, timezone

import psutil

log = logging.getLogger("system-telemetry")

TOPIC_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


# ------------------------------------------------------------------
# Metric collectors
# ------------------------------------------------------------------

def collect_cpu() -> dict:
    usage = psutil.cpu_percent(interval=None, percpu=True)
    return {
        "usage_percent": sum(usage) / len(usage) if usage else 0.0,
        "per_core": usage,
        "count": psutil.cpu_count(logical=True),
    }


def collect_memory() -> dict:
    m = psutil.virtual_memory()
    return {
        "total_bytes": m.total,
        "used_bytes": m.used,
        "available_bytes": m.available,
        "usage_percent": m.percent,
    }


def collect_disk() -> dict:
    usage = psutil.disk_usage("/")
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "available_bytes": usage.free,
        "usage_percent": usage.percent,
    }


def collect_network(prev: dict | None) -> dict:
    counters = psutil.net_io_counters()
    now = time.monotonic()
    result = {
        "bytes_sent": counters.bytes_sent,
        "bytes_recv": counters.bytes_recv,
    }
    result["_ts"] = now
    result["_sent"] = counters.bytes_sent
    result["_recv"] = counters.bytes_recv
    return result


def collect_load() -> dict:
    avg = psutil.getloadavg()
    return {"one_min": avg[0], "five_min": avg[1], "fifteen_min": avg[2]}


# ------------------------------------------------------------------
# Node
# ------------------------------------------------------------------

class SystemTelemetryNode:
    name = "system-telemetry"

    def __init__(self, ctx, config: dict):
        self.ctx = ctx
        topic = config.get("publish_topic", "system-telemetry/metrics")
        if not TOPIC_RE.match(topic):
            raise ValueError(f"Invalid publish_topic: {topic!r}")

        self.rate_hz = float(config.get("rate_hz", 1.0))
        if not (0.001 <= self.rate_hz <= 100.0):
            raise ValueError(f"rate_hz {self.rate_hz} out of range (0.001–100)")

        collect = config.get("collect", {})
        self.do_cpu = collect.get("cpu", True)
        self.do_memory = collect.get("memory", True)
        self.do_disk = collect.get("disk", True)
        self.do_network = collect.get("network", True)
        self.do_load = collect.get("load", True)

        self.pub = ctx.publisher_json(topic)
        self._prev_net = None
        self._seq = 0

        log.info("Publishing to %s at %.2f Hz", ctx.topic(topic), self.rate_hz)

        # Warm up CPU percent (first call always returns 0)
        psutil.cpu_percent(interval=None)

    def run(self):
        interval = 1.0 / self.rate_hz
        while not self.ctx.is_shutdown():
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence": self._seq,
                "machine_id": self.ctx.machine_id,
                "scope": "",
            }
            if self.do_cpu:
                payload["cpu"] = collect_cpu()
            if self.do_memory:
                payload["memory"] = collect_memory()
            if self.do_disk:
                payload["disk"] = collect_disk()
            if self.do_network:
                net = collect_network(self._prev_net)
                self._prev_net = net
                payload["network"] = {"bytes_sent": net["bytes_sent"], "bytes_recv": net["bytes_recv"]}
            if self.do_load:
                payload["load"] = collect_load()

            self.pub.put(payload)

            if self._seq % 10 == 0:
                cpu_pct = payload.get("cpu", {}).get("usage_percent", 0)
                mem_pct = payload.get("memory", {}).get("usage_percent", 0)
                log.info("seq=%d cpu=%.1f%% mem=%.1f%%", self._seq, cpu_pct, mem_pct)

            self._seq += 1
            self.ctx._shutdown.wait(timeout=interval)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node
    run_node(SystemTelemetryNode)
