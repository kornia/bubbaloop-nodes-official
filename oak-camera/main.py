#!/usr/bin/env python3
"""oak-camera — publishes OAK RGB + aligned depth as a single RGBD message.

Topics (auto-scoped under ``config.name``):

- ``{name}/compressed`` — global, CBOR envelope, body = {width, height, encoding:"jpeg", data}.
- ``{name}/rgbd``       — local SHM (zero-copy, CongestionControl.BLOCK), CBOR envelope,
  body = {header, rgb, depth?}. Both `rgb` and `depth` share the shape
  {width, height, encoding, step, data}. `depth` is omitted when the device has
  no stereo cameras or they're disabled in config.
"""

from __future__ import annotations

import logging
import re
import time

import cv2
import depthai as dai
import numpy as np

log = logging.getLogger("oak-camera")

# CLAUDE.md security requirement for topic-name fragments.
_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


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

    return {
        "name": name,
        "width": width,
        "height": height,
        "fps": fps,
        "jpeg_every_n": jpeg_every_n,
        "jpeg_quality": jpeg_quality,
        "enable_depth": bool(cfg.get("enable_depth", True)),
    }


def _rgbd_body(
    rgba: bytes,
    width: int,
    height: int,
    instance: str,
    machine_id: str,
    seq: int,
    depth: bytes | None = None,
    depth_width: int = 0,
    depth_height: int = 0,
) -> dict:
    """Build an RGBD body with symmetric RGB + depth planes.

    Each plane is a dict with the same shape: {width, height, encoding, step, data}.
    The inner `header` carries capture-timing metadata (acq_time, pub_time,
    sequence, frame_id, machine_id) — shape mirrors rtsp-camera's HeaderCbor.
    The outer SDK envelope (schema_uri, source_instance, monotonic_seq, ts_ns)
    is applied automatically by `publisher_cbor`.

    When `depth` is None the top-level `depth` key is omitted so consumers can
    cheaply check `"depth" in body` — no None/null on the wire.
    """
    ts = time.time_ns()
    body: dict = {
        "header": {
            "acq_time": ts,
            "pub_time": ts,
            "sequence": seq,
            "frame_id": instance,
            "machine_id": machine_id,
        },
        "rgb": {
            "width": width,
            "height": height,
            "encoding": "rgba8",
            "step": width * 4,
            "data": rgba,
        },
    }
    if depth is not None:
        body["depth"] = {
            "width": depth_width,
            "height": depth_height,
            "encoding": "depth16_mm",
            "step": depth_width * 2,
            "data": depth,
        }
    return body


class OakCameraNode:
    name = "oak-camera"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)

        # Compressed JPEG → global. RGBD → local SHM. Both go through the SDK so
        # SHM auto-negotiates for same-machine consumers (run_node() enables it
        # at session build time).
        self._compressed_pub = ctx.publisher_cbor("compressed", schema_uri="bubbaloop://compressed/v1")
        self._rgbd_pub = ctx.publisher_cbor("rgbd", local=True, schema_uri="bubbaloop://rgbd/v1")
        self._seq = 0

        # Pre-allocated scratch buffer for BGR→RGBA (3.7 MB at 1280×720). Avoids
        # ~111 MB/s of per-frame allocations at 30 fps.
        self._rgba_buf = np.empty(
            (self._cfg["height"], self._cfg["width"], 4), dtype=np.uint8,
        )

        log.info("Configured: %s", self._cfg)
        log.info("compressed → %s", ctx.topic("compressed"))
        log.info("rgbd (SHM) → %s", ctx.local_topic("rgbd"))

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
            pipeline.start()
            log.info("Pipeline started. Streaming at %.1f fps", cfg["fps"])

            # Last-known depth: depth queue may be slower than RGB, so we reuse
            # the most recent depth frame until a fresher one lands. Tag it with
            # its own sequence so stale-beyond-N-frames detection is possible
            # downstream if needed.
            depth_bytes: bytes | None = None
            depth_w = 0
            depth_h = 0

            while not ctx.is_shutdown():
                rgb_msg = q_rgb.get()
                if rgb_msg is None:
                    continue
                bgr = rgb_msg.getCvFrame()
                h, w = bgr.shape[:2]

                if q_depth is not None:
                    depth_msg = q_depth.tryGet()
                    if depth_msg is not None:
                        # Copy out of DepthAI-owned memory — the view goes stale
                        # once the message is released.
                        depth_frame = np.ascontiguousarray(
                            depth_msg.getFrame().astype(np.uint16, copy=False)
                        )
                        depth_h, depth_w = depth_frame.shape
                        depth_bytes = depth_frame.tobytes()

                cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA, dst=self._rgba_buf)
                body = _rgbd_body(
                    self._rgba_buf.tobytes(), w, h,
                    instance, ctx.machine_id, self._seq,
                    depth=depth_bytes,
                    depth_width=depth_w,
                    depth_height=depth_h,
                )
                self._rgbd_pub.put(body)
                self._seq += 1

                if self._seq % cfg["jpeg_every_n"] == 0:
                    ok, jpeg = cv2.imencode(
                        ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, cfg["jpeg_quality"]]
                    )
                    if ok:
                        self._compressed_pub.put({"width": w, "height": h, "encoding": "jpeg", "data": jpeg.tobytes()})

            log.info("Shutdown requested — stopping")

        self._rgbd_pub.undeclare()
        self._compressed_pub.undeclare()


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(OakCameraNode)
