#!/usr/bin/env python3
"""network-monitor node - Network connectivity monitor (HTTP, DNS, ping health checks)"""

import argparse
import json
import logging
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import re

import requests
import yaml
import zenoh

# Topic name validation pattern
TOPIC_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Import generated protobuf modules
# Run `python build_proto.py` first to generate these
try:
    import header_pb2
    import network_monitor_pb2
except ImportError:
    logger.error(
        "Protobuf modules not found. Run 'python build_proto.py' or 'pixi run build-proto' first."
    )
    sys.exit(1)


class NetworkMonitorNode:
    """Network connectivity monitor with HTTP, DNS, and ping checks."""

    def __init__(self, config_path: Path, endpoint: str | None = None):
        # Load configuration
        if config_path.exists():
            with open(config_path) as f:
                self.config = yaml.safe_load(f)
        else:
            logger.warning(f"Config file not found: {config_path}, using defaults")
            self.config = {
                "publish_topic": "network-monitor/status",
                "rate_hz": 0.1,
                "timeout_secs": 5,
                "checks": [],
            }

        # Validate config
        topic = self.config.get("publish_topic", "")
        if not TOPIC_RE.match(topic):
            raise ValueError(
                f"publish_topic '{topic}' contains invalid characters "
                f"(must match [a-zA-Z0-9/_\\-\\.]+)"
            )
        rate_hz = self.config.get("rate_hz", 0.1)
        if not (0.01 <= rate_hz <= 1000.0):
            raise ValueError(f"rate_hz {rate_hz} out of range (0.01-1000.0)")
        timeout_secs = self.config.get("timeout_secs", 5)
        if not (1 <= timeout_secs <= 300):
            raise ValueError(f"timeout_secs {timeout_secs} out of range (1-300)")

        # Resolve scope and machine_id from env vars
        import os
        self.scope = os.environ.get("BUBBALOOP_SCOPE", "local")
        self.machine_id = os.environ.get(
            "BUBBALOOP_MACHINE_ID", socket.gethostname()
        )

        # Setup zenoh
        zenoh_config = zenoh.Config()
        if endpoint:
            zenoh_config.insert_json5("connect/endpoints", json.dumps([endpoint]))

        self.session = zenoh.open(zenoh_config)
        logger.info("Connected to zenoh")

        # Build scoped topic: bubbaloop/{scope}/{machine_id}/{publish_topic}
        topic_suffix = self.config["publish_topic"]
        self.full_topic = f"bubbaloop/{self.scope}/{self.machine_id}/{topic_suffix}"

        # Setup publishers
        self.publisher = self.session.declare_publisher(self.full_topic)
        logger.info(f"Publishing to: {self.full_topic}")

        self.health_publisher = self.session.declare_publisher(
            f"bubbaloop/{self.scope}/{self.machine_id}/health/network-monitor"
        )

        # Declare schema queryable so dashboard/tools can discover this node's protobuf schemas
        descriptor_path = Path(__file__).parent / "descriptor.bin"
        if descriptor_path.exists():
            self.descriptor_bytes = descriptor_path.read_bytes()
            schema_key = f"bubbaloop/{self.scope}/{self.machine_id}/network-monitor/schema"
            self.schema_queryable = self.session.declare_queryable(
                schema_key,
                lambda query: query.reply(query.key_expr, self.descriptor_bytes),
            )
            logger.info(f"Schema queryable: {schema_key}")
        else:
            self.descriptor_bytes = None
            self.schema_queryable = None
            logger.warning("descriptor.bin not found, schema queryable not available")

        self.hostname = socket.gethostname()
        self.running = True
        self.sequence = 0

    def _check_http(self, target: str) -> network_monitor_pb2.HealthCheck:
        """Perform an HTTP health check."""
        check = network_monitor_pb2.HealthCheck()
        check.type = network_monitor_pb2.CHECK_TYPE_HTTP
        check.target = target
        timeout = self.config.get("timeout_secs", 5)

        try:
            start = time.monotonic()
            resp = requests.get(target, timeout=timeout, allow_redirects=True)
            elapsed = (time.monotonic() - start) * 1000
            check.latency_ms = elapsed
            check.status_code = resp.status_code
            if resp.ok:
                check.status = network_monitor_pb2.CHECK_STATUS_OK
            else:
                check.status = network_monitor_pb2.CHECK_STATUS_FAILED
                check.error = f"HTTP {resp.status_code}"
        except requests.Timeout:
            check.status = network_monitor_pb2.CHECK_STATUS_TIMEOUT
            check.error = "Request timed out"
        except requests.RequestException as e:
            check.status = network_monitor_pb2.CHECK_STATUS_FAILED
            check.error = str(e)

        return check

    def _check_dns(self, target: str) -> network_monitor_pb2.HealthCheck:
        """Perform a DNS resolution check."""
        check = network_monitor_pb2.HealthCheck()
        check.type = network_monitor_pb2.CHECK_TYPE_DNS
        check.target = target
        timeout = self.config.get("timeout_secs", 5)

        try:
            socket.setdefaulttimeout(timeout)
            start = time.monotonic()
            ip = socket.gethostbyname(target)
            elapsed = (time.monotonic() - start) * 1000
            check.latency_ms = elapsed
            check.resolved = ip
            check.status = network_monitor_pb2.CHECK_STATUS_OK
        except socket.timeout:
            check.status = network_monitor_pb2.CHECK_STATUS_TIMEOUT
            check.error = "DNS resolution timed out"
        except socket.gaierror as e:
            check.status = network_monitor_pb2.CHECK_STATUS_FAILED
            check.error = str(e)

        return check

    def _check_ping(self, target: str) -> network_monitor_pb2.HealthCheck:
        """Perform an ICMP ping check."""
        check = network_monitor_pb2.HealthCheck()
        check.type = network_monitor_pb2.CHECK_TYPE_PING
        check.target = target
        timeout = self.config.get("timeout_secs", 5)

        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), target],
                capture_output=True,
                text=True,
                timeout=timeout + 1,
            )
            if result.returncode == 0:
                check.status = network_monitor_pb2.CHECK_STATUS_OK
                # Parse latency from ping output (e.g., "time=1.23 ms")
                for line in result.stdout.split("\n"):
                    if "time=" in line:
                        try:
                            time_part = line.split("time=")[1].split()[0]
                            check.latency_ms = float(time_part)
                        except (IndexError, ValueError):
                            pass
                        break
            else:
                check.status = network_monitor_pb2.CHECK_STATUS_FAILED
                check.error = "Ping failed"
        except subprocess.TimeoutExpired:
            check.status = network_monitor_pb2.CHECK_STATUS_TIMEOUT
            check.error = "Ping timed out"
        except FileNotFoundError:
            check.status = network_monitor_pb2.CHECK_STATUS_FAILED
            check.error = "ping command not found"

        return check

    def process(self) -> bytes:
        """Run all configured checks and return serialized NetworkStatus."""
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

        checks = []
        for entry in self.config.get("checks", []):
            name = entry.get("name", "")
            check_type = entry.get("type", "http").lower()
            target = entry.get("target", "")

            if check_type == "http":
                check = self._check_http(target)
            elif check_type == "dns":
                check = self._check_dns(target)
            elif check_type == "ping":
                check = self._check_ping(target)
            else:
                logger.warning(f"Unknown check type: {check_type}")
                continue

            check.name = name
            checks.append(check)

        # Build summary
        healthy = sum(
            1
            for c in checks
            if c.status == network_monitor_pb2.CHECK_STATUS_OK
        )
        unhealthy = len(checks) - healthy

        pub_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)

        status = network_monitor_pb2.NetworkStatus()
        status.header.CopyFrom(
            header_pb2.Header(
                acq_time=now_ns,
                pub_time=pub_ns,
                sequence=self.sequence,
                frame_id="network-monitor",
                machine_id=self.machine_id,
                scope=self.scope,
            )
        )
        status.checks.extend(checks)
        status.summary.CopyFrom(
            network_monitor_pb2.Summary(
                total=len(checks),
                healthy=healthy,
                unhealthy=unhealthy,
            )
        )

        return status.SerializeToString()

    def run(self):
        """Run the node main loop."""
        interval = 1.0 / self.config.get("rate_hz", 0.1)
        logger.info(
            f"network-monitor node started "
            f"(rate: {self.config.get('rate_hz', 0.1)} Hz, "
            f"checks: {len(self.config.get('checks', []))})"
        )

        while self.running:
            output = self.process()
            self.publisher.put(output)

            # Health heartbeat
            self.health_publisher.put(b"ok")

            if self.sequence % 5 == 0:
                logger.debug(f"Published status seq={self.sequence}")

            self.sequence += 1
            time.sleep(interval)

        logger.info("network-monitor node stopped")

    def stop(self):
        """Stop the node."""
        self.running = False

    def close(self):
        """Clean up resources."""
        self.publisher.undeclare()
        self.health_publisher.undeclare()
        if self.schema_queryable is not None:
            self.schema_queryable.undeclare()
        self.session.close()


def main():
    parser = argparse.ArgumentParser(
        description="Network connectivity monitor (HTTP, DNS, ping health checks)"
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to configuration file",
    )
    parser.add_argument(
        "-e",
        "--endpoint",
        type=str,
        default="tcp/127.0.0.1:7447",
        help="Zenoh endpoint to connect to (default: tcp/127.0.0.1:7447)",
    )
    args = parser.parse_args()

    node = NetworkMonitorNode(args.config, args.endpoint)

    # Setup signal handlers
    def signal_handler(signum, frame):
        logger.info("Shutdown signal received")
        node.stop()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        node.run()
    finally:
        node.close()


if __name__ == "__main__":
    main()
