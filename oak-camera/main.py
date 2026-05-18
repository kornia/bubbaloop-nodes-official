#!/usr/bin/env python3
"""oak-camera — publishes OAK RGB + aligned depth as RGBD messages.

Topics (auto-scoped under ``config.name``):

- ``{name}/compressed``      — global CBOR, body = {width, height, encoding:"jpeg", data}.
- ``{name}/rgbd``            — local SHM CBOR, body = {header, rgb, depth?}.
- ``{name}/rgbd_compressed`` — global CBOR (opt-in), body = {width, height, rgb, depth}
  where rgb.encoding="jpeg" and depth.encoding="rvl" (lossless RVL-compressed 16-bit).
- ``{name}/imu``             — global CBOR (opt-in), body = {accel, gyro, timestamp_us}.
- ``{name}/grab_frame``      — local queryable, returns JPEG receipt JSON on demand.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import cv2
import depthai as dai
import kornia_rs.kornia_rs as kr
import numpy as np

log = logging.getLogger("oak-camera")

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


def _ns_to_iso8601(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).isoformat()


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

    imu_hz = int(cfg.get("imu_hz", 50))
    if not 1 <= imu_hz <= 500:
        raise ValueError("imu_hz must be in [1, 500]")

    return {
        "name": name,
        "width": width,
        "height": height,
        "fps": fps,
        "jpeg_every_n": jpeg_every_n,
        "jpeg_quality": jpeg_quality,
        "enable_depth": bool(cfg.get("enable_depth", True)),
        "enable_imu": bool(cfg.get("enable_imu", True)),
        "imu_hz": imu_hz,
        "enable_rgbd_compressed": bool(cfg.get("enable_rgbd_compressed", False)),
        "enable_grab_frame": bool(cfg.get("enable_grab_frame", True)),
    }


def _rgbd_body(
    rgba: bytes,
    width: int,
    height: int,
    instance: str,
    machine_id: str,
    seq: int,
    acq_time_ns: int = 0,
    depth: bytes | None = None,
    depth_width: int = 0,
    depth_height: int = 0,
) -> dict:
    """Build an RGBD body with symmetric RGB + depth planes.

    Each plane is a dict with the same shape: {width, height, encoding, step, data}.
    The inner `header` carries capture-timing metadata (acq_time, pub_time,
    sequence, frame_id, machine_id) — shape mirrors rtsp-camera's HeaderCbor.
    `acq_time_ns` should be the device-clock timestamp from getTimestampDevice()
    so it shares the same clock domain as IMU timestamps.

    When `depth` is None the top-level `depth` key is omitted so consumers can
    cheaply check `"depth" in body` — no None/null on the wire.
    """
    pub_time = time.time_ns()
    if acq_time_ns == 0:
        acq_time_ns = pub_time
    body: dict = {
        "header": {
            "acq_time": acq_time_ns,
            "pub_time": pub_time,
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


def _grab_frame_worker(
    session,
    key_expr: str,
    cache: dict,
    lock: threading.Lock,
    shutdown_evt: threading.Event,
) -> None:
    """Background thread serving the grab_frame queryable.

    Encodes the cached RGBA frame as JPEG (quality=75) on demand.
    Non-blocking lock: if the capture loop is updating the cache, we serve
    the previous frame rather than stalling the queryable thread.
    """
    def _on_query(query):
        with lock:
            frame = cache.get("frame")
        if frame is None:
            query.reply_err(query.key_expr, b"no frame available yet")
            return
        orig_w, orig_h = frame["width"], frame["height"]
        scale = min(1.0, 1024 / max(orig_w, orig_h))
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        rgb = np.ascontiguousarray(frame["rgba"][:, :, :3])
        if (new_w, new_h) != (orig_w, orig_h):
            rgb = kr.resize(rgb, (new_h, new_w), "bilinear")
        jpeg = kr.image.Image.frombuffer(np.ascontiguousarray(rgb)).encode("jpeg", quality=75)
        age_ms = (time.time_ns() - frame["recv_wall_ns"]) // 1_000_000
        acq_iso = _ns_to_iso8601(frame["recv_wall_ns"])
        label = (
            f"Camera '{frame['instance']}' — captured {acq_iso} ({age_ms}ms ago), "
            f"original {orig_w}×{orig_h} → downsampled {new_w}×{new_h}:"
        )
        receipt = {
            "camera": frame["instance"],
            "machine_id": frame["machine_id"],
            "acq_time_iso": acq_iso,
            "age_ms": age_ms,
            "original_width": orig_w,
            "original_height": orig_h,
            "downsampled_width": new_w,
            "downsampled_height": new_h,
            "jpeg_b64": base64.b64encode(jpeg).decode(),
            "media_type": "image/jpeg",
            "label": label,
        }
        query.reply(query.key_expr, json.dumps(receipt).encode())

    qable = session.declare_queryable(key_expr, _on_query)
    shutdown_evt.wait()
    qable.undeclare()


class OakCameraNode:
    name = "oak-camera"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)

        self._compressed_pub = ctx.publisher_cbor("compressed", schema_uri="bubbaloop://compressed/v1")
        self._rgbd_pub = ctx.publisher_cbor("rgbd", local=True, schema_uri="bubbaloop://rgbd/v1")
        self._rgbd_compressed_pub = (
            ctx.publisher_cbor("rgbd_compressed") if self._cfg["enable_rgbd_compressed"] else None
        )
        self._imu_pub = (
            ctx.publisher_cbor("imu") if self._cfg["enable_imu"] else None
        )
        self._seq = 0

        # Pre-allocated scratch buffer for BGR→RGBA (3.7 MB at 1280×720). Avoids
        # ~111 MB/s of per-frame allocations at 30 fps.
        self._rgba_buf = np.empty(
            (self._cfg["height"], self._cfg["width"], 4), dtype=np.uint8,
        )

        # grab_frame queryable: non-blocking lock keeps the capture loop hot.
        self._frame_cache: dict = {}
        self._frame_lock = threading.Lock()
        self._shutdown_evt = threading.Event()

        if self._cfg["enable_grab_frame"]:
            grab_key = ctx.local_topic("grab_frame")
            threading.Thread(
                target=_grab_frame_worker,
                args=(ctx.session, grab_key, self._frame_cache, self._frame_lock, self._shutdown_evt),
                daemon=True,
            ).start()
            log.info("grab_frame queryable → %s", grab_key)

        log.info("Configured: %s", self._cfg)
        log.info("compressed → %s", ctx.topic("compressed"))
        log.info("rgbd (SHM) → %s", ctx.local_topic("rgbd"))
        if self._rgbd_compressed_pub:
            log.info("rgbd_compressed → %s", ctx.topic("rgbd_compressed"))
        if self._imu_pub:
            log.info("imu → %s", ctx.topic("imu"))

    def _build_pipeline(self, pipeline: dai.Pipeline):
        """Build DepthAI pipeline.

        RGB and depth (when available) are always synced on-device via
        dai.node.Sync (16 ms threshold) so both planes share the same
        hardware-capture timestamp before crossing USB.  IMU is kept on a
        separate queue so every packet between frames is delivered — not just
        the one closest to the frame timestamp.

        Returns (q_sync, has_depth, q_imu).
        """
        w = self._cfg["width"]
        h = self._cfg["height"]
        fps = self._cfg["fps"]

        cam_rgb = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
        rgb_out = cam_rgb.requestOutput((w, h), type=dai.ImgFrame.Type.BGR888i, fps=fps)

        stereo = None
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
                log.info("Stereo depth enabled, aligned to CAM_A, %dx%d", w, h)
            except Exception as exc:
                log.warning("Stereo depth unavailable (%s) — RGB-only mode", exc)
                stereo = None

        # On-device sync: RGB + depth share the same capture timestamp.
        # 16 ms = half a frame at 30 fps — tight enough for aligned RGBD,
        # loose enough to absorb stereo pipeline latency.
        sync = pipeline.create(dai.node.Sync)
        sync.setSyncThreshold(timedelta(milliseconds=16))
        sync.setSyncAttempts(-1)
        rgb_out.link(sync.inputs["rgb"])
        if stereo is not None:
            stereo.depth.link(sync.inputs["depth"])
        q_sync = sync.out.createOutputQueue(maxSize=4, blocking=False)
        log.info("On-device Sync enabled (threshold=16ms)")

        q_imu = None
        if self._cfg["enable_imu"]:
            try:
                imu = pipeline.create(dai.node.IMU)
                imu.enableIMUSensor(dai.IMUSensor.ACCELEROMETER_RAW, self._cfg["imu_hz"])
                imu.enableIMUSensor(dai.IMUSensor.GYROSCOPE_RAW, self._cfg["imu_hz"])
                # Batch 5 reports before sending — reduces USB overhead when running
                # RGB + depth + IMU simultaneously (community-validated threshold).
                imu.setBatchReportThreshold(5)
                imu.setMaxBatchReports(20)
                q_imu = imu.out.createOutputQueue(maxSize=50, blocking=False)
                log.info("IMU enabled: ACCEL + GYRO at %d Hz", self._cfg["imu_hz"])
            except Exception as exc:
                log.warning("IMU unavailable (%s) — IMU disabled", exc)
                q_imu = None

        return q_sync, stereo is not None, q_imu

    def _open_device(self) -> dai.Device:
        """Open the first available OAK device, falling back to USB2 if USB3 fails.

        MyriadX defaults to SuperSpeed after firmware boot; if the camera is
        behind a USB2 hub the boot succeeds but re-enumeration fails at SUPER
        speed.  Trying HIGH (480 Mbps) makes the round-trip work correctly.
        """
        devices = dai.Device.getAllAvailableDevices()
        if not devices:
            raise RuntimeError("No OAK device found")
        dev_info = devices[0]
        for speed in (dai.UsbSpeed.SUPER, dai.UsbSpeed.HIGH):
            try:
                dev = dai.Device(dev_info, speed)
                log.info("Device %s opened at USB speed %s", dev_info.name, speed.name)
                return dev
            except Exception as exc:
                log.warning("USB speed %s failed (%s), trying next", speed.name, exc)
        raise RuntimeError("Could not open OAK device at any USB speed")

    def run(self) -> None:
        ctx = self._ctx
        cfg = self._cfg
        instance = ctx.instance_name or self.name

        device = self._open_device()
        with dai.Pipeline(device) as pipeline:
            q_sync, has_depth, q_imu = self._build_pipeline(pipeline)
            pipeline.start()
            log.info("Pipeline started. Streaming at %.1f fps", cfg["fps"])

            depth_frame: np.ndarray | None = None
            depth_w = 0
            depth_h = 0

            while not ctx.is_shutdown():
                group = q_sync.get()
                if group is None:
                    continue

                rgb_msg = group["rgb"]
                bgr = rgb_msg.getCvFrame()
                h, w = bgr.shape[:2]
                # Use the device-clock capture timestamp so RGB, depth, and IMU
                # timestamps are all on the same clock domain.
                acq_time_ns = int(rgb_msg.getTimestampDevice().total_seconds() * 1e9)

                if has_depth and "depth" in group:
                    depth_msg = group["depth"]
                    # Copy out of DepthAI-owned memory — the view goes stale
                    # once the message group is released.
                    depth_frame = np.ascontiguousarray(
                        depth_msg.getFrame().astype(np.uint16, copy=False)
                    )
                    depth_h, depth_w = depth_frame.shape

                cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA, dst=self._rgba_buf)

                # Update grab_frame cache (non-blocking — liveness > consistency).
                if cfg["enable_grab_frame"] and self._frame_lock.acquire(blocking=False):
                    try:
                        self._frame_cache["frame"] = {
                            "rgba": self._rgba_buf.copy(),
                            "width": w,
                            "height": h,
                            "acq_time": acq_time_ns,
                            "recv_wall_ns": time.time_ns(),
                            "instance": instance,
                            "machine_id": ctx.machine_id,
                        }
                    finally:
                        self._frame_lock.release()

                body = _rgbd_body(
                    self._rgba_buf.tobytes(), w, h,
                    instance, ctx.machine_id, self._seq,
                    acq_time_ns=acq_time_ns,
                    depth=depth_frame.tobytes() if depth_frame is not None else None,
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
                        self._compressed_pub.put({
                            "width": w, "height": h, "encoding": "jpeg", "data": jpeg.tobytes(),
                        })

                    # rgbd_compressed: JPEG rgb + RVL depth (lossless).
                    # RVL delta+zigzag+VLE: ~11ms on NEON, 13% smaller than PNG lvl1.
                    if self._rgbd_compressed_pub is not None and depth_frame is not None:
                        rgb = self._rgba_buf[:, :, :3]  # RGBA → RGB (already in R,G,B order)
                        rgb_jpeg = kr.image.Image.frombuffer(
                            np.ascontiguousarray(rgb)
                        ).encode("jpeg", quality=cfg["jpeg_quality"])
                        depth_rvl = kr.io.encode_image_rvl(depth_frame[:, :, np.newaxis])
                        self._rgbd_compressed_pub.put({
                            "width": w,
                            "height": h,
                            "rgb":   {"encoding": "jpeg", "data": rgb_jpeg},
                            "depth": {"encoding": "rvl",  "data": depth_rvl},
                        })

                # IMU: drain all packets accumulated since the last RGB frame.
                # Kept on a separate queue (not in the sync group) so every reading
                # between frames is delivered — not just the one nearest the frame.
                if q_imu is not None and self._imu_pub is not None:
                    for pkt in q_imu.tryGetAll():
                        for report in pkt.packets:
                            acc = report.acceleroMeter
                            gyr = report.gyroscope
                            ts_us = int(acc.getTimestampDevice().total_seconds() * 1_000_000)
                            self._imu_pub.put({
                                "accel": {"x": acc.x, "y": acc.y, "z": acc.z},
                                "gyro":  {"x": gyr.x, "y": gyr.y, "z": gyr.z},
                                "timestamp_us": ts_us,
                            })

            log.info("Shutdown requested — stopping")

        self._shutdown_evt.set()
        self._rgbd_pub.undeclare()
        self._compressed_pub.undeclare()
        if self._rgbd_compressed_pub:
            self._rgbd_compressed_pub.undeclare()
        if self._imu_pub:
            self._imu_pub.undeclare()


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(OakCameraNode)
