# RF-DETR Detector Node Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python processor node `rf-detr-detector` that subscribes to camera H264 frames, decodes via GStreamer, runs RF-DETR inference on CPU, and publishes JSON detections.

**Architecture:** A single `main.py` contains three focused classes: `H264Decoder` (GStreamer pipeline), `Detector` (RF-DETR wrapper), and `RfDetrDetectorNode` (bubbaloop node wiring). The node follows the multi-instance pattern — one process per camera, configured via YAML.

**Tech Stack:** Python 3.11, GStreamer 1.20 (system), PyGObject (conda-forge), rfdetr 1.6.3, torch (CPU), bubbaloop-sdk (main), eclipse-zenoh

---

## File Map

| File | Responsibility |
|---|---|
| `rf-detr-detector/main.py` | `H264Decoder`, `Detector`, `RfDetrDetectorNode`, `__main__` |
| `rf-detr-detector/node.yaml` | Manifest: processor capability, subscribes/publishes |
| `rf-detr-detector/pixi.toml` | Python + PyGObject + torch-cpu + rfdetr + bubbaloop-sdk |
| `rf-detr-detector/configs/tapo_terrace.yaml` | Instance config for terrace camera |
| `rf-detr-detector/configs/tapo_entrance.yaml` | Instance config for entrance camera |
| `rf-detr-detector/tests/test_main.py` | Unit tests for config validation and payload builder |

---

### Task 1: Scaffold directory and dependencies

**Files:**
- Create: `rf-detr-detector/pixi.toml`
- Create: `rf-detr-detector/node.yaml`
- Create: `rf-detr-detector/configs/tapo_terrace.yaml`
- Create: `rf-detr-detector/configs/tapo_entrance.yaml`

- [ ] **Step 1: Create `rf-detr-detector/pixi.toml`**

```toml
[workspace]
name = "rf-detr-detector"
channels = ["conda-forge"]
platforms = ["linux-64", "linux-aarch64"]

[tasks]
main = "python main.py"

[dependencies]
python = ">=3.11"
pip = "*"
pygobject = ">=3.44"

[pypi-dependencies]
eclipse-zenoh = ">=1.0"
pyyaml = ">=6.0"
torch = { version = ">=2.0", index = "https://pypi.org/simple/" }
torchvision = { version = ">=0.15", index = "https://pypi.org/simple/" }
rfdetr = ">=1.6.3"
pillow = ">=10.0"
numpy = ">=1.24"
bubbaloop-sdk = { git = "https://github.com/kornia/bubbaloop.git", branch = "main", subdirectory = "python-sdk" }
```

- [ ] **Step 2: Create `rf-detr-detector/node.yaml`**

```yaml
name: rf-detr-detector
version: 0.1.0
type: python
description: Object detection using RF-DETR on camera H264 streams
author: Bubbaloop Team

command: pixi run main

capabilities:
  - processor

subscribes:
  - suffix: camera/{name}/compressed
    description: H264 compressed frames from an rtsp-camera node
    encoding: application/protobuf

publishes:
  - suffix: rf-detr-detector/{name}/detections
    description: RF-DETR object detections with bounding boxes and confidence scores
    encoding: application/json
    rate_hz: 10.0

requires:
  hardware: []
  software:
    - GStreamer 1.20+ with gst-plugins-ugly (avdec_h264)
    - Python 3.11+
```

- [ ] **Step 3: Create `rf-detr-detector/configs/tapo_terrace.yaml`**

```yaml
name: tapo_terrace
subscribe_topic: camera/tapo_terrace/compressed
publish_topic: rf-detr-detector/tapo_terrace/detections
confidence_threshold: 0.5
```

- [ ] **Step 4: Create `rf-detr-detector/configs/tapo_entrance.yaml`**

```yaml
name: tapo_entrance
subscribe_topic: camera/tapo_entrance/compressed
publish_topic: rf-detr-detector/tapo_entrance/detections
confidence_threshold: 0.5
```

- [ ] **Step 5: Run `pixi install` to verify deps resolve**

```bash
cd rf-detr-detector
pixi install
```

Expected: environment created, no errors. RF-DETR and torch download may take a few minutes.

- [ ] **Step 6: Verify GStreamer elements available inside pixi env**

```bash
pixi run python -c "
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
for name in ['appsrc', 'appsink', 'h264parse', 'avdec_h264', 'videoconvert']:
    e = Gst.ElementFactory.find(name)
    print(name, ':', 'ok' if e else 'MISSING')
"
```

