#!/usr/bin/env python3
"""oak-camera — publishes OAK RGB + aligned depth as a single RGBD message.

Topics (auto-scoped under ``config.name``):

- ``{name}/compressed`` — global, CBOR envelope, body = {header, rgb (jpeg),
  depth (png16)?}. Network-visible recordable RGBD: JPEG RGB + PNG-16 lossless
  depth. Throttled to ``jpeg_every_n`` frames; sized for ~12 GB/hr at 1280×720@10 fps.
- ``{name}/rgbd`` — local SHM (zero-copy, CongestionControl.BLOCK), CBOR
  envelope, body = {header, rgb, depth?}. Same shape as ``compressed`` but
  rgb/depth are raw (rgba8 / depth16_mm). For downstream perception nodes
  that need pixel-accurate input on the same machine.
- ``{name}/calibration`` — global, CBOR envelope, body = camera intrinsics
  block. Static for the session; re-published periodically (default 1 Hz)
  so late-joining subscribers (recorder, dashboard) see it within one
  publish interval. Drop-rate-resilient analog of an MQTT retained message.

Both image topics share the body schema, so a single decoder works for both.
The ``rgb["encoding"]`` field discriminates: ``"jpeg"`` vs ``"rgba8"``.

RGB ↔ depth synchronization is enforced by ``dai.node.Sync`` on the OAK's
Leon coprocessor: only frame pairs whose mid-shutter timestamps fall within
``sync_threshold_ms`` are emitted. The ``header.acq_time`` carries the
RGB mid-shutter device clock; ``header.sync_interval_ns`` carries the actual
RGB↔depth gap so downstream consumers can filter further if needed.
"""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import timedelta

import depthai as dai
import kornia_rs as kr
import numpy as np
from PIL import Image

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

    # Level 1 is ~2 ms encode at 1280×720 vs ~10 ms at level 6; on natural depth
    # the size delta between them is small because PNG's filter stage already
    # captures most spatial redundancy. Default to fast.
    depth_png_compression = int(cfg.get("depth_png_compression", 1))
    if not 0 <= depth_png_compression <= 9:
        raise ValueError("depth_png_compression must be in [0, 9]")

    # Sync threshold for RGB↔depth pairing. Default = half the frame interval
    # (e.g. 16 ms at 30 fps) — the strictest setting that still tolerates
    # normal pipeline jitter. Tighter → more drops; looser → consecutive depth
    # frames may pair with the same RGB, defeating the sync.
    default_sync_ms = max(1, int(1000.0 / fps / 2.0))
    sync_threshold_ms = int(cfg.get("sync_threshold_ms", default_sync_ms))
    if not 1 <= sync_threshold_ms <= 1000:
        raise ValueError("sync_threshold_ms must be in [1, 1000]")

    # -1 (default): only emit perfectly-synced pairs (drop unmatched frames).
    # 0: emit as soon as the group is filled, regardless of timing.
    # >0: try N times to sync, then send best-effort.
    sync_attempts = int(cfg.get("sync_attempts", -1))
    if sync_attempts < -1:
        raise ValueError("sync_attempts must be >= -1")

    # How often to (re-)publish the calibration on the separate topic.
    # Static-data semantics: any subscriber joining within this interval sees
    # the calibration. Trade-off: lower interval = lower late-join latency,
    # higher background bandwidth. Default 1 Hz ≈ 200 B/s overhead.
    calibration_publish_interval_secs = float(cfg.get("calibration_publish_interval_secs", 1.0))
    if not 0.1 <= calibration_publish_interval_secs <= 60.0:
        raise ValueError("calibration_publish_interval_secs must be in [0.1, 60.0]")

    return {
        "name": name,
        "width": width,
        "height": height,
        "fps": fps,
        "jpeg_every_n": jpeg_every_n,
        "jpeg_quality": jpeg_quality,
        "enable_depth": bool(cfg.get("enable_depth", True)),
        "depth_png_compression": depth_png_compression,
        "sync_threshold_ms": sync_threshold_ms,
        "sync_attempts": sync_attempts,
        "calibration_publish_interval_secs": calibration_publish_interval_secs,
    }


