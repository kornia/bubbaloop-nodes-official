"""video-embedder — V-JEPA 2.1 clip embeddings from a camera frame stream.

Subscribes to a local-SHM camera topic, maintains a ring buffer of the last
``clip_frames`` frames, and on every ``1 / target_hz`` seconds packs them as a
``(1, 3, T, 384, 384)`` clip tensor, runs a forward pass through V-JEPA 2.1,
mean-pools the patch features, and publishes the resulting vector as JSON.

V-JEPA 2.1 weights are not on HuggingFace yet (as of 2026-04), so we load via
``torch.hub.load('facebookresearch/vjepa2', '<entrypoint>', trust_repo=True)``.
First run downloads the model into the torch hub cache.

Accepts two wire formats on the input topic:

- **oak-camera rgbd** — body has ``rgb`` sub-dict with ``width/height/data``.
- **legacy RGBA** — body has ``width/height/data`` at the top level
  (e.g. rtsp-camera's raw image).

Both are RGBA bytes; the node resizes each frame to 384x384, ImageNet-
normalizes, and pushes into the ring.
"""

from __future__ import annotations

import argparse
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Optional

import numpy as np
import torch
from PIL import Image
from bubbaloop_sdk import NodeContext, run_node

log = logging.getLogger("video-embedder")

_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")
_RESIZE = (384, 384)
_IMAGENET_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
_IMAGENET_STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)


def _validate(cfg: dict) -> dict:
    name = cfg.get("name")
    if not name or not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name required, must match ^[a-zA-Z0-9/_\\-\\.]+$: {name!r}")
    if "input_topic" not in cfg:
        raise ValueError("config.input_topic required (upstream camera raw/rgbd topic suffix)")
    clip_frames = int(cfg.get("clip_frames", 16))
    if not 2 <= clip_frames <= 64:
        raise ValueError("config.clip_frames must be in [2, 64]")
    target_hz = float(cfg.get("target_hz", 0.5))
    if not 0.01 <= target_hz <= 30.0:
        raise ValueError("config.target_hz must be in [0.01, 30]")
    return {
        "name": name,
        "input_topic": cfg["input_topic"],
        "model": cfg.get("model", "vjepa2_1_vit_base_384"),
        "device": cfg.get("device", "cuda"),
        "clip_frames": clip_frames,
        "target_hz": target_hz,
    }


def _extract_rgba(msg) -> Optional[tuple[bytes, int, int]]:
    """Pull (rgba_bytes, width, height) from either oak-camera rgbd or legacy RGBA.

    Returns None if the message doesn't look like either format (e.g. a control
    message that slipped through).
    """
    rgb = getattr(msg, "rgb", None)
    if rgb is not None:
        # oak-camera layout: body.rgb.{width, height, data}
        return bytes(rgb.data), int(rgb.width), int(rgb.height)
    data = getattr(msg, "data", None)
    width = getattr(msg, "width", None)
    height = getattr(msg, "height", None)
    if data is not None and width is not None and height is not None:
        return bytes(data), int(width), int(height)
    return None


def preprocess_frame(rgba_bytes: bytes, width: int, height: int) -> torch.Tensor:
    """RGBA bytes -> (3, 384, 384) ImageNet-normalized float32 tensor."""
    image = Image.frombytes("RGBA", (width, height), rgba_bytes)
    image = image.convert("RGB").resize(_RESIZE, Image.BILINEAR)
    arr = np.array(image, dtype=np.float32) / 255.0
    tensor = torch.from_numpy(arr).permute(2, 0, 1)  # HWC -> CHW
    return (tensor - _IMAGENET_MEAN) / _IMAGENET_STD


