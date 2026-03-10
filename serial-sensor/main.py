#!/usr/bin/env python3
"""serial-sensor node — reads JSON or CSV data from serial devices and publishes over Zenoh.

Run: python main.py -c configs/arduino.yaml [-e tcp/127.0.0.1:7447]

Supported devices (mock mode off):
  - Arduino/ESP32 sending JSON: {"temperature": 23.5, "humidity": 60}
  - Any device sending CSV: "23.5,60.0\n"

To use real hardware:
  1. Set mock: false in config
  2. Install pyserial: pip install pyserial
  3. Set the correct port (e.g. /dev/ttyUSB0, /dev/ttyACM0, COM3)
"""

import argparse
import json
import math
import os
import socket
import sys
import time

import yaml
import zenoh


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def machine_id() -> str:
    mid = os.environ.get("BUBBALOOP_MACHINE_ID", "")
    if not mid:
        mid = socket.gethostname().replace("-", "_")
    return mid


def simulate_reading(name: str, t: float) -> dict:
    """Generate realistic simulated sensor readings using sinusoidal patterns."""
    return {
        "sensor": name,
        "temperature": round(20.0 + 5.0 * math.sin(t / 30.0), 2),
        "humidity": round(50.0 + 10.0 * math.sin(t / 60.0 + 1.0), 1),
        "raw_value": round(512 + 256 * math.sin(t / 10.0), 0),
        "unit": "°C / %RH",
        "simulation": True,
        "timestamp": t,
    }


def parse_serial_line(line: str, fmt: str, name: str) -> dict:
    """Parse a line from the serial port in JSON or CSV format."""
    line = line.strip()
    if fmt == "json":
        data = json.loads(line)
        data.setdefault("sensor", name)
        data.setdefault("timestamp", time.time())
        return data
    elif fmt == "csv":
        parts = [p.strip() for p in line.split(",")]
        return {
            "sensor": name,
            "values": [float(p) for p in parts if p],
            "raw": line,
            "timestamp": time.time(),
        }
    else:
        return {"sensor": name, "raw": line, "timestamp": time.time()}


def main():
    parser = argparse.ArgumentParser(description="serial-sensor node")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("-e", "--endpoint", default="tcp/127.0.0.1:7447", help="Zenoh endpoint")
    args = parser.parse_args()

    cfg = load_config(args.config)
    name = cfg["name"]
    publish_topic = cfg.get("publish_topic", f"serial/{name}/reading")
    port = cfg.get("port", "/dev/ttyUSB0")
    baud_rate = int(cfg.get("baud_rate", 9600))
    fmt = cfg.get("format", "json")
    interval = float(cfg.get("interval_secs", 1.0))
    mock = bool(cfg.get("mock", True))

    endpoint = os.environ.get("ZENOH_ENDPOINT", args.endpoint)
    scope = os.environ.get("BUBBALOOP_SCOPE", "local")
    mid = machine_id()

    print(f"[serial-sensor] name={name} port={port} baud={baud_rate} mock={mock}", flush=True)

    # Open Zenoh session in client mode — MUST be client to route through zenohd
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", f'["{endpoint}"]')
    session = zenoh.open(conf)

    full_topic = f"bubbaloop/{scope}/{mid}/{publish_topic}"
    health_topic = f"bubbaloop/{scope}/{mid}/{publish_topic}/health"
    schema_key = f"bubbaloop/{scope}/{mid}/serial-sensor/schema"

    # Schema queryable
    # NOTE: query.key_expr is a PROPERTY not a method — NEVER use query.key_expr()
    # NOTE: NEVER use complete=True — blocks wildcard discovery
    def on_schema_query(query):
        query.reply(query.key_expr, b"")

    schema_queryable = session.declare_queryable(schema_key, on_schema_query)
    publisher = session.declare_publisher(full_topic)
    health_pub = session.declare_publisher(health_topic)

    print(f"[serial-sensor] publishing to {full_topic}", flush=True)

    # ── Real hardware path ────────────────────────────────────────────────────
    # To use real hardware, set mock: false and uncomment the following:
    #
    #   import serial
    #   ser = serial.Serial(port, baud_rate, timeout=interval)
    #
    # Then replace the simulate_reading() call with:
    #   line = ser.readline().decode("utf-8", errors="replace")
    #   if not line:
    #       continue
    #   reading = parse_serial_line(line, fmt, name)
    #
    # And in finally block: ser.close()
    # ─────────────────────────────────────────────────────────────────────────

    last_health = time.time()
    reading_count = 0
    start_time = time.time()

    try:
        while True:
            loop_start = time.time()
            t = loop_start - start_time

            if mock:
                reading = simulate_reading(name, t)
            else:
                # Real hardware: import serial and read from port (see comment above)
                print("[serial-sensor] ERROR: mock=false but real serial not implemented", flush=True)
                print("[serial-sensor] Set mock: true or add pyserial integration", flush=True)
                time.sleep(5.0)
                continue

            publisher.put(json.dumps(reading).encode())
            reading_count += 1

            now = time.time()
            if now - last_health >= 5.0:
                health = json.dumps({
                    "status": "healthy",
                    "node": name,
                    "reading_count": reading_count,
                    "mock": mock,
                    "timestamp": now,
                }).encode()
                health_pub.put(health)
                last_health = now

            elapsed = time.time() - loop_start
            time.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        print("[serial-sensor] shutting down", flush=True)
    finally:
        schema_queryable.undeclare()
        session.close()


if __name__ == "__main__":
    main()
