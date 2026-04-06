#!/usr/bin/env python3
"""rf-detr-detector — Object detection on camera raw frames via Zenoh SHM.

Subscribes to `{key}/raw` (RawImage protobuf, RGBA, published over Zenoh SHM
by the rtsp-camera node) and publishes JSON detections to `{key}/detections`.

Topic key is derived from the instance name: `tapo_terrace_detector` → `tapo_terrace`.
Schema is fetched live from the camera node's schema queryable.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import numpy as np
import torch
import yaml
from rfdetr import RFDETRBase, RFDETRLarge, RFDETRMedium, RFDETRSmall, RFDETRNano

log = logging.getLogger("rf-detr-detector")

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
    """Load and validate config YAML."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("name"):
        raise ValueError("Missing required config field: name")

    threshold = float(cfg.get("confidence_threshold", 0.5))
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"confidence_threshold {threshold} out of range (0.0–1.0)")
    cfg["confidence_threshold"] = threshold

    valid_models = ("nano", "small", "base", "medium", "large")
    model = cfg.get("model", "base")
    if model not in valid_models:
        raise ValueError(f"model {model!r} must be one of {valid_models}")
    cfg["model"] = model

    target_fps = float(cfg.get("target_fps", 1.0))
    if not (0.1 <= target_fps <= 30.0):
        raise ValueError(f"target_fps {target_fps} out of range (0.1–30.0)")
    cfg["target_fps"] = target_fps

    return cfg


def build_payload(
    frame_id: str,
    machine_id: str,
    scope: str,
    sequence: int,
    detections: list[dict],
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "frame_id": frame_id,
        "machine_id": machine_id,
        "scope": scope,
        "sequence": sequence,
        "detections": detections,
    }


class Detector:
    """RF-DETR inference wrapper. Accepts a CHW float32 CUDA tensor in [0,1]."""

    _MODEL_CLASSES = {
        "nano": RFDETRNano,
        "small": RFDETRSmall,
        "base": RFDETRBase,
        "medium": RFDETRMedium,
        "large": RFDETRLarge,
    }

    def __init__(self, confidence_threshold: float = 0.5, model: str = "base") -> None:
        model_cls = self._MODEL_CLASSES[model]
        log.info("Loading RF-DETR model (size=%s, device=cuda)...", model)
        self._model = model_cls(device="cuda")
        self._model.optimize_for_inference()
        self._threshold = confidence_threshold
        log.info("RF-DETR model loaded and optimized (size=%s).", model)

    def detect(self, image: torch.Tensor) -> list[dict]:
        """Run inference on a CHW float32 CUDA tensor. Returns list of detections."""
        try:
            result = self._model.predict(image, threshold=self._threshold)
        except Exception as e:
            log.error("RF-DETR inference error: %s", e)
            return []

        detections = []
        for box, class_id, score in zip(result.xyxy, result.class_id, result.confidence):
            class_id = int(class_id)
            class_name = COCO_CLASSES[class_id] if class_id < len(COCO_CLASSES) else str(class_id)
            detections.append(
                {
                    "class_id": class_id,
                    "class_name": class_name,
                    "confidence": round(float(score), 4),
                    "bbox": {
                        "x1": int(box[0]),
                        "y1": int(box[1]),
                        "x2": int(box[2]),
                        "y2": int(box[3]),
                    },
                }
            )
        return detections


class RfDetrDetectorNode:
    """Bubbaloop node: subscribe to raw RGBA camera frames over SHM, detect, publish JSON.

    Topic derivation from instance name:
      tapo_terrace_detector → topic key: tapo_terrace
        subscribe:  tapo_terrace/raw          (RawImage proto, RGBA, SHM)
        publish:    tapo_terrace/detections   (JSON)
        schema:     tapo_terrace_camera/schema
    """

    name = "rf-detr-detector"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._target_fps = config["target_fps"]

        # Derive topic key: strip "_detector" suffix from instance name
        instance_name = config["name"]
        topic_key = instance_name.removesuffix("_detector")

        self._raw_topic = ctx.topic(f"{topic_key}/raw")
        self._raw_width = int(config.get("raw_width", 560))
        self._raw_height = int(config.get("raw_height", 560))

        self._pub = ctx.publisher_json(f"{topic_key}/detections")
        self._detector = Detector(
            confidence_threshold=config["confidence_threshold"],
            model=config["model"],
        )

        # Latest decoded frame slot — written by subscriber callback, read by inference loop
        self._latest_frame: "torch.Tensor | None" = None
        self._frame_lock = threading.Lock()
        self._seq = 0

        log.info(
            "Subscribing to %s (SHM RGBA %dx%d), publishing to %s at %.1f fps",
            self._raw_topic,
            self._raw_width,
            self._raw_height,
            ctx.topic(f"{topic_key}/detections"),
            self._target_fps,
        )

    def run(self) -> None:
        ctx = self._ctx

        w, h = self._raw_width, self._raw_height
        expected = w * h * 4

        def _on_raw_frame(sample) -> None:
            # Payload is raw RGBA bytes published from Zenoh SHM — no protobuf wrapper.
            raw_bytes = bytes(sample.payload)
            if len(raw_bytes) != expected:
                return

            # RGBA HWC uint8 → RGB CHW float32 [0,1] on CUDA (drop alpha, non-blocking H2D)
            frame_cpu = np.frombuffer(raw_bytes, dtype=np.uint8).reshape(h, w, 4)
            tensor = (
                torch.from_numpy(frame_cpu[:, :, :3].copy())
                .permute(2, 0, 1)
                .to(dtype=torch.float32, device="cuda", non_blocking=True)
                .div_(255.0)
            )
            with self._frame_lock:
                self._latest_frame = tensor

        def _inference_loop() -> None:
            interval = 1.0 / self._target_fps
            next_run = time.monotonic()
            while not ctx._shutdown.is_set():
                now = time.monotonic()
                if now < next_run:
                    time.sleep(min(0.05, next_run - now))
                    continue

                with self._frame_lock:
                    frame = self._latest_frame
                    self._latest_frame = None

                if frame is None:
                    time.sleep(0.05)
                    continue

                next_run = time.monotonic() + interval
                t0 = time.monotonic()
                detections = self._detector.detect(frame)
                t1 = time.monotonic()

                payload = build_payload(
                    frame_id=ctx.instance_name,
                    machine_id=ctx.machine_id,
                    scope=ctx.scope,
                    sequence=self._seq,
                    detections=detections,
                )
                self._pub.put(payload)
                self._seq += 1

                log.info(
                    "seq=%d detections=%d infer=%.1fms",
                    self._seq,
                    len(detections),
                    (t1 - t0) * 1000,
                )

        inference_thread = threading.Thread(target=_inference_loop, daemon=True, name="inference")
        inference_thread.start()

        sub = ctx.session.declare_subscriber(self._raw_topic, _on_raw_frame)
        ctx._shutdown.wait()
        sub.undeclare()
        inference_thread.join(timeout=2.0)
        log.info("rf-detr-detector shutdown complete (processed %d frames)", self._seq)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(RfDetrDetectorNode)
