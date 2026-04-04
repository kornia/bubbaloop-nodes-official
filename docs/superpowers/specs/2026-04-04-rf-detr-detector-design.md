# RF-DETR Detector Node Design

**Date**: 2026-04-04
**Status**: Implemented
**Scope**: New Python processor node in `bubbaloop-nodes-official`

---

## Problem

The rtsp-camera nodes publish H264 frames continuously. There is no node that performs inference on those frames. Agents cannot detect people in a scene without a dedicated processor node.

## Solution

A `processor` node called `rf-detr-detector` that subscribes to a camera's H264 stream, decodes frames via GStreamer, runs RF-DETR inference on CPU, and publishes structured JSON detections.

---

## Architecture

```
{subscribe_topic}  (CompressedImage protobuf, H264)
        ↓
  ProtoDecoder     — fetch schema once from node's /schema queryable
        ↓
  GStreamer        — appsrc → h264parse → avdec_h264 → videoconvert → appsink (RGB)
        ↓
  RF-DETR (CPU)   — smallest model (RFDETRBase), all COCO classes
        ↓
{publish_topic}    (JSON detections)
```

Multi-instance pattern: one node instance per camera, configured via separate YAML files — identical to how `rtsp-camera` works.

---

## Configuration

```yaml
# configs/tapo_terrace.yaml
name: tapo_terrace                                        # instance name → health/schema topics
subscribe_topic: camera/tapo_terrace/compressed           # camera H264 topic suffix
publish_topic: rf-detr-detector/tapo_terrace/detections   # detection output topic suffix
confidence_threshold: 0.5                                 # drop detections below this score
```

---

## Detection Output (JSON)

Published to `bubbaloop/{scope}/{machine_id}/{publish_topic}` at inference rate.

```json
{
  "timestamp": "2026-04-04T09:00:00Z",
  "frame_id": "tapo_terrace",
  "machine_id": "nvidia_orin00",
  "scope": "local",
  "sequence": 1234,
  "detections": [
    {
      "class_id": 0,
      "class_name": "person",
      "confidence": 0.91,
      "bbox": {"x1": 120, "y1": 80, "x2": 340, "y2": 610}
    }
  ]
}
```

- `bbox` in absolute pixels (x1/y1 top-left, x2/y2 bottom-right)
- All classes published (agent filters by `class_name` if needed)
- Empty `detections: []` published when nothing is detected (keeps stream alive)

---

## Node Structure

```
rf-detr-detector/
├── main.py              # Node class + GStreamer decoder + RF-DETR inference
├── node.yaml            # Manifest: processor, subscribes/publishes declared
├── pixi.toml            # Dependencies
├── configs/
│   ├── tapo_entrance.yaml
│   └── tapo_terrace.yaml
```

---

## Key Dependencies

| Package | Source | Purpose |
|---|---|---|
| `gstreamer`, `gst-plugins-base`, `gst-plugins-ugly` | conda-forge | H264 decode (avdec_h264) |
| `python-gstreamer` / `gst-python` | conda-forge | Python GStreamer bindings |
| `torch` (CPU) | PyPI | RF-DETR inference backend |
| `rfdetr` | PyPI | RF-DETR model + inference API |
| `protobuf` | PyPI | Decode CompressedImage schema |
| `bubbaloop-sdk` | git main | NodeContext, ProtoDecoder, run_node |
| `pillow` | PyPI | numpy → PIL for RF-DETR input |

---

## GStreamer Pipeline

```
appsrc name=src caps=video/x-h264,stream-format=byte-stream,alignment=au
  ! h264parse
  ! avdec_h264
  ! videoconvert
  ! video/x-raw,format=RGB
  ! appsink name=sink emit-signals=true sync=false
```

- `appsrc`: fed raw H264 bytes from each CompressedImage payload
- `avdec_h264`: software decode (CPU), available via `gst-plugins-ugly`
- `appsink`: pulls RGB frames as numpy arrays for RF-DETR input

---

## node.yaml

```yaml
name: rf-detr-detector
version: 0.1.0
type: python
description: Person and object detection using RF-DETR on camera H264 streams
author: Bubbaloop Team

command: pixi run main

capabilities:
  - processor

subscribes:
  - suffix: "{{subscribe_topic}}"
    description: H264 compressed frames from an rtsp-camera node
    encoding: application/protobuf

publishes:
  - suffix: "{{publish_topic}}"
    description: RF-DETR object detections with bounding boxes and confidence scores
    encoding: application/json
    rate_hz: 10.0

requires:
  hardware: []
  software:
    - GStreamer 1.0+ with gst-plugins-ugly (avdec_h264)
    - Python 3.11+
```

---

## Error Handling

- **Schema fetch fails**: log warning, retry on next frame (ProtoDecoder caches on first success)
- **GStreamer decode fails**: log warning, skip frame, pipeline stays alive
- **RF-DETR inference error**: log error, publish `detections: []` to keep topic alive
- **Camera goes offline**: subscriber stays declared; resumes automatically when camera restarts

---

## Implementation Notes

- GStreamer pipeline is created once at node init, kept alive for the duration
- ProtoDecoder fetches the camera's schema once on first frame, then caches
- RF-DETR model loaded at init (not per-frame) — model stays in RAM
- `confidence_threshold` applied after inference to drop low-confidence boxes
- Publish rate matches inference rate (no separate timer — publish on each decoded frame)
