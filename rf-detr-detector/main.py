#!/usr/bin/env python3
"""rf-detr-detector — Object detection on H264 camera streams using RF-DETR."""

import base64
import logging
import re
import threading
import time
from datetime import datetime, timezone

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst

import numpy as np
import torch
import yaml
from rfdetr import RFDETRBase, RFDETRLarge, RFDETRMedium, RFDETRSmall, RFDETRNano

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
            raise ValueError(
                f"Invalid {field}: {cfg[field]!r} — must match [a-zA-Z0-9/_\\-.]+"
            )

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
    """Build the JSON detection payload published to Zenoh."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "frame_id": frame_id,
        "machine_id": machine_id,
        "scope": scope,
        "sequence": sequence,
        "detections": detections,
    }


class H264Decoder:
    """GStreamer H264 decoder — CPU fallback (avdec_h264 → RGB numpy).

    Decoded frames are stored in a single overwriting slot so pull() always
    returns the most recent frame, not a stale buffered one.
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
        Gst.init(None)
        self._pipeline = Gst.parse_launch(self._PIPELINE)
        self._appsrc = self._pipeline.get_by_name("src")
        self._appsink = self._pipeline.get_by_name("sink")
        self._latest_frame: "np.ndarray | None" = None
        self._frame_lock = threading.Lock()
        self._appsink.connect("new-sample", self._on_new_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()

    def push(self, h264_bytes: bytes) -> None:
        buf = Gst.Buffer.new_wrapped(h264_bytes)
        self._appsrc.emit("push-buffer", buf)

    def pull(self) -> "np.ndarray | None":
        """Return and consume the latest decoded frame, or None if none ready."""
        with self._frame_lock:
            frame = self._latest_frame
            self._latest_frame = None
            return frame

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
            frame = (
                np.frombuffer(mapinfo.data, dtype=np.uint8)
                .reshape(height, width, 3)
                .copy()
            )
            buf.unmap(mapinfo)
            with self._frame_lock:
                self._latest_frame = frame
        return Gst.FlowReturn.OK


class H264DecoderCUDA:
    """GStreamer H264 decoder using Jetson NVDEC hardware — outputs CHW float CUDA tensor.

    Pipeline: nvv4l2decoder (Jetson hardware H264 decode) → nvvidconv (VIC
    color conversion NV12→RGB) → CPU-side appsink → torch CHW tensor on CUDA.

    Decoded frames are stored in a single overwriting slot so pull() always
    returns the most recent frame, not a stale buffered one.
    """

    # nvvidconv does not support RGB output — use RGBA then drop the alpha channel
    _PIPELINE = (
        "appsrc name=src format=3 block=false max-bytes=2000000 "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! h264parse "
        "! nvv4l2decoder "
        "! nvvidconv "
        "! video/x-raw,format=RGBA "
        "! appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
    )

    def __init__(self) -> None:
        Gst.init(None)
        self._pipeline = Gst.parse_launch(self._PIPELINE)
        self._appsrc = self._pipeline.get_by_name("src")
        self._appsink = self._pipeline.get_by_name("sink")
        self._latest_frame: "torch.Tensor | None" = None
        self._frame_lock = threading.Lock()
        self._appsink.connect("new-sample", self._on_new_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()

    def push(self, h264_bytes: bytes) -> None:
        buf = Gst.Buffer.new_wrapped(h264_bytes)
        self._appsrc.emit("push-buffer", buf)

    def pull(self) -> "torch.Tensor | None":
        """Return and consume the latest (C, H, W) float32 CUDA tensor [0,1], or None."""
        with self._frame_lock:
            frame = self._latest_frame
            self._latest_frame = None
            return frame

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
            # CPU RGBA bytes from nvvidconv — one H2D transfer to CUDA
            frame_cpu = (
                np.frombuffer(mapinfo.data, dtype=np.uint8)
                .reshape(height, width, 4)
                .copy()
            )
            buf.unmap(mapinfo)
            # RGBA HWC uint8 → RGB CHW float32 [0,1] on CUDA (drop alpha, non-blocking H2D)
            tensor = (
                torch.from_numpy(frame_cpu[:, :, :3])
                .permute(2, 0, 1)
                .to(dtype=torch.float32, device="cuda", non_blocking=True)
                .div_(255.0)
            )
            with self._frame_lock:
                self._latest_frame = tensor
        return Gst.FlowReturn.OK


class Detector:
    """RF-DETR inference wrapper.

    Accepts either a PIL/numpy image (CPU path) or a normalized CHW torch.Tensor
    (GPU path — RF-DETR moves it to model.device automatically).
    """

    _MODEL_CLASSES = {
        "nano": RFDETRNano,
        "small": RFDETRSmall,
        "base": RFDETRBase,
        "medium": RFDETRMedium,
        "large": RFDETRLarge,
    }

    def __init__(self, confidence_threshold: float = 0.5, device: str = "cpu", model: str = "base") -> None:
        model_cls = self._MODEL_CLASSES[model]
        log.info("Loading RF-DETR model (size=%s, device=%s)...", model, device)
        self._model = model_cls(device=device)
        self._model.optimize_for_inference()
        self._threshold = confidence_threshold
        log.info("RF-DETR model loaded and optimized (size=%s).", model)

    def detect(self, image) -> list[dict]:
        """Run inference. image may be np.ndarray (HWC uint8) or torch.Tensor (CHW float [0,1]).

        torch.Tensor is passed directly to RF-DETR (no PIL roundtrip).
        np.ndarray (HWC uint8) is converted to CHW float32 tensor first.
        """
        if isinstance(image, np.ndarray):
            image = (
                torch.from_numpy(image)
                .permute(2, 0, 1)
                .to(dtype=torch.float32)
                .div_(255.0)
            )

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
    """Bubbaloop node: subscribe to H264 camera frames, detect, publish JSON."""

    name = "rf-detr-detector"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._subscribe_topic = config["subscribe_topic"]
        self._target_fps = config.get("target_fps", 1.0)

        self._pub = ctx.publisher_json(config["publish_topic"])

        cuda_ok = torch.cuda.is_available()
        model_size = config.get("model", "base")
        if cuda_ok:
            log.info("CUDA available — using nvv4l2decoder (NVDEC) + H2D CUDA tensor path")
            self._decoder = H264DecoderCUDA()
            self._detector = Detector(
                confidence_threshold=config["confidence_threshold"],
                device="cuda",
                model=model_size,
            )
        else:
            log.info("CUDA not available — using CPU avdec_h264 path")
            self._decoder = H264Decoder()
            self._detector = Detector(
                confidence_threshold=config["confidence_threshold"],
                device="cpu",
                model=model_size,
            )

        from bubbaloop_sdk import ProtoDecoder

        self._proto = ProtoDecoder(ctx.session)
        self._seq = 0

        # Camera node publishes at "camera/{name}/compressed" but its schema
        # lives at "{name}/schema" (instance_name level, not data path level).
        parts = self._subscribe_topic.split("/")
        source_instance = parts[1] if len(parts) >= 2 else parts[0]
        self._schema_key = ctx.topic(f"{source_instance}/schema")

        log.info(
            "Subscribing to %s, publishing to %s at %.1f fps (schema: %s)",
            ctx.topic(self._subscribe_topic),
            ctx.topic(config["publish_topic"]),
            self._target_fps,
            self._schema_key,
        )

    def run(self) -> None:
        ctx = self._ctx

        # Subscriber: decode H264 and keep the latest frame ready — no inference here.
        def _on_frame(sample) -> None:
            data = self._proto.decode(sample, schema_key=self._schema_key)
            if data is None:
                return
            h264_bytes = data.get("data")
            if not h264_bytes:
                return
            if isinstance(h264_bytes, str):
                h264_bytes = base64.b64decode(h264_bytes)
            self._decoder.push(h264_bytes)

        # Inference thread: fires at target_fps, grabs the latest decoded frame.
        def _inference_loop() -> None:
            interval = 1.0 / self._target_fps
            next_run = time.monotonic() + interval
            while not ctx._shutdown.is_set():
                now = time.monotonic()
                sleep_for = next_run - now
                if sleep_for > 0:
                    time.sleep(sleep_for)
                next_run += interval

                frame = self._decoder.pull()
                if frame is None:
                    continue

                header_frame_id = ctx.instance_name  # fallback; header not tracked here

                t0 = time.monotonic()
                detections = self._detector.detect(frame)
                t1 = time.monotonic()

                payload = build_payload(
                    frame_id=header_frame_id,
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

        sub = ctx.session.declare_subscriber(ctx.topic(self._subscribe_topic), _on_frame)
        ctx._shutdown.wait()
        sub.undeclare()
        inference_thread.join(timeout=2.0)
        self._decoder.close()
        log.info(
            "rf-detr-detector shutdown complete (processed %d frames)", self._seq
        )


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(RfDetrDetectorNode)
