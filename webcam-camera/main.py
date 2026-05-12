#!/usr/bin/env python3
"""webcam-camera — publishes USB/V4L2 webcam frames as compressed JPEG.

Topic:
- ``{name}/compressed`` — global, CBOR envelope, body = {width, height, encoding:"jpeg", data}.
"""

from __future__ import annotations

import logging
import re

import cv2

log = logging.getLogger("webcam-camera")

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")


def _validate(cfg: dict) -> dict:
    name = cfg.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("config.name is required")
    if not _NAME_RE.match(name):
        raise ValueError("config.name must match ^[a-zA-Z0-9/_\\-\\.]+$ (got {!r})".format(name))

    device = cfg.get("device", 0)
    if isinstance(device, str):
        if ".." in device or "\x00" in device:
            raise ValueError("config.device string must not contain '..' or NUL")
    elif isinstance(device, int):
        if device < 0:
            raise ValueError("config.device int must be >= 0")
    else:
        raise ValueError("config.device must be an int or a string path")

    width = int(cfg.get("width", 1280))
    height = int(cfg.get("height", 720))
    if not (16 <= width <= 4096) or width % 16:
        raise ValueError("width must be a multiple of 16 in [16, 4096]")
    if not (16 <= height <= 4096) or height % 16:
        raise ValueError("height must be a multiple of 16 in [16, 4096]")

    fps = float(cfg.get("fps", 30))
    if not 1.0 <= fps <= 120.0:
        raise ValueError("fps must be in [1, 120]")

    jpeg_quality = int(cfg.get("jpeg_quality", 80))
    if not 1 <= jpeg_quality <= 100:
        raise ValueError("jpeg_quality must be in [1, 100]")

    return {
        "name": name,
        "device": device,
        "width": width,
        "height": height,
        "fps": fps,
        "jpeg_quality": jpeg_quality,
        "warn_on_param_mismatch": bool(cfg.get("warn_on_param_mismatch", True)),
    }


class WebcamCameraNode:
    name = "webcam-camera"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)
        self._compressed_pub = ctx.publisher_cbor(
            "compressed", schema_uri="bubbaloop://compressed/v1"
        )
        self._seq = 0
        log.info("Configured: %s", self._cfg)
        log.info("compressed → %s", ctx.topic("compressed"))

    def _open_capture(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self._cfg["device"])
        if not cap.isOpened():
            raise RuntimeError(f"failed to open device {self._cfg['device']!r}")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cfg["height"])
        cap.set(cv2.CAP_PROP_FPS, self._cfg["fps"])
        if self._cfg["warn_on_param_mismatch"]:
            actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            if (actual_w, actual_h) != (self._cfg["width"], self._cfg["height"]):
                log.warning(
                    "driver chose %dx%d (requested %dx%d)",
                    actual_w, actual_h, self._cfg["width"], self._cfg["height"],
                )
            if abs(actual_fps - self._cfg["fps"]) > 1:
                log.warning(
                    "driver chose fps=%.1f (requested %.1f)", actual_fps, self._cfg["fps"]
                )
        return cap

    def _capture_loop(self, cap: cv2.VideoCapture) -> None:
        consecutive_failures = 0
        max_failures = 5

        while not self._ctx.is_shutdown():
            ok, bgr = cap.read()
            if not ok:
                consecutive_failures += 1
                log.warning("cap.read() failed (%d/%d)", consecutive_failures, max_failures)
                if consecutive_failures >= max_failures:
                    raise RuntimeError(
                        f"camera read failed {consecutive_failures} times in a row — "
                        "daemon will restart the service"
                    )
                continue
            consecutive_failures = 0

            h, w = bgr.shape[:2]
            ok2, jpeg = cv2.imencode(
                ".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self._cfg["jpeg_quality"]]
            )
            if not ok2:
                log.warning("imencode failed on frame %d — skipping", self._seq)
                continue

            self._compressed_pub.put(
                {"width": w, "height": h, "encoding": "jpeg", "data": jpeg.tobytes()}
            )
            self._seq += 1

    def run(self) -> None:
        cap = self._open_capture()
        try:
            self._capture_loop(cap)
        finally:
            cap.release()
            self._compressed_pub.undeclare()
            log.info("Shutdown complete")


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(WebcamCameraNode)
