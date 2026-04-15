#!/usr/bin/env python3
"""camera-object-detector — Object detection on camera raw frames via Zenoh SHM.

Subscribes to `{key}/raw` (CBOR RawImage, encoding="rgba8", over Zenoh SHM
published by the rtsp-camera node) and publishes JSON detections to
`{key}/detections`.

Topic key is derived from the instance name: `tapo_terrace_detector` → `tapo_terrace`.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import numpy as np
import torch
import yaml
from ultralytics import YOLO

log = logging.getLogger("camera-object-detector")


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

    valid_models = ("nano", "small", "medium", "large", "xlarge")
    model = cfg.get("model", "nano")
    if model not in valid_models:
        raise ValueError(f"model {model!r} must be one of {valid_models}")
    cfg["model"] = model

    target_fps = float(cfg.get("target_fps", 1.0))
    if not (0.1 <= target_fps <= 30.0):
        raise ValueError(f"target_fps {target_fps} out of range (0.1–30.0)")
    cfg["target_fps"] = target_fps

    device = cfg.get("device", "cuda")
    if device not in ("cuda", "cpu"):
        raise ValueError(f"device {device!r} must be 'cuda' or 'cpu'")
    cfg["device"] = device

    return cfg


def build_payload(
    frame_id: str,
    machine_id: str,
    sequence: int,
    detections: list[dict],
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "frame_id": frame_id,
        "machine_id": machine_id,
        "sequence": sequence,
        "detections": detections,
    }


class Detector:
    """YOLO11 inference wrapper. Accepts a CHW float32 CUDA tensor in [0,1]."""

    _MODEL_FILES = {
        "nano": "yolo11n.pt",
        "small": "yolo11s.pt",
        "medium": "yolo11m.pt",
        "large": "yolo11l.pt",
        "xlarge": "yolo11x.pt",
    }

    def __init__(self, confidence_threshold: float = 0.5, model: str = "nano", device: str = "cuda") -> None:
        model_file = self._MODEL_FILES[model]
        self._device = device
        log.info("Loading YOLO11 model (size=%s, device=%s)...", model, device)
        self._model = YOLO(model_file)
        self._threshold = confidence_threshold
        self._class_names = self._model.names  # {0: 'person', 1: 'bicycle', ...}
        log.info("YOLO11 model loaded (size=%s, device=%s, %d classes).", model, device, len(self._class_names))

    def detect(self, image: torch.Tensor) -> list[dict]:
        """Run inference on a CHW float32 CUDA tensor. Returns list of detections."""
        try:
            # YOLO expects BCHW with dims divisible by stride (32).
            # Resize if needed — bilinear on CUDA is ~1ms.
            _, h, w = image.shape
            if h % 32 != 0 or w % 32 != 0:
                new_h = (h + 31) // 32 * 32
                new_w = (w + 31) // 32 * 32
                image = torch.nn.functional.interpolate(
                    image.unsqueeze(0), size=(new_h, new_w), mode="bilinear", align_corners=False
                )
            else:
                image = image.unsqueeze(0)
            results = self._model(image, conf=self._threshold, device=self._device, verbose=False)
        except Exception as e:
            log.error("YOLO11 inference error: %s", e)
            return []

        result = results[0]
        detections = []
        for box, conf, cls_id in zip(result.boxes.xyxy, result.boxes.conf, result.boxes.cls):
            class_id = int(cls_id)
            class_name = self._class_names.get(class_id, str(class_id))
            detections.append(
                {
                    "class_id": class_id,
                    "class_name": class_name,
                    "confidence": round(float(conf), 4),
                    "bbox": {
                        "x1": int(box[0]),
                        "y1": int(box[1]),
                        "x2": int(box[2]),
                        "y2": int(box[3]),
                    },
                }
            )
        return detections


class CameraObjectDetector:
    """Bubbaloop node: subscribe to raw RGBA camera frames over SHM, detect, publish JSON.

    Topic derivation from instance name:
      tapo_terrace_detector → topic key: tapo_terrace
        subscribe:  tapo_terrace/raw          (CBOR, RGBA, SHM)
        publish:    tapo_terrace/detections   (JSON)
    """

    name = "camera-object-detector"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._target_fps = config["target_fps"]

        # Derive topic key: strip "_detector" suffix from instance name
        instance_name = config["name"]
        topic_key = instance_name.removesuffix("_detector")

        self._topic_key = topic_key
        self._pub = ctx.publisher_json(f"{topic_key}/detections")
        self._device = config["device"]
        self._detector = Detector(
            confidence_threshold=config["confidence_threshold"],
            model=config["model"],
            device=self._device,
        )

        # Latest decoded frame slot — written by subscriber callback, read by inference loop
        self._latest_frame: "torch.Tensor | None" = None
        self._frame_lock = threading.Lock()
        self._seq = 0

        log.info(
            "Subscribing to %s/raw (CBOR SHM), publishing to %s at %.1f fps",
            topic_key,
            ctx.topic(f"{topic_key}/detections"),
            self._target_fps,
        )

    def run(self) -> None:
        ctx = self._ctx
        sub = ctx.subscribe(f"{self._topic_key}/raw", local=True)

        def _receive_loop() -> None:
            # Buffers are allocated on the first frame using dimensions from the
            # CBOR message (msg.width, msg.height).  After that, every frame
            # reuses the same memory — zero per-frame allocs.
            # On Jetson unified memory, uncontrolled CUDA allocs eat system RAM
            # and cause OOM reboots.
            device = self._device
            rgb_buf = None
            rgb_np = None
            dev_buf = None

            for env in sub:
                # SDK >=Apr2026 wraps CBOR payloads in a {header, body} Envelope.
                # getattr keeps compatibility with non-enveloped upstreams.
                msg = getattr(env, "body", env)
                w, h = msg.width, msg.height

                # First frame (or resolution change): allocate once.
                if rgb_buf is None or rgb_buf.shape[0] != h or rgb_buf.shape[1] != w:
                    if device == "cuda":
                        rgb_buf = torch.empty((h, w, 3), dtype=torch.uint8).pin_memory()
                        dev_buf = torch.empty((3, h, w), dtype=torch.float32, device="cuda")
                        log.info("Allocated frame buffers: %dx%d (pinned CPU + CUDA f32)", w, h)
                    else:
                        rgb_buf = torch.empty((h, w, 3), dtype=torch.uint8)
                        dev_buf = torch.empty((3, h, w), dtype=torch.float32)
                        log.info("Allocated frame buffers: %dx%d (CPU f32)", w, h)
                    rgb_np = rgb_buf.numpy()

                rgba = np.frombuffer(msg.data, dtype=np.uint8).reshape(h, w, 4)
                np.copyto(rgb_np, rgba[:, :, :3])
                del rgba, msg

                chw_u8 = rgb_buf.permute(2, 0, 1).contiguous()
                dev_buf.copy_(chw_u8).div_(255.0)
                del chw_u8

                with self._frame_lock:
                    self._latest_frame = dev_buf

        def _inference_loop() -> None:
            interval = 1.0 / self._target_fps
            next_run = time.monotonic()
            while not ctx._shutdown.is_set():
                now = time.monotonic()
                if now < next_run:
                    time.sleep(min(0.05, next_run - now))
                    continue

                with self._frame_lock:
                    # Clone inside lock to prevent receive thread from overwriting
                    # cuda_buf contents between lock release and clone.
                    frame = self._latest_frame.clone() if self._latest_frame is not None else None
                    self._latest_frame = None

                if frame is None:
                    time.sleep(0.05)
                    continue

                next_run = time.monotonic() + interval
                t0 = time.monotonic()
                detections = self._detector.detect(frame)
                del frame
                t1 = time.monotonic()

                payload = build_payload(
                    frame_id=ctx.instance_name,
                    machine_id=ctx.machine_id,
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

        receive_thread = threading.Thread(target=_receive_loop, daemon=True, name="receive")
        inference_thread = threading.Thread(target=_inference_loop, daemon=True, name="inference")
        receive_thread.start()
        inference_thread.start()

        ctx._shutdown.wait()
        sub.undeclare()
        inference_thread.join(timeout=2.0)
        log.info("camera-object-detector shutdown complete (processed %d frames)", self._seq)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(CameraObjectDetector)