class VJepa21Model:
    """V-JEPA 2.1 encoder loaded via torch.hub from facebookresearch/vjepa2.

    First instantiation triggers a ~hundreds-of-MB download into the torch hub
    cache. Subsequent runs reuse it.
    """

    def __init__(self, entrypoint: str, device: str = "cuda"):
        self.entrypoint = entrypoint
        self.device = device
        log.info("Loading V-JEPA 2.1 via torch.hub: %s on %s...", entrypoint, device)
        t0 = time.monotonic()
        loaded = torch.hub.load(
            "facebookresearch/vjepa2", entrypoint, trust_repo=True,
        )
        # vjepa2 torch.hub entrypoints historically return (encoder, predictor).
        # For embedding extraction we only need the encoder.
        self._model = loaded[0] if isinstance(loaded, tuple) else loaded
        self._model.to(device)
        self._model.train(False)  # inference mode: no dropout, frozen BN
        log.info("Model loaded in %.1fs", time.monotonic() - t0)
        self.embedding_dim: Optional[int] = None

    @torch.inference_mode()
    def encode(self, clip: torch.Tensor) -> torch.Tensor:
        """Run clip through V-JEPA 2.1, return a single pooled embedding vector.

        Args:
            clip: Tensor of shape (1, 3, T, 384, 384), ImageNet-normalized.

        Returns:
            1D tensor of shape (embedding_dim,) -- mean of patch features.
        """
        clip = clip.to(self.device)
        out = self._model(clip)
        # Output is either (B, N_tokens, D) tensor or an object with .last_hidden_state.
        tokens = getattr(out, "last_hidden_state", out)
        embedding = tokens.mean(dim=1).squeeze(0)  # (D,)
        if self.embedding_dim is None:
            self.embedding_dim = int(embedding.shape[0])
        return embedding.cpu()


class FrameRing:
    """Thread-safe bounded deque of preprocessed frames (latest-N-wins)."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._buf: Deque[torch.Tensor] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def push(self, frame: torch.Tensor) -> None:
        with self._lock:
            self._buf.append(frame)

    def snapshot(self) -> Optional[torch.Tensor]:
        """Return a (1, 3, T, H, W) clip tensor if the ring is full, else None."""
        with self._lock:
            if len(self._buf) < self.capacity:
                return None
            frames = list(self._buf)  # each (3, H, W)
        stacked = torch.stack(frames, dim=1)  # (3, T, H, W)
        return stacked.unsqueeze(0)  # (1, 3, T, H, W)


class VideoEmbedderNode:
    name = "video-embedder"

    def __init__(self, ctx: NodeContext, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)
        self._ring = FrameRing(self._cfg["clip_frames"])
        self._model = VJepa21Model(self._cfg["model"], self._cfg["device"])
        self._interval = 1.0 / self._cfg["target_hz"]
        self._seq = 0

        self._sub = ctx.subscribe(self._cfg["input_topic"], local=True)
        self._pub = ctx.publisher_json("embeddings")

        log.info(
            "Ready: input=%s model=%s device=%s clip_frames=%d target_hz=%.2f",
            self._cfg["input_topic"],
            self._cfg["model"],
            self._cfg["device"],
            self._cfg["clip_frames"],
            self._cfg["target_hz"],
        )

    def run(self) -> None:
        receive_thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="receive",
        )
        receive_thread.start()
        try:
            self._inference_loop()
        finally:
            self._sub.undeclare()
            self._pub.undeclare()
            receive_thread.join(timeout=2.0)
            log.info("Shutdown complete (published %d clip embeddings)", self._seq)

    def _receive_loop(self) -> None:
        """Decode each sample, preprocess, push into the ring."""
        for env in self._sub:
            if self._ctx.is_shutdown():
                return
            body = getattr(env, "body", env)
            if isinstance(body, (bytes, bytearray)):
                continue
            rgba = _extract_rgba(body)
            if rgba is None:
                continue
            data, w, h = rgba
            try:
                frame = preprocess_frame(data, w, h)
            except Exception as exc:
                log.warning("preprocess failed: %s", exc)
                continue
            self._ring.push(frame)

    def _inference_loop(self) -> None:
        """Every ``1/target_hz`` seconds, snapshot the ring, infer, publish."""
        next_run = time.monotonic()
        while not self._ctx.is_shutdown():
            now = time.monotonic()
            if now < next_run:
                self._ctx._shutdown.wait(timeout=next_run - now)
                continue
            clip = self._ring.snapshot()
            if clip is None:
                # Not enough frames yet; try again shortly.
                self._ctx._shutdown.wait(timeout=0.1)
                continue

            next_run = time.monotonic() + self._interval
            t0 = time.monotonic()
            embedding = self._model.encode(clip)
            inference_ms = (time.monotonic() - t0) * 1000

            self._pub.put({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "embedding": embedding.tolist(),
                "dim": self._model.embedding_dim or int(embedding.shape[0]),
                "model": self._cfg["model"],
                "clip_frames": self._cfg["clip_frames"],
                "resolution": _RESIZE[0],
                "inference_ms": round(inference_ms, 1),
            })
            self._seq += 1
            log.info(
                "seq=%d dim=%d infer=%.1fms",
                self._seq, self._model.embedding_dim, inference_ms,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.parse_known_args()  # run_node re-parses internally
    run_node(VideoEmbedderNode)