Expected: all five elements print `ok`.

- [ ] **Step 7: Verify RF-DETR imports**

```bash
pixi run python -c "from rfdetr import RFDETRBase; print('rfdetr ok')"
```

Expected: `rfdetr ok` (model weights not downloaded yet — that happens at first instantiation).

- [ ] **Step 8: Commit scaffold**

```bash
git add rf-detr-detector/
git commit -m "feat(rf-detr-detector): scaffold directory, pixi.toml, node.yaml, configs"
```

---

### Task 2: Config validation + payload builder (with tests)

**Files:**
- Create: `rf-detr-detector/tests/__init__.py` (empty)
- Create: `rf-detr-detector/tests/test_main.py`
- Create: `rf-detr-detector/main.py` (config + payload functions only, no GStreamer/model yet)

- [ ] **Step 1: Write failing tests**

Create `rf-detr-detector/tests/test_main.py`:

```python
"""Unit tests for config validation and detection payload builder."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from main import load_config, build_payload


# --- Config validation ---

def test_load_config_valid(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace\n"
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: rf-detr-detector/tapo_terrace/detections\n"
        "confidence_threshold: 0.5\n"
    )
    cfg = load_config(str(cfg_file))
    assert cfg["name"] == "tapo_terrace"
    assert cfg["subscribe_topic"] == "camera/tapo_terrace/compressed"
    assert cfg["publish_topic"] == "rf-detr-detector/tapo_terrace/detections"
    assert cfg["confidence_threshold"] == 0.5


def test_load_config_missing_name(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: rf-detr-detector/tapo_terrace/detections\n"
    )
    with pytest.raises(ValueError, match="name"):
        load_config(str(cfg_file))


def test_load_config_invalid_topic(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace\n"
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: bad topic with spaces\n"
        "confidence_threshold: 0.5\n"
    )
    with pytest.raises(ValueError, match="publish_topic"):
        load_config(str(cfg_file))


def test_load_config_threshold_bounds(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "name: tapo_terrace\n"
        "subscribe_topic: camera/tapo_terrace/compressed\n"
        "publish_topic: rf-detr-detector/tapo_terrace/detections\n"
        "confidence_threshold: 1.5\n"
    )
    with pytest.raises(ValueError, match="confidence_threshold"):
        load_config(str(cfg_file))


# --- Payload builder ---

def test_build_payload_with_detections():
    detections = [
        {"class_id": 0, "class_name": "person", "confidence": 0.91,
         "bbox": {"x1": 10, "y1": 20, "x2": 100, "y2": 200}}
    ]
    payload = build_payload(
        frame_id="tapo_terrace",
        machine_id="nvidia_orin00",
        scope="local",
        sequence=42,
        detections=detections,
    )
    assert payload["frame_id"] == "tapo_terrace"
    assert payload["machine_id"] == "nvidia_orin00"
    assert payload["scope"] == "local"
    assert payload["sequence"] == 42
    assert len(payload["detections"]) == 1
    assert payload["detections"][0]["class_name"] == "person"
    assert "timestamp" in payload


def test_build_payload_empty_detections():
    payload = build_payload(
        frame_id="tapo_terrace",
        machine_id="nvidia_orin00",
        scope="local",
        sequence=0,
        detections=[],
    )
    assert payload["detections"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd rf-detr-detector
pixi run python -m pytest tests/test_main.py -v 2>&1 | head -20
```

Expected: `ImportError: cannot import name 'load_config' from 'main'`

- [ ] **Step 3: Create `rf-detr-detector/main.py` with config + payload only**

