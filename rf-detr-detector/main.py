#!/usr/bin/env python3
"""rf-detr-detector — Object detection on H264 camera streams using RF-DETR."""

import base64
import logging
import queue
import re
import threading
from datetime import datetime, timezone

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst

import numpy as np
import yaml
from PIL import Image
from rfdetr import RFDETRBase

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
        Gst.init(None)
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
            frame = (
                np.frombuffer(mapinfo.data, dtype=np.uint8)
                .reshape(height, width, 3)
                .copy()
            )
            buf.unmap(mapinfo)
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                pass  # drop oldest — keep pipeline moving
        return Gst.FlowReturn.OK


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

        Returns a list of detection dicts with class_id, class_name,
        confidence, and bbox (x1/y1/x2/y2 in absolute pixels).
        """
        pil_img = Image.fromarray(frame_rgb)
        try:
            result = self._model.predict(pil_img, threshold=self._threshold)
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

        self._pub = ctx.publisher_json(config["publish_topic"])
        self._decoder = H264Decoder()
        self._detector = Detector(confidence_threshold=config["confidence_threshold"])

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
            data = self._proto.decode(sample)
            if data is None:
                # Schema not cached yet — fetch and wait for next frame
                schema_key = str(sample.key_expr).rsplit("/", 1)[0] + "/schema"
                self._proto.prefetch_schema(schema_key)
                return

            h264_bytes = data.get("data")
            if not h264_bytes:
                return

            if isinstance(h264_bytes, str):
                h264_bytes = base64.b64decode(h264_bytes)

            header = data.get("header") or {}
            frame_id = header.get("frame_id", ctx.instance_name)

            self._decoder.push(h264_bytes)
            frame = self._decoder.pull(timeout=0.2)
            if frame is None:
                return

            detections = self._detector.detect(frame)

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
                log.info("seq=%d detections=%d", self._seq, len(detections))

        sub = ctx._session.declare_subscriber(ctx.topic(self._subscribe_topic), _on_frame)
        ctx._shutdown.wait()
        sub.undeclare()
        self._decoder.close()
        log.info(
            "rf-detr-detector shutdown complete (processed %d frames)", self._seq
        )


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(RfDetrDetectorNode)
