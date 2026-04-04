#!/usr/bin/env python3
"""rf-detr-detector — Object detection on H264 camera streams using RF-DETR."""

import base64
import ctypes
import logging
import queue
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

# ---------------------------------------------------------------------------
# NvBufSurface ctypes structs (Jetson NVMM zero-copy path)
# From /usr/src/jetson_multimedia_api/include/nvbufsurface.h
# STRUCTURE_PADDING=4, NVBUF_MAX_PLANES=4
# ---------------------------------------------------------------------------
_MAX_PLANES = 4
_STRUCT_PAD = 4


class _NvBufSurfacePlaneParams(ctypes.Structure):
    _fields_ = [
        ("num_planes",   ctypes.c_uint32),
        ("width",        ctypes.c_uint32 * _MAX_PLANES),
        ("height",       ctypes.c_uint32 * _MAX_PLANES),
        ("pitch",        ctypes.c_uint32 * _MAX_PLANES),
        ("offset",       ctypes.c_uint32 * _MAX_PLANES),
        ("psize",        ctypes.c_uint32 * _MAX_PLANES),
        ("bytesPerPix",  ctypes.c_uint32 * _MAX_PLANES),
        ("_reserved",    ctypes.c_void_p * (_STRUCT_PAD * _MAX_PLANES)),
    ]


class _NvBufSurfaceMappedAddr(ctypes.Structure):
    _fields_ = [
        ("addr",      ctypes.c_void_p * _MAX_PLANES),
        ("eglImage",  ctypes.c_void_p),
        ("_reserved", ctypes.c_void_p * _STRUCT_PAD),
    ]


class _NvBufSurfaceParams(ctypes.Structure):
    _fields_ = [
        ("width",        ctypes.c_uint32),
        ("height",       ctypes.c_uint32),
        ("pitch",        ctypes.c_uint32),
        ("colorFormat",  ctypes.c_int),    # enum
        ("layout",       ctypes.c_int),    # enum
        ("bufferDesc",   ctypes.c_uint64),
        ("dataSize",     ctypes.c_uint32),
        ("dataPtr",      ctypes.c_void_p), # CUDA device ptr for NVBUF_MEM_CUDA_DEVICE
        ("planeParams",  _NvBufSurfacePlaneParams),
        ("mappedAddr",   _NvBufSurfaceMappedAddr),
        ("paramex",      ctypes.c_void_p), # NvBufSurfaceParamsEx*
        ("_reserved",    ctypes.c_void_p * (_STRUCT_PAD - 1)),
    ]


