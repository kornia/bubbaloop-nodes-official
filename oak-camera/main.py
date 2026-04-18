#!/usr/bin/env python3
"""oak-camera — publishes OAK RGB frames and serves on-demand depth queries.

Topics (all auto-scoped under ``config.name``):

- ``{name}/compressed`` — global, CBOR ``{header, body}`` envelope,
  ``body = {width, height, encoding:"jpeg", data}``.
- ``{name}/raw``        — local SHM (zero-copy), CBOR ``{header, body}``,
  ``body = {header: HeaderCbor, width, height, encoding:"rgba8", step, data}``
  — matches the rtsp-camera ``RawImageCborRef`` wire shape.
- ``{name}/depth_at_bbox`` — global queryable. Request JSON
  ``{x1, y1, x2, y2, op?}``; reply JSON
  ``{depth_mm, valid_pixels, total_pixels, ts_ns}`` or ``{"error": ...}``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import cbor2
import cv2
import depthai as dai
import numpy as np
import zenoh

log = logging.getLogger("oak-camera")

# Same regex the CLAUDE.md security checklist mandates for topic-name fragments.
_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")

_DEPTH_OPS = {
    "median": np.median,
    "mean": np.mean,
    "min": np.min,
}


def _validate(cfg: dict) -> dict:
    name = cfg.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("config.name is required")
    if not _NAME_RE.match(name):
        raise ValueError(
            "config.name must match ^[a-zA-Z0-9/_\\-\\.]+$ (got {!r})".format(name)
        )

    width = int(cfg.get("width", 1280))
    height = int(cfg.get("height", 720))
    if width % 16 or height % 16:
        raise ValueError("width/height must be multiples of 16")
    fps = float(cfg.get("fps", 30))
    if not 1.0 <= fps <= 60.0:
        raise ValueError("fps must be in [1, 60]")

    jpeg_every_n = int(cfg.get("jpeg_every_n", 3))
    if not 1 <= jpeg_every_n <= 60:
        raise ValueError("jpeg_every_n must be in [1, 60]")
    jpeg_quality = int(cfg.get("jpeg_quality", 80))
    if not 1 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be in [1, 100]")

    max_depth_mm = int(cfg.get("max_depth_mm", 10000))
    if max_depth_mm <= 0:
        raise ValueError("max_depth_mm must be > 0")

    return {
        "name": name,
        "width": width,
        "height": height,
        "fps": fps,
        "jpeg_every_n": jpeg_every_n,
        "jpeg_quality": jpeg_quality,
        "enable_depth": bool(cfg.get("enable_depth", True)),
        "max_depth_mm": max_depth_mm,
    }


def _envelope(body: dict, instance: str, suffix: str, seq: int) -> dict:
    return {
        "header": {
            "schema_uri": f"bubbaloop://{instance}/{suffix}@v1",
            "source_instance": instance,
            "monotonic_seq": seq,
            "ts_ns": time.time_ns(),
        },
        "body": body,
    }


def _raw_body(
    rgba: bytes, width: int, height: int, instance: str, machine_id: str, seq: int
) -> dict:
    """Build a RawImageCbor body that matches rtsp-camera's wire shape."""
    ts = time.time_ns()
    return {
        "header": {
            "acq_time": ts,
            "pub_time": ts,
            "sequence": seq,
            "frame_id": instance,
            "machine_id": machine_id,
        },
        "width": width,
        "height": height,
        "encoding": "rgba8",
        "step": width * 4,
        "data": rgba,
    }


