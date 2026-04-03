#!/usr/bin/env python3
"""network-monitor — Network connectivity monitor (HTTP, DNS, ping).

Runs configured health checks and publishes results as JSON.
"""

import logging
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

log = logging.getLogger("network-monitor")

TOPIC_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


# ------------------------------------------------------------------
# Health checks
# ------------------------------------------------------------------

def check_http(name: str, target: str, timeout: float) -> dict:
    try:
        r = requests.get(target, timeout=timeout)
        return {"name": name, "type": "http", "target": target,
                "statusName": "OK", "latencyMs": r.elapsed.total_seconds() * 1000,
                "statusCode": r.status_code}
    except requests.Timeout:
        return {"name": name, "type": "http", "target": target, "statusName": "TIMEOUT"}
    except Exception as e:
        return {"name": name, "type": "http", "target": target, "statusName": "FAILED", "error": str(e)}


def check_dns(name: str, target: str, timeout: float) -> dict:
    try:
        t0 = time.monotonic()
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(target)
        return {"name": name, "type": "dns", "target": target,
                "statusName": "OK", "latencyMs": (time.monotonic() - t0) * 1000}
    except socket.timeout:
        return {"name": name, "type": "dns", "target": target, "statusName": "TIMEOUT"}
    except Exception as e:
        return {"name": name, "type": "dns", "target": target, "statusName": "FAILED", "error": str(e)}


def check_ping(name: str, target: str, timeout: float) -> dict:
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout)), target],
            capture_output=True, text=True, timeout=timeout + 1,
        )
        if result.returncode == 0:
            latency = None
            for part in result.stdout.split():
                if part.startswith("time="):
                    try:
                        latency = float(part.split("=")[1])
                    except ValueError:
                        pass
            return {"name": name, "type": "ping", "target": target,
                    "statusName": "OK", "latencyMs": latency}
        return {"name": name, "type": "ping", "target": target, "statusName": "FAILED"}
    except subprocess.TimeoutExpired:
        return {"name": name, "type": "ping", "target": target, "statusName": "TIMEOUT"}
    except FileNotFoundError:
        return {"name": name, "type": "ping", "target": target,
                "statusName": "FAILED", "error": "ping not found"}


# ------------------------------------------------------------------
# Node
# ------------------------------------------------------------------

class NetworkMonitorNode:
    name = "network-monitor"

    def __init__(self, ctx, config: dict):
        self.ctx = ctx
        topic = config.get("publish_topic", "network-monitor/status")
        if not TOPIC_RE.match(topic):
            raise ValueError(f"Invalid publish_topic: {topic!r}")

        self.checks = config.get("checks", [])
        self.interval = 1.0 / max(config.get("rate_hz", 0.1), 1e-6)
        self.timeout = config.get("timeout_secs", 5)
        self.sequence = 0

        self.pub = ctx.publisher_json(topic)
        log.info("Publishing to: %s (%.2f Hz)", ctx.topic(topic), config.get("rate_hz", 0.1))

    def _run_checks(self) -> list[dict]:
        results = []
        for entry in self.checks:
            name = entry.get("name", "")
            kind = entry.get("type", "http").lower()
            target = entry.get("target", "")
            if kind == "http":
                results.append(check_http(name, target, self.timeout))
            elif kind == "dns":
                results.append(check_dns(name, target, self.timeout))
            elif kind == "ping":
                results.append(check_ping(name, target, self.timeout))
            else:
                log.warning("Unknown check type: %s", kind)
        return results

    def run(self):
        log.info("Running %d checks every %.0fs", len(self.checks), self.interval)
        while not self.ctx.is_shutdown():
            checks = self._run_checks()
            healthy = sum(1 for c in checks if c.get("statusName") == "OK")
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence": self.sequence,
                "machine_id": self.ctx.machine_id,
                "scope": self.ctx.scope,
                "summary": {"total": len(checks), "healthy": healthy, "unhealthy": len(checks) - healthy},
                "checks": checks,
            }
            self.pub.put(payload)
            log.info("seq=%d: %d/%d healthy", self.sequence, healthy, len(checks))
            self.sequence += 1
            self.ctx._shutdown.wait(timeout=self.interval)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node
    run_node(NetworkMonitorNode)