def _rgbd_body(
    rgba: bytes,
    width: int,
    height: int,
    instance: str,
    machine_id: str,
    seq: int,
    acq_time_ns: int | None = None,
    sync_interval_ns: int | None = None,
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

    `acq_time_ns` is the device-clock mid-shutter time of the RGB frame (also
    the depth's mid-shutter time, since Sync only emitted them together);
    falls back to host wall-clock when not provided. `sync_interval_ns` is
    the actual RGB↔depth gap as measured by Sync — downstream consumers can
    filter on it; omitted when no sync metadata is available.

    When `depth` is None the top-level `depth` key is omitted so consumers can
    cheaply check `"depth" in body` — no None/null on the wire. Calibration
    travels on its own topic, not in the body.
    """
    pub_ts = time.time_ns()
    acq_ts = acq_time_ns if acq_time_ns is not None else pub_ts
    header: dict = {
        "acq_time": acq_ts,
        "pub_time": pub_ts,
        "sequence": seq,
        "frame_id": instance,
        "machine_id": machine_id,
    }
    if sync_interval_ns is not None:
        header["sync_interval_ns"] = sync_interval_ns
    body: dict = {
        "header": header,
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


def _fetch_calibration(device, width: int, height: int) -> dict | None:
    """Read the OAK's RGB intrinsics + distortion at the published resolution.

    `getCameraIntrinsics(CAM_A, w, h)` returns the 3x3 matrix scaled to the
    requested output size (intrinsics scale with resolution — `cx, cy, fx, fy`
    all change). Depth is hardware-aligned to CAM_A at the same resolution
    (see `stereo.setDepthAlign(CAM_A)` + `stereo.setOutputSize(w, h)`), so this
    one calibration block applies to BOTH RGB and depth planes.

    Returns None if the device EEPROM has no calibration data (rare on
    factory OAKs, possible on dev boards) — callers omit the field rather
    than blocking the pipeline.
    """
    try:
        calib = device.readCalibrationOrDefault()
        K = calib.getCameraIntrinsics(dai.CameraBoardSocket.CAM_A, width, height)
        dist = calib.getDistortionCoefficients(dai.CameraBoardSocket.CAM_A)
        model = calib.getDistortionModel(dai.CameraBoardSocket.CAM_A)
        # CameraModel enum → lowercase string for wire format consistency
        # (matches kornia / opencv-python conventions).
        model_name = str(model.name).lower()
        return {
            "model": model_name,
            "width": width,
            "height": height,
            "fx": float(K[0][0]),
            "fy": float(K[1][1]),
            "cx": float(K[0][2]),
            "cy": float(K[1][2]),
            "distortion": [float(x) for x in dist],
        }
    except Exception as exc:
        log.warning("Failed to read OAK calibration: %s — calibration block omitted", exc)
        return None


class OakCameraNode:
    name = "oak-camera"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)

        # compressed → global, network-recordable JPEG RGB + PNG-16 depth.
        # rgbd → local SHM (zero-copy, raw RGBA + depth16) for downstream
        # perception nodes. calibration → global, separate static-data topic
        # (re-published periodically). All go through the SDK so SHM
        # auto-negotiates when consumers are on the same machine.
        self._compressed_pub = ctx.publisher_cbor("compressed", schema_uri="bubbaloop://compressed/v3")
        self._rgbd_pub = ctx.publisher_cbor("rgbd", local=True, schema_uri="bubbaloop://rgbd/v2")
        self._calibration_pub = ctx.publisher_cbor(
            "calibration", schema_uri="bubbaloop://calibration/v1"
        )
        self._seq = 0
        self._calibration: dict | None = None  # filled in run() after pipeline.start()
        self._last_calibration_pub_mono = 0.0

        # JPEG encoder reused across frames — kornia_rs ImageEncoder wraps
        # libjpeg-turbo via Rust. set_quality is sticky across encode() calls.
        self._jpeg_encoder = kr.ImageEncoder()
        self._jpeg_encoder.set_quality(self._cfg["jpeg_quality"])

        # Pre-allocated scratch buffers. RGB feeds the JPEG encoder
        # (kornia_rs requires contiguous arrays); RGBA is the SHM payload.
        # Together: ~6.6 MB at 1280×720, avoids ~200 MB/s of per-frame allocs at 30 fps.
        self._rgb_buf = np.empty(
            (self._cfg["height"], self._cfg["width"], 3), dtype=np.uint8,
        )
        self._rgba_buf = np.empty(
            (self._cfg["height"], self._cfg["width"], 4), dtype=np.uint8,
        )
        self._rgba_buf[:, :, 3] = 255  # alpha — set once, never touched per frame

        log.info("Configured: %s", self._cfg)
        log.info("compressed  → %s", ctx.topic("compressed"))
        log.info("rgbd (SHM)  → %s", ctx.local_topic("rgbd"))
        log.info("calibration → %s (every %.1fs)",
                 ctx.topic("calibration"), self._cfg["calibration_publish_interval_secs"])

    def _build_pipeline(self, pipeline: dai.Pipeline):
        """Build the OAK pipeline.

        Returns a single output queue. When stereo is enabled, the queue
        carries ``MessageGroup`` items with ``rgb`` and ``depth`` keys, paired
        by ``dai.node.Sync`` on the device's Leon coprocessor. When stereo is
        disabled, the queue carries plain ``ImgFrame`` (RGB only).
        """
        w = self._cfg["width"]
        h = self._cfg["height"]
        fps = self._cfg["fps"]

        cam_rgb = pipeline.create(dai.node.Camera).build(dai.CameraBoardSocket.CAM_A)
        rgb_out = cam_rgb.requestOutput((w, h), type=dai.ImgFrame.Type.BGR888i, fps=fps)

        depth_out = None
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
                depth_out = stereo.depth
                log.info("Stereo depth enabled, aligned to CAM_A, %dx%d", w, h)
            except Exception as exc:
                log.warning("Stereo depth unavailable (%s) — RGB-only mode", exc)
                depth_out = None

        if depth_out is None:
            # RGB-only — no need for Sync, return the raw queue.
            return rgb_out.createOutputQueue(maxSize=4, blocking=False), False

        # RGB + depth — sync them on the Leon coprocessor. Sync emits a
        # MessageGroup whose `rgb` and `depth` mid-shutter timestamps fall
        # within `sync_threshold_ms`. With sync_attempts=-1 (default), unsynced
        # frames are dropped silently; downstream sees only matched pairs.
        sync = pipeline.create(dai.node.Sync)
        sync.setSyncThreshold(timedelta(milliseconds=self._cfg["sync_threshold_ms"]))
        sync.setSyncAttempts(self._cfg["sync_attempts"])
        rgb_out.link(sync.inputs["rgb"])
        depth_out.link(sync.inputs["depth"])
        log.info(
            "Sync node: threshold=%dms attempts=%d (Leon coprocessor)",
            self._cfg["sync_threshold_ms"], self._cfg["sync_attempts"],
        )
        return sync.out.createOutputQueue(maxSize=4, blocking=False), True

    def run(self) -> None:
        ctx = self._ctx
        cfg = self._cfg
        instance = ctx.instance_name or self.name

        with dai.Pipeline() as pipeline:
            q_out, synced = self._build_pipeline(pipeline)
            pipeline.start()
            log.info("Pipeline started. Streaming at %.1f fps (synced=%s)", cfg["fps"], synced)

            # Calibration is static — fetch once after start() (Pipeline binds
            # the device on start), cache for the rest of the session, and
            # publish on its own topic at low rate.
            self._calibration = _fetch_calibration(
                pipeline.getDefaultDevice(), cfg["width"], cfg["height"]
            )
            if self._calibration is not None:
                log.info(
                    "Calibration: model=%s fx=%.2f fy=%.2f cx=%.2f cy=%.2f distortion=%d coeffs",
                    self._calibration["model"],
                    self._calibration["fx"], self._calibration["fy"],
                    self._calibration["cx"], self._calibration["cy"],
                    len(self._calibration["distortion"]),
                )
                # Publish once immediately so any subscriber that's already up
                # gets it before the first frame.
                self._calibration_pub.put(self._calibration)
                self._last_calibration_pub_mono = time.monotonic()

            while not ctx.is_shutdown():
                msg = q_out.get()
                if msg is None:
                    continue

                rgb_msg, depth_msg, sync_interval_ns = self._unpack_message(msg, synced)

                # Mid-shutter device clock — physically meaningful capture time.
                # When Sync paired the messages, depth has the same mid-shutter
                # time within `sync_threshold_ms`.
                acq_td = rgb_msg.getTimestamp(dai.CameraExposureOffset.MIDDLE)
                acq_time_ns = int(acq_td.total_seconds() * 1e9)

                bgr = rgb_msg.getCvFrame()
                h, w = bgr.shape[:2]

                depth_frame: np.ndarray | None = None
                depth_bytes: bytes | None = None
                depth_w = 0
                depth_h = 0
                if depth_msg is not None:
                    depth_frame = np.ascontiguousarray(
                        depth_msg.getFrame().astype(np.uint16, copy=False)
                    )
                    depth_h, depth_w = depth_frame.shape
                    depth_bytes = depth_frame.tobytes()

                # BGR (depthai) → RGB (for kornia_rs JPEG encode + the RGB
                # plane of the SHM RGBA payload). One channel-reverse, two
                # consumers — no redundant work.
                np.copyto(self._rgb_buf, bgr[:, :, ::-1])
                self._rgba_buf[:, :, :3] = self._rgb_buf  # alpha already set at init

                body = _rgbd_body(
                    self._rgba_buf.tobytes(), w, h,
                    instance, ctx.machine_id, self._seq,
                    acq_time_ns=acq_time_ns,
                    sync_interval_ns=sync_interval_ns,
                    depth=depth_bytes,
                    depth_width=depth_w,
                    depth_height=depth_h,
                )
                self._rgbd_pub.put(body)
                self._seq += 1

                if self._seq % cfg["jpeg_every_n"] == 0:
                    self._publish_compressed(
                        w, h, depth_frame, depth_w, depth_h, instance, ctx.machine_id,
                        acq_time_ns=acq_time_ns,
                        sync_interval_ns=sync_interval_ns,
                    )

                self._maybe_republish_calibration()

            log.info("Shutdown requested — stopping")

        self._rgbd_pub.undeclare()
        self._compressed_pub.undeclare()
        self._calibration_pub.undeclare()

    @staticmethod
    def _unpack_message(msg, synced: bool):
        """Normalize the queue output to (rgb_msg, depth_msg, sync_interval_ns).

        Synced path: msg is a `MessageGroup` keyed by "rgb"/"depth" — depth may
        still be missing if `sync_attempts > 0` and the depth side timed out.
        Unsynced path: msg is the bare RGB `ImgFrame`.
        """
        if not synced:
            return msg, None, None
        rgb_msg = msg["rgb"]
        depth_msg = None
        if "depth" in msg.getMessageNames():
            depth_msg = msg["depth"]
        return rgb_msg, depth_msg, int(msg.getIntervalNs())

    def _maybe_republish_calibration(self) -> None:
        """Re-publish calibration on its topic if the configured interval has elapsed.

        Called from the main loop so we don't need a separate timer thread.
        Publishing more often than every-frame is wasteful (static data); less
        often than the configured interval delays late-joining subscribers.
        """
        if self._calibration is None:
            return
        now = time.monotonic()
        if now - self._last_calibration_pub_mono >= self._cfg["calibration_publish_interval_secs"]:
            self._calibration_pub.put(self._calibration)
            self._last_calibration_pub_mono = now

    def _publish_compressed(
        self,
        width: int,
        height: int,
        depth_frame: np.ndarray | None,
        depth_w: int,
        depth_h: int,
        instance: str,
        machine_id: str,
        acq_time_ns: int | None = None,
        sync_interval_ns: int | None = None,
    ) -> None:
        """Encode + publish JPEG RGB + PNG-16 depth to ``compressed``.

        Reads from ``self._rgb_buf`` (already populated by the run loop's
        BGR→RGB copy) — kornia_rs ImageEncoder requires contiguous arrays.
        PNG-16 is lossless on uint16 (the only safe choice for depth: JPEG's
        DCT catastrophically smears across object-boundary discontinuities).

        Body shape mirrors ``rgbd`` exactly so a single decoder handles both
        topics — only ``rgb["encoding"]`` (``"jpeg"`` vs ``"rgba8"``) and
        ``depth["encoding"]`` (``"png16_mm"`` vs ``"depth16_mm"``) differ.
        Calibration is on its own topic — not embedded here.
        """
        jpeg_bytes = self._jpeg_encoder.encode(self._rgb_buf)
        pub_ts = time.time_ns()
        acq_ts = acq_time_ns if acq_time_ns is not None else pub_ts
        header: dict = {
            "acq_time": acq_ts,
            "pub_time": pub_ts,
            "sequence": self._seq,
            "frame_id": instance,
            "machine_id": machine_id,
        }
        if sync_interval_ns is not None:
            header["sync_interval_ns"] = sync_interval_ns
        body: dict = {
            "header": header,
            "rgb": {
                "width": width,
                "height": height,
                "encoding": "jpeg",
                "data": jpeg_bytes,
            },
        }
        if depth_frame is not None:
            png_bytes = _encode_png16(depth_frame, self._cfg["depth_png_compression"])
            body["depth"] = {
                "width": depth_w,
                "height": depth_h,
                "encoding": "png16_mm",
                "data": png_bytes,
            }
        self._compressed_pub.put(body)


def _encode_png16(depth_u16: np.ndarray, compress_level: int) -> bytes:
    """Encode a uint16 depth array as PNG-16 bytes via Pillow.

    kornia_rs has no in-memory PNG-16 encoder. As of 0.1.10 (and on main as
    of 2026-04) ``kornia-py/src/io/png.rs`` exposes ``read/write_image_png_u16``
    (file-based) and ``decode_image_png_u16`` (in-memory) — but NOT an
    ``encode_image_png_u16`` counterpart. JPEG has the symmetric pair via
    ``ImageEncoder``; PNG does not. Pillow fills the gap; ``Image.fromarray``
    auto-detects ``mode="I;16"`` from uint16 dtype on Pillow 10+.

    Swap path: if/when kornia_rs adds ``encode_image_png_u16(image, level) -> bytes``
    (watch ``kornia-py/src/io/png.rs``), replace this body with the kr call
    and drop Pillow from pixi.toml.
    """
    img = Image.fromarray(depth_u16)
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=compress_level)
    return buf.getvalue()


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(OakCameraNode)