class OakCameraNode:
    name = "oak-camera"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)

        # Compressed JPEG → global. Raw CBOR via SDK helper; we encode the
        # {header, body} envelope ourselves because the Python SDK has no
        # publisher_cbor helper yet.
        self._compressed_pub = ctx.publisher_raw("compressed")

        # Raw RGBA8 → local SHM. SHM auto-negotiates because run_node()
        # calls NodeContext.builder().with_shm() at startup.
        self._raw_pub = ctx.publisher_raw_local("raw")
        self._raw_seq = 0

        # Pre-allocated scratch buffer for BGR→RGBA conversion (3.7 MB at
        # 1280×720). Avoids ~111 MB/s of per-frame allocations at 30 fps.
        self._rgba_buf = np.empty(
            (self._cfg["height"], self._cfg["width"], 4), dtype=np.uint8,
        )

        # Depth — lazy request/response instead of streaming the full frame.
        self._depth_lock = threading.Lock()
        self._latest_depth: np.ndarray | None = None
        self._latest_depth_ts_ns: int = 0
        self._depth_queryable = None
        # Reject absurdly-large bbox requests: cap at full frame area.
        self._max_bbox_pixels = self._cfg["width"] * self._cfg["height"]

        log.info("Configured: %s", self._cfg)
        log.info("compressed → %s", ctx.topic("compressed"))
        log.info("raw (SHM)  → %s", ctx.local_topic("raw"))

    def _on_depth_query(self, query: zenoh.Query) -> None:
        try:
            payload = query.payload
            raw = bytes(payload) if payload is not None else b""
            req = json.loads(raw.decode()) if raw else {}
            x1 = int(req.get("x1", 0))
            y1 = int(req.get("y1", 0))
            x2 = int(req.get("x2", 0))
            y2 = int(req.get("y2", 0))
            op = str(req.get("op", "median")).lower()
            reducer = _DEPTH_OPS.get(op)
            if reducer is None:
                raise ValueError(f"unsupported op: {op}")

            with self._depth_lock:
                frame = self._latest_depth
                ts_ns = self._latest_depth_ts_ns
            if frame is None:
                reply = {"error": "no_depth_frame_yet"}
            else:
                h, w = frame.shape
                x1c = max(0, min(x1, w))
                x2c = max(0, min(x2, w))
                y1c = max(0, min(y1, h))
                y2c = max(0, min(y2, h))
                if x2c <= x1c or y2c <= y1c:
                    reply = {"error": "empty_bbox"}
                elif (x2c - x1c) * (y2c - y1c) > self._max_bbox_pixels:
                    reply = {"error": "bbox_too_large"}
                else:
                    roi = frame[y1c:y2c, x1c:x2c]
                    total = int(roi.size)
                    valid_mask = (roi > 0) & (roi < self._cfg["max_depth_mm"])
                    valid = roi[valid_mask]
                    if valid.size == 0:
                        reply = {"error": "no_valid_depth", "total_pixels": total}
                    else:
                        reply = {
                            "depth_mm": int(reducer(valid)),
                            "valid_pixels": int(valid.size),
                            "total_pixels": total,
                            "ts_ns": int(ts_ns),
                            "op": op,
                        }
            query.reply(query.key_expr, json.dumps(reply).encode())
        except Exception as exc:
            log.exception("depth query failed")
            query.reply(
                query.key_expr,
                json.dumps({"error": f"internal: {exc}"}).encode(),
            )

    def _build_pipeline(self, pipeline: dai.Pipeline):
        w = self._cfg["width"]
        h = self._cfg["height"]
        fps = self._cfg["fps"]

        cam_rgb = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
        rgb_out = cam_rgb.requestOutput((w, h), type=dai.ImgFrame.Type.BGR888i, fps=fps)
        q_rgb = rgb_out.createOutputQueue(maxSize=4, blocking=False)

        q_depth = None
        if self._cfg["enable_depth"]:
            try:
                mono_left = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_B)
                mono_right = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_C)
                stereo = pipeline.create(dai.node.StereoDepth)
                mono_left.requestOutput((640, 400), fps=fps).link(stereo.left)
                mono_right.requestOutput((640, 400), fps=fps).link(stereo.right)
                stereo.setDepthAlign(dai.CameraBoardSocket.CAM_A)
                try:
                    stereo.setOutputSize(w, h)
                except AttributeError:
                    pass
                q_depth = stereo.depth.createOutputQueue(maxSize=4, blocking=False)
                log.info("Stereo depth enabled, aligned to CAM_A, %dx%d", w, h)
            except Exception as exc:
                log.warning("Stereo depth unavailable (%s) — RGB-only mode", exc)
                q_depth = None

        return q_rgb, q_depth

    def run(self) -> None:
        ctx = self._ctx
        cfg = self._cfg
        instance = ctx.instance_name or self.name

        with dai.Pipeline() as pipeline:
            q_rgb, q_depth = self._build_pipeline(pipeline)

            if q_depth is not None:
                key = ctx.topic("depth_at_bbox")
                self._depth_queryable = ctx.session.declare_queryable(
                    key, self._on_depth_query
                )
                log.info("depth_at_bbox queryable → %s", key)

            pipeline.start()
            log.info("Pipeline started. Streaming at %.1f fps", cfg["fps"])

            while not ctx.is_shutdown():
                rgb_msg = q_rgb.get()
                if rgb_msg is None:
                    continue
                bgr = rgb_msg.getCvFrame()
                h, w = bgr.shape[:2]

                cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA, dst=self._rgba_buf)
                raw_envelope = _envelope(
                    _raw_body(
                        self._rgba_buf.tobytes(), w, h,
                        instance, ctx.machine_id, self._raw_seq,
                    ),
                    instance, "raw", self._raw_seq,
                )
                self._raw_pub.put(cbor2.dumps(raw_envelope))
                self._raw_seq += 1

                if self._raw_seq % cfg["jpeg_every_n"] == 0:
                    ok, jpeg = cv2.imencode(
                        ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, cfg["jpeg_quality"]]
                    )
                    if ok:
                        jpeg_envelope = _envelope(
                            {
                                "width": w, "height": h,
                                "encoding": "jpeg",
                                "data": jpeg.tobytes(),
                            },
                            instance, "compressed", self._raw_seq,
                        )
                        self._compressed_pub.put(cbor2.dumps(jpeg_envelope))

                if q_depth is not None:
                    # Non-blocking pull: skip this frame's depth update if the queue
                    # is empty rather than stalling the RGB loop.
                    depth_msg = q_depth.tryGet()
                    if depth_msg is not None:
                        # Copy out of DepthAI-owned memory; otherwise the view
                        # goes stale once the message is released.
                        frame = np.ascontiguousarray(depth_msg.getFrame())
                        with self._depth_lock:
                            self._latest_depth = frame
                            self._latest_depth_ts_ns = time.time_ns()

            log.info("Shutdown requested — stopping")

        if self._depth_queryable is not None:
            self._depth_queryable.undeclare()
        self._raw_pub.undeclare()
        self._compressed_pub.undeclare()


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(OakCameraNode)