```python
#!/usr/bin/env python3
"""rf-detr-detector — Object detection on H264 camera streams using RF-DETR."""

import logging
import queue
import re
import threading
from datetime import datetime, timezone

import yaml

log = logging.getLogger("rf-detr-detector")

TOPIC_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")

# COCO class names (80 classes, index = class_id)
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep",
    "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard",
    "sports ball", "kite", "baseball bat", "baseball glove", "skateboard",
    "surfboard", "tennis racket", "bottle", "wine glass", "cup", "fork",
    "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair",
    "couch", "potted plant", "bed", "dining table", "toilet", "tv",
    "laptop", "mouse", "remote", "keyboard", "cell phone", "microwave",
    "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase",
    "scissors", "teddy bear", "hair drier", "toothbrush",
]


def load_config(path: str) -> dict:
    """Load and validate config YAML. Raises ValueError on invalid fields."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    for required in ("name", "subscribe_topic", "publish_topic"):
        if not cfg.get(required):
            raise ValueError(f"Missing required config field: {required}")

    for field in ("subscribe_topic", "publish_topic"):
        if not TOPIC_RE.match(cfg[field]):
            raise ValueError(f"Invalid {field}: {cfg[field]!r} — must match [a-zA-Z0-9/_\\-.]+")

    threshold = float(cfg.get("confidence_threshold", 0.5))
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"confidence_threshold {threshold} out of range (0.0–1.0)")
    cfg["confidence_threshold"] = threshold

    return cfg


def build_payload(
    frame_id: str,
    machine_id: str,
    scope: str,
    sequence: int,
    detections: list[dict],
) -> dict:
    """Build the JSON detection payload published to Zenoh."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "frame_id": frame_id,
        "machine_id": machine_id,
        "scope": scope,
        "sequence": sequence,
        "detections": detections,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pixi run python -m pytest tests/test_main.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rf-detr-detector/main.py rf-detr-detector/tests/
git commit -m "feat(rf-detr-detector): config validation and payload builder with tests"
```

---

### Task 3: H264Decoder (GStreamer)

**Files:**
- Modify: `rf-detr-detector/main.py` — add `H264Decoder` class

- [ ] **Step 1: Add `H264Decoder` to `main.py`**

Add the following class after the `COCO_CLASSES` list and before `load_config`:

```python
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GLib
import numpy as np

Gst.init(None)


class H264Decoder:
    """GStreamer pipeline: H264 bytes → RGB numpy frames.

    Push raw H264 bytes with push(), pull decoded RGB frames with pull().
    A GLib main loop runs in a background thread to drive GStreamer callbacks.
    """

    _PIPELINE = (
        "appsrc name=src format=3 block=false max-bytes=2000000 "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! h264parse "
        "! avdec_h264 "
        "! videoconvert "
        "! video/x-raw,format=RGB "
        "! appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
    )

    def __init__(self) -> None:
        self._pipeline = Gst.parse_launch(self._PIPELINE)
        self._appsrc = self._pipeline.get_by_name("src")
        self._appsink = self._pipeline.get_by_name("sink")
        self._frame_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=2)
        self._appsink.connect("new-sample", self._on_new_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()

    def push(self, h264_bytes: bytes) -> None:
        """Feed one H264 AU (Access Unit) into the pipeline."""
        buf = Gst.Buffer.new_wrapped(h264_bytes)
        self._appsrc.emit("push-buffer", buf)

    def pull(self, timeout: float = 0.5) -> "np.ndarray | None":
        """Return the latest decoded RGB frame, or None if none arrived."""
        try:
            return self._frame_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        self._appsrc.emit("end-of-stream")
        self._pipeline.set_state(Gst.State.NULL)
        self._loop.quit()

    def _on_new_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0)
        width = struct.get_value("width")
        height = struct.get_value("height")
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if ok:
            frame = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape(height, width, 3).copy()
            buf.unmap(mapinfo)
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                pass  # drop oldest — keep pipeline moving
        return Gst.FlowReturn.OK
```

- [ ] **Step 2: Smoke-test the decoder with a synthetic H264 frame**

```bash
pixi run python -c "
import sys, time
sys.path.insert(0, '.')
# Verify the pipeline starts without error (no actual H264 data needed)
from main import H264Decoder
d = H264Decoder()
print('pipeline started ok')
# pull with short timeout — will be None since no data pushed
frame = d.pull(timeout=0.2)
print('pull result (expect None):', frame)
d.close()
print('pipeline closed ok')
"
```

Expected output:
```
pipeline started ok
pull result (expect None): None
pipeline closed ok
```

- [ ] **Step 3: Commit**

```bash
git add rf-detr-detector/main.py
git commit -m "feat(rf-detr-detector): H264Decoder GStreamer pipeline"
```

---

### Task 4: Detector (RF-DETR inference)

**Files:**
- Modify: `rf-detr-detector/main.py` — add `Detector` class

- [ ] **Step 1: Add `Detector` to `main.py`**

Add after the `H264Decoder` class:

```python
from PIL import Image
from rfdetr import RFDETRBase


class Detector:
    """RF-DETR inference wrapper.

    Loads the smallest RF-DETR model (RFDETRBase) on CPU at init.
    Call detect() with an RGB numpy frame to get detections.
    """

    def __init__(self, confidence_threshold: float = 0.5) -> None:
        log.info("Loading RF-DETR model (CPU)...")
        self._model = RFDETRBase(device="cpu")
        self._threshold = confidence_threshold
        log.info("RF-DETR model loaded.")

    def detect(self, frame_rgb: np.ndarray) -> list[dict]:
        """Run inference on an RGB numpy frame.

        Returns a list of detection dicts:
            {"class_id": int, "class_name": str, "confidence": float,
             "bbox": {"x1": int, "y1": int, "x2": int, "y2": int}}
        """
        pil_img = Image.fromarray(frame_rgb)
        try:
            result = self._model.predict(pil_img, threshold=self._threshold)
        except Exception as e:
            log.error("RF-DETR inference error: %s", e)
            return []

        detections = []
        for box, label, score in zip(result.boxes, result.labels, result.scores):
            class_id = int(label)
            class_name = COCO_CLASSES[class_id] if class_id < len(COCO_CLASSES) else str(class_id)
            detections.append({
                "class_id": class_id,
                "class_name": class_name,
                "confidence": round(float(score), 4),
                "bbox": {
                    "x1": int(box[0]), "y1": int(box[1]),
                    "x2": int(box[2]), "y2": int(box[3]),
                },
            })
        return detections
```

- [ ] **Step 2: Smoke-test the detector with a blank frame**

```bash
pixi run python -c "
import numpy as np
import sys
sys.path.insert(0, '.')
from main import Detector
d = Detector(confidence_threshold=0.5)
# blank 640x480 RGB frame — expect empty detections, no crash
blank = np.zeros((480, 640, 3), dtype=np.uint8)
result = d.detect(blank)
print('detections on blank frame:', result)
print('Detector smoke test passed')
"
```

Expected: `detections on blank frame: []` (model runs, no detections on blank image).

Note: first run downloads model weights (~150MB) to `~/.cache/`.

- [ ] **Step 3: Commit**

```bash
git add rf-detr-detector/main.py
git commit -m "feat(rf-detr-detector): Detector RF-DETR inference wrapper"
```

---

### Task 5: RfDetrDetectorNode — wiring it all together

**Files:**
- Modify: `rf-detr-detector/main.py` — add `RfDetrDetectorNode` class and `__main__` block

- [ ] **Step 1: Add node class and `__main__` to `main.py`**

Append to `main.py`:

```python
class RfDetrDetectorNode:
    """Bubbaloop node: subscribe to H264 camera frames, detect, publish JSON."""

    name = "rf-detr-detector"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._subscribe_topic = config["subscribe_topic"]
        self._threshold = config["confidence_threshold"]

        # Publishers and subscribers
        self._pub = ctx.publisher_json(config["publish_topic"])
        self._decoder = H264Decoder()
        self._detector = Detector(confidence_threshold=self._threshold)

        # ProtoDecoder for decoding CompressedImage protobuf payloads
        from bubbaloop_sdk import ProtoDecoder
        self._proto = ProtoDecoder(ctx._session)

        self._seq = 0
        log.info(
            "Subscribing to %s, publishing to %s",
            ctx.topic(self._subscribe_topic),
            ctx.topic(config["publish_topic"]),
        )

    def run(self) -> None:
        ctx = self._ctx

        def _on_frame(sample) -> None:
            # Decode CompressedImage protobuf
            data = self._proto.decode(sample)
            if data is None:
                # Schema not yet available — try fetching it
                key = str(sample.key_expr).rsplit("/", 1)[0] + "/schema"
                self._proto.prefetch_schema(key)
                return

            h264_bytes = data.get("data")
            if not h264_bytes:
                return

            # Bytes come as base64 string from MessageToDict
            import base64
            if isinstance(h264_bytes, str):
                h264_bytes = base64.b64decode(h264_bytes)

            header = data.get("header") or {}
            frame_id = header.get("frame_id", ctx.instance_name)

            # Push into GStreamer and pull decoded RGB frame
            self._decoder.push(h264_bytes)
            frame = self._decoder.pull(timeout=0.2)
            if frame is None:
                return

            # Run RF-DETR
            detections = self._detector.detect(frame)

            # Publish
            payload = build_payload(
                frame_id=frame_id,
                machine_id=ctx.machine_id,
                scope=ctx.scope,
                sequence=self._seq,
                detections=detections,
            )
            self._pub.put(payload)
            self._seq += 1

            if self._seq % 30 == 0:
                log.info(
                    "seq=%d detections=%d",
                    self._seq,
                    len(detections),
                )

        sub = ctx._session.declare_subscriber(
            ctx.topic(self._subscribe_topic), _on_frame
        )

        # Block until shutdown
        ctx._shutdown.wait()

        sub.undeclare()
        self._decoder.close()
        log.info("rf-detr-detector shutdown complete (processed %d frames)", self._seq)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node
    run_node(RfDetrDetectorNode)
```

