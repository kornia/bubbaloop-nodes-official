#!/usr/bin/env python3
"""mqtt-bridge node — subscribes to MQTT topics and republishes messages on Zenoh.

Run: python main.py -c configs/home-assistant.yaml [-e tcp/127.0.0.1:7447]

This bridge connects an MQTT broker (e.g. Home Assistant, Mosquitto) to Zenoh,
making IoT sensor data available to the rest of the Bubbaloop fleet.

To use real MQTT:
  1. Set mock: false in config
  2. Install paho-mqtt: pip install paho-mqtt
  3. Configure broker host/port and topics
"""

import argparse
import json
import math
import os
import re
import socket
import threading
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


def sanitize_topic(mqtt_topic: str) -> str:
    """Convert MQTT topic to a valid Zenoh topic segment (replace / with _ for sub-path)."""
    # MQTT topics use / separators — we keep them as Zenoh sub-paths
    # but sanitize any characters that aren't valid in Zenoh topics
    return re.sub(r"[^a-zA-Z0-9/_\-\.]", "_", mqtt_topic)


def main():
    parser = argparse.ArgumentParser(description="mqtt-bridge node")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config YAML")
    parser.add_argument("-e", "--endpoint", default="tcp/127.0.0.1:7447", help="Zenoh endpoint")
    args = parser.parse_args()

    cfg = load_config(args.config)
    name = cfg["name"]
    topic_prefix = cfg.get("publish_topic_prefix", f"mqtt/{name}")
    broker = cfg.get("broker", "localhost")
    port = int(cfg.get("port", 1883))
    topics = cfg.get("topics", ["#"])
    mock = bool(cfg.get("mock", True))
    mock_interval = float(cfg.get("mock_interval_secs", 2.0))

    endpoint = os.environ.get("ZENOH_ENDPOINT", args.endpoint)
    scope = os.environ.get("BUBBALOOP_SCOPE", "local")
    mid = machine_id()

    print(f"[mqtt-bridge] name={name} broker={broker}:{port} mock={mock}", flush=True)
    print(f"[mqtt-bridge] topics={topics}", flush=True)

    # Open Zenoh session in client mode — MUST be client to route through zenohd
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", f'["{endpoint}"]')
    session = zenoh.open(conf)

    health_topic = f"bubbaloop/{scope}/{mid}/{topic_prefix}/health"
    schema_key = f"bubbaloop/{scope}/{mid}/mqtt-bridge/schema"

    # Schema queryable
    # NOTE: query.key_expr is a PROPERTY not a method — NEVER use query.key_expr()
    # NOTE: NEVER use complete=True — blocks wildcard discovery
    def on_schema_query(query):
        query.reply(query.key_expr, b"")

    schema_queryable = session.declare_queryable(schema_key, on_schema_query)
    health_pub = session.declare_publisher(health_topic)

    # Publisher cache: mqtt_topic -> zenoh Publisher
    publishers: dict = {}

    def publish_to_zenoh(mqtt_topic: str, payload: bytes):
        """Forward an MQTT message to Zenoh."""
        sanitized = sanitize_topic(mqtt_topic)
        zenoh_topic = f"bubbaloop/{scope}/{mid}/{topic_prefix}/{sanitized}"
        if zenoh_topic not in publishers:
            publishers[zenoh_topic] = session.declare_publisher(zenoh_topic)
        publishers[zenoh_topic].put(payload)

    # ── Real MQTT path ────────────────────────────────────────────────────────
    # To use real MQTT, set mock: false and uncomment:
    #
    #   import paho.mqtt.client as mqtt
    #
    #   def on_connect(client, userdata, flags, rc):
    #       print(f"[mqtt-bridge] connected to {broker}:{port} rc={rc}", flush=True)
    #       for topic in topics:
    #           client.subscribe(topic)
    #
    #   def on_message(client, userdata, msg):
    #       publish_to_zenoh(msg.topic, msg.payload)
    #
    #   mqtt_client = mqtt.Client()
    #   mqtt_client.on_connect = on_connect
    #   mqtt_client.on_message = on_message
    #   mqtt_client.connect(broker, port, keepalive=60)
    #   mqtt_client.loop_start()   # background thread
    #
    # In finally block: mqtt_client.loop_stop(); mqtt_client.disconnect()
    # ─────────────────────────────────────────────────────────────────────────

    # Mock mode: simulate MQTT messages from common IoT sensors
    _mock_messages = [
        ("homeassistant/sensor/temperature/state", lambda t: {"value": round(20 + 3 * math.sin(t / 30), 2), "unit": "°C"}),
        ("homeassistant/sensor/humidity/state", lambda t: {"value": round(55 + 10 * math.sin(t / 60 + 1), 1), "unit": "%"}),
        ("homeassistant/binary_sensor/motion/state", lambda t: {"value": "on" if math.sin(t / 7) > 0.8 else "off"}),
        ("homeassistant/sensor/power/state", lambda t: {"value": round(120 + 50 * abs(math.sin(t / 20)), 1), "unit": "W"}),
    ]

    stop_event = threading.Event()
    message_count = 0

    def mock_publisher_thread():
        nonlocal message_count
        t_start = time.time()
        idx = 0
        while not stop_event.is_set():
            t = time.time() - t_start
            mqtt_topic, value_fn = _mock_messages[idx % len(_mock_messages)]
            payload = json.dumps({
                **value_fn(t),
                "topic": mqtt_topic,
                "source": "mqtt-mock",
                "timestamp": time.time(),
            }).encode()
            publish_to_zenoh(mqtt_topic, payload)
            message_count += 1
            idx += 1
            stop_event.wait(mock_interval / len(_mock_messages))

    if mock:
        mock_thread = threading.Thread(target=mock_publisher_thread, daemon=True)
        mock_thread.start()
        print(f"[mqtt-bridge] mock mode: simulating {len(_mock_messages)} MQTT topics", flush=True)
    else:
        print("[mqtt-bridge] ERROR: mock=false but real MQTT not implemented", flush=True)
        print("[mqtt-bridge] Set mock: true or add paho-mqtt integration", flush=True)

    print(f"[mqtt-bridge] running", flush=True)

    last_health = time.time()
    try:
        while True:
            time.sleep(1.0)
            now = time.time()
            if now - last_health >= 5.0:
                health = json.dumps({
                    "status": "healthy",
                    "node": name,
                    "message_count": message_count,
                    "mock": mock,
                    "timestamp": now,
                }).encode()
                health_pub.put(health)
                last_health = now

    except KeyboardInterrupt:
        print("[mqtt-bridge] shutting down", flush=True)
    finally:
        stop_event.set()
        schema_queryable.undeclare()
        session.close()


if __name__ == "__main__":
    main()