class _NvBufSurface(ctypes.Structure):
    _fields_ = [
        ("gpuId",        ctypes.c_uint32),
        ("batchSize",    ctypes.c_uint32),
        ("numFilled",    ctypes.c_uint32),
        ("isContiguous", ctypes.c_bool),
        ("memType",      ctypes.c_int),    # enum; NVBUF_MEM_CUDA_DEVICE = 2
        ("surfaceList",  ctypes.POINTER(_NvBufSurfaceParams)),
        ("_reserved",    ctypes.c_void_p * _STRUCT_PAD),
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
    """GStreamer H264 decoder — CPU fallback (avdec_h264 → RGB numpy).

    Used when CUDA is not available. Outputs HWC uint8 numpy arrays.
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
        buf = Gst.Buffer.new_wrapped(h264_bytes)
        self._appsrc.emit("push-buffer", buf)

    def pull(self, timeout: float = 0.5) -> "np.ndarray | None":
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
                pass
        return Gst.FlowReturn.OK


class H264DecoderCUDA:
    """GStreamer H264 decoder using Jetson hardware — zero-copy CUDA tensor output.

    Pipeline: nvv4l2decoder (NVDEC hardware decode) → nvvidconv with
    NVBUF_MEM_CUDA_DEVICE → NVMM buffer in CUDA device memory.

    In the appsink callback, buf.map(READ) gives a pointer to the NvBufSurface
    metadata struct (NOT the pixel data). We read surfaceList[0].dataPtr via
    ctypes to get the raw CUDA device pointer, wrap it as a cupy UnownedMemory,
    convert RGBA → RGB CHW float32 entirely on GPU, then hand off to torch via
    DLPack — zero CPU memory involvement.
    """

    # nvbuf-memory-type=2 → NVBUF_MEM_CUDA_DEVICE (GPU-accessible CUDA memory)
    _PIPELINE = (
        "appsrc name=src format=3 block=false max-bytes=2000000 "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! h264parse "
        "! nvv4l2decoder "
        "! nvvidconv nvbuf-memory-type=2 "
        "! video/x-raw(memory:NVMM),format=RGBA "
        "! appsink name=sink emit-signals=true sync=false max-buffers=2 drop=true"
    )

    def __init__(self) -> None:
        import cupy as cp
        self._cp = cp

        Gst.init(None)
        self._pipeline = Gst.parse_launch(self._PIPELINE)
        self._appsrc = self._pipeline.get_by_name("src")
        self._appsink = self._pipeline.get_by_name("sink")
        self._frame_queue: queue.Queue[torch.Tensor] = queue.Queue(maxsize=2)
        self._appsink.connect("new-sample", self._on_new_sample)
        self._pipeline.set_state(Gst.State.PLAYING)
        self._loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._loop.run, daemon=True)
        self._thread.start()

    def push(self, h264_bytes: bytes) -> None:
        buf = Gst.Buffer.new_wrapped(h264_bytes)
        self._appsrc.emit("push-buffer", buf)

    def pull(self, timeout: float = 0.5) -> "torch.Tensor | None":
        """Return a (C, H, W) float32 CUDA tensor [0,1], or None."""
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
            # mapinfo.data for NVMM buffers points to the NvBufSurface metadata struct,
            # not the pixel data. We read surfaceList[0].dataPtr to get the CUDA device ptr.
            nvbuf = ctypes.cast(mapinfo.data, ctypes.POINTER(_NvBufSurface))
            cuda_ptr = nvbuf[0].surfaceList[0].dataPtr

            cp = self._cp
            # All cupy ops in device(0) context — prevents peer-access probe on
            # single-GPU Jetson (cupy tries deviceCanAccessPeer(0,1) → invalid ordinal)
            with cp.cuda.Device(0):
                # Wrap existing CUDA device memory without allocation (zero-copy).
                # device_id=0 is required — without it cupy probes peer access to
                # non-existent devices and raises cudaErrorInvalidDevice on Jetson.
                mem = cp.cuda.UnownedMemory(cuda_ptr, height * width * 4, owner=None, device_id=0)
                arr_rgba = cp.ndarray(
                    (height, width, 4),
                    dtype=cp.uint8,
                    memptr=cp.cuda.MemoryPointer(mem, 0),
                )
                # GPU-only: RGBA HWC → RGB CHW float32 [0,1]
                arr_rgb_chw = cp.ascontiguousarray(
                    arr_rgba[:, :, :3].transpose(2, 0, 1)
                ).astype(cp.float32) / 255.0

                # Synchronize before releasing the GstBuffer so GPU copy is complete
                cp.cuda.Stream.null.synchronize()

            buf.unmap(mapinfo)

            # DLPack hand-off: torch takes ownership of arr_rgb_chw's CUDA memory
            tensor = torch.from_dlpack(arr_rgb_chw.toDlpack())
            try:
                self._frame_queue.put_nowait(tensor)
            except queue.Full:
                pass
        return Gst.FlowReturn.OK


class Detector:
    """RF-DETR inference wrapper.

    Accepts either a PIL/numpy image (CPU path) or a normalized CHW torch.Tensor
    (GPU path — RF-DETR moves it to model.device automatically).
    """

    def __init__(self, confidence_threshold: float = 0.5, device: str = "cpu") -> None:
        log.info("Loading RF-DETR model (device=%s)...", device)
        self._model = RFDETRBase(device=device)
        self._threshold = confidence_threshold
        log.info("RF-DETR model loaded.")

    def detect(self, image) -> list[dict]:
        """Run inference. image may be np.ndarray (HWC uint8) or torch.Tensor (CHW float [0,1])."""
        if isinstance(image, np.ndarray):
            from PIL import Image
            image = Image.fromarray(image)

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

        self._pub = ctx.publisher_json(config["publish_topic"])

        cuda_ok = torch.cuda.is_available()
        if cuda_ok:
            log.info("CUDA available — using nvv4l2decoder + zero-copy CUDA tensor path")
            self._decoder = H264DecoderCUDA()
            self._detector = Detector(
                confidence_threshold=config["confidence_threshold"],
                device="cuda",
            )
        else:
            log.info("CUDA not available — using CPU avdec_h264 path")
            self._decoder = H264Decoder()
            self._detector = Detector(
                confidence_threshold=config["confidence_threshold"],
                device="cpu",
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
            "Subscribing to %s, publishing to %s (schema: %s)",
            ctx.topic(self._subscribe_topic),
            ctx.topic(config["publish_topic"]),
            self._schema_key,
        )

    def run(self) -> None:
        ctx = self._ctx

        def _on_frame(sample) -> None:
            t0 = time.monotonic()

            data = self._proto.decode(sample, schema_key=self._schema_key)
            if data is None:
                return

            h264_bytes = data.get("data")
            if not h264_bytes:
                return

            if isinstance(h264_bytes, str):
                h264_bytes = base64.b64decode(h264_bytes)

            header = data.get("header") or {}
            frame_id = header.get("frame_id", ctx.instance_name)

            t1 = time.monotonic()
            self._decoder.push(h264_bytes)
            frame = self._decoder.pull(timeout=0.5)
            if frame is None:
                return
            t2 = time.monotonic()

            detections = self._detector.detect(frame)
            t3 = time.monotonic()

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
                    "seq=%d detections=%d decode=%.1fms gst=%.1fms infer=%.1fms total=%.1fms",
                    self._seq,
                    len(detections),
                    (t1 - t0) * 1000,
                    (t2 - t1) * 1000,
                    (t3 - t2) * 1000,
                    (t3 - t0) * 1000,
                )

        sub = ctx.session.declare_subscriber(ctx.topic(self._subscribe_topic), _on_frame)
        ctx._shutdown.wait()
        sub.undeclare()
        self._decoder.close()
        log.info(
            "rf-detr-detector shutdown complete (processed %d frames)", self._seq
        )


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(RfDetrDetectorNode)