- [ ] **Step 2: Verify syntax**

```bash
pixi run python -c "import main; print('syntax ok')"
```

Expected: `syntax ok`

- [ ] **Step 3: Run the existing unit tests to confirm nothing broke**

```bash
pixi run python -m pytest tests/test_main.py -v
```

Expected: all 6 tests pass.

- [ ] **Step 4: Commit**

```bash
git add rf-detr-detector/main.py
git commit -m "feat(rf-detr-detector): RfDetrDetectorNode — subscribe, decode, detect, publish"
```

---

### Task 6: Register with daemon and live test

**Files:** none — runtime registration only

- [ ] **Step 1: Register the tapo_terrace instance with the daemon**

```bash
cd /home/nvidia/bubbaloop-nodes-official
/home/nvidia/bubbaloop/target/release/bubbaloop node add \
  $(pwd)/rf-detr-detector \
  -n rf-detr-tapo-terrace \
  -c $(pwd)/rf-detr-detector/configs/tapo_terrace.yaml
```

Expected: `Node 'rf-detr-tapo-terrace' added`

- [ ] **Step 2: Install the systemd service**

```bash
/home/nvidia/bubbaloop/target/release/bubbaloop node install rf-detr-tapo-terrace
```

Expected: `Node 'rf-detr-tapo-terrace' installed`

- [ ] **Step 3: Start the node**

```bash
/home/nvidia/bubbaloop/target/release/bubbaloop node start rf-detr-tapo-terrace
```

Expected: `Started rf-detr-tapo-terrace`

- [ ] **Step 4: Tail logs to confirm model loaded and frames are flowing**

```bash
/home/nvidia/bubbaloop/target/release/bubbaloop node logs rf-detr-tapo-terrace -f
```

Expected within 30s (model download may take a minute on first run):
```
rf-detr-detector: Loading RF-DETR model (CPU)...
rf-detr-detector: RF-DETR model loaded.
rf-detr-detector: Subscribing to bubbaloop/local/nvidia_orin00/camera/tapo_terrace/compressed ...
```

Then every ~30 frames:
```
rf-detr-detector: seq=30 detections=2
```

- [ ] **Step 5: Verify detections are published via Zenoh**

```bash
python3 -c "
import sys, time
sys.path.insert(0, '/home/nvidia/bubbaloop/python-sdk')
import zenoh, json

conf = zenoh.Config()
conf.insert_json5('mode', '\"client\"')
conf.insert_json5('connect/endpoints', '[\"tcp/127.0.0.1:7447\"]')
session = zenoh.open(conf)

def cb(sample):
    data = json.loads(bytes(sample.payload))
    n = len(data.get('detections', []))
    persons = [d for d in data.get('detections', []) if d['class_name'] == 'person']
    print(f\"seq={data['sequence']} detections={n} persons={len(persons)}\")

sub = session.declare_subscriber('bubbaloop/**/detections', cb)
time.sleep(15)
sub.undeclare()
session.close()
" 2>/dev/null
```

Expected: detection lines printing every ~0.1s with detection counts.

- [ ] **Step 6: Commit final state and push**

```bash
cd /home/nvidia/bubbaloop-nodes-official
git add rf-detr-detector/
git commit -m "feat(rf-detr-detector): complete implementation — GStreamer + RF-DETR + bubbaloop node"
git push --set-upstream origin feat/rf-detr-detector
```

---

### Task 7: Add to nodes.yaml registry

**Files:**
- Modify: `nodes.yaml`

- [ ] **Step 1: Read current `nodes.yaml`**

```bash
cat /home/nvidia/bubbaloop-nodes-official/nodes.yaml
```

- [ ] **Step 2: Add rf-detr-detector entry**

Add to `nodes.yaml` alongside existing entries:

```yaml
- name: rf-detr-detector
  version: 0.1.0
  description: Object detection using RF-DETR on camera H264 streams
  type: python
  capabilities:
    - processor
  path: rf-detr-detector
```

- [ ] **Step 3: Commit**

```bash
git add nodes.yaml
git commit -m "chore: register rf-detr-detector in nodes.yaml"
git push
```
