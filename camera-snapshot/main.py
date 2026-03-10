#!/usr/bin/env python3
"""camera-snapshot node — polls HTTP/MJPEG camera URL and publishes JPEG frames over Zenoh.

Run: python main.py -c configs/entrance.yaml [-e tcp/127.0.0.1:7447]
"""

import argparse
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

import yaml
import zenoh

# Minimal valid 1x1 white JPEG (mock frames when hardware unavailable)
_MOCK_JPEG = bytes([
    0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46, 0x49, 0x46, 0x00, 0x01,
    0x01, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
    0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08, 0x07, 0x07, 0x07, 0x09,
    0x09, 0x08, 0x0A, 0x0C, 0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
    0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D, 0x1A, 0x1C, 0x1C, 0x20,
    0x24, 0x2E, 0x27, 0x20, 0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
    0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27, 0x39, 0x3D, 0x38, 0x32,
    0x3C, 0x2E, 0x33, 0x34, 0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x01,
    0x00, 0x01, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4, 0x00, 0x1F, 0x00, 0x00,
    0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08,
    0x09, 0x0A, 0x0B, 0xFF, 0xC4, 0x00, 0xB5, 0x10, 0x00, 0x02, 0x01, 0x03,
    0x03, 0x02, 0x04, 0x03, 0x05, 0x05, 0x04, 0x04, 0x00, 0x00, 0x01, 0x7D,
    0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F, 0x00, 0xFB, 0x26,
    0x4A, 0xFF, 0xD9,
])


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def fetch_snapshot(url: str, timeout: float = 5.0) -> bytes:
    """Fetch a JPEG snapshot from an HTTP URL."""
    req = urllib.request.Request(url, headers={"User-Agent": "bubbaloop-camera-snapshot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def machine_id() -> str:
    mid = os.environ.get("BUBBALOOP_MACHINE_ID", "")
    if not mid:
        mid = socket.gethostname().replace("-", "_")
    return mid


def main():
    parser = argparse.ArgumentParser(description="camera-snapshot node")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("-e", "--endpoint", default="tcp/127.0.0.1:7447", help="Zenoh endpoint")
    args = parser.parse_args()

    cfg = load_config(args.config)
    name = cfg["name"]
    publish_topic = cfg.get("publish_topic", f"camera/{name}/snapshot")
    url = cfg.get("url", "")
    interval = float(cfg.get("interval_secs", 1.0))
    mock = bool(cfg.get("mock", True))

    endpoint = os.environ.get("ZENOH_ENDPOINT", args.endpoint)
    scope = os.environ.get("BUBBALOOP_SCOPE", "local")
    mid = machine_id()

    print(f"[camera-snapshot] name={name} topic={publish_topic} mock={mock}", flush=True)
    print(f"[camera-snapshot] scope={scope} machine_id={mid}", flush=True)

    # Open Zenoh session in client mode — MUST be client to route through zenohd
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", f'["{endpoint}"]')
    session = zenoh.open(conf)

    full_topic = f"bubbaloop/{scope}/{mid}/{publish_topic}"
    health_topic = f"bubbaloop/{scope}/{mid}/{publish_topic}/health"
    schema_key = f"bubbaloop/{scope}/{mid}/camera-snapshot/schema"

    # Schema queryable — reply with empty bytes (no protobuf schema for raw JPEG)
    # NOTE: query.key_expr is a PROPERTY not a method — NEVER use query.key_expr()
    # NOTE: NEVER use complete=True — blocks wildcard discovery like bubbaloop/**/schema
    def on_schema_query(query):
        query.reply(query.key_expr, b"")

    schema_queryable = session.declare_queryable(schema_key, on_schema_query)
    print(f"[camera-snapshot] schema queryable: {schema_key}", flush=True)

    publisher = session.declare_publisher(full_topic)
    health_pub = session.declare_publisher(health_topic)

    last_health = time.time()
    frame_count = 0

    print(f"[camera-snapshot] publishing to {full_topic}", flush=True)

    try:
        while True:
            loop_start = time.time()

            # Capture frame
            if mock:
                jpeg_bytes = _MOCK_JPEG
            else:
                try:
                    jpeg_bytes = fetch_snapshot(url)
                except (urllib.error.URLError, OSError) as e:
                    print(f"[camera-snapshot] fetch error: {e}", flush=True)
                    time.sleep(interval)
                    continue

            publisher.put(jpeg_bytes)
            frame_count += 1

            # Health heartbeat every 5 seconds
            now = time.time()
            if now - last_health >= 5.0:
                health = json.dumps({
                    "status": "healthy",
                    "node": name,
                    "frame_count": frame_count,
                    "mock": mock,
                    "timestamp": now,
                }).encode()
                health_pub.put(health)
                last_health = now

            elapsed = time.time() - loop_start
            sleep_for = max(0.0, interval - elapsed)
            time.sleep(sleep_for)

    except KeyboardInterrupt:
        print("[camera-snapshot] shutting down", flush=True)
    finally:
        schema_queryable.undeclare()
        session.close()


if __name__ == "__main__":
    main()
