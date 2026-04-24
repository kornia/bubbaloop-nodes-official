"""jepa-video-embedder — V-JEPA 2.1 clip embeddings from a camera frame stream.

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
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Optional

import numpy as np
import torch
import torch.nn.functional as F
from bubbaloop_sdk import NodeContext, run_node

# Our clip shape is fixed at (1, 3, clip_frames, 384, 384). cuDNN's autotuner
# picks the fastest conv algorithm on the first forward pass and caches it for
# every subsequent identically-shaped call. 5-15% win, zero risk.
torch.backends.cudnn.benchmark = True

log = logging.getLogger("jepa-video-embedder")

# facebookresearch/vjepa2's hub config (src/hub/backbones.py) currently ships
# with a dev placeholder URL: VJEPA_BASE_URL = "http://localhost:8300".
# We rewrite it to Meta's public CDN the first time we see it in the torch
# hub cache; subsequent runs are no-ops. Remove once upstream issue #137
# (HF weight hosting) lands or they fix the placeholder.
_VJEPA_HUB_CACHED_CONFIG = os.path.expanduser(
    "~/.cache/torch/hub/facebookresearch_vjepa2_main/src/hub/backbones.py"
)
_VJEPA_BAD_URL = 'VJEPA_BASE_URL = "http://localhost:8300"'
_VJEPA_GOOD_URL = 'VJEPA_BASE_URL = "https://dl.fbaipublicfiles.com/vjepa2"'


def _patch_vjepa2_hub_url() -> bool:
    """Rewrite the dev placeholder URL in the cached hubconf. Idempotent.

    Returns True iff the file was patched this call. Returns False if the
    cached repo isn't present yet (first run — the caller should invoke
    torch.hub.load once to trigger the clone, then retry).
    """
    if not os.path.exists(_VJEPA_HUB_CACHED_CONFIG):
        return False
    with open(_VJEPA_HUB_CACHED_CONFIG) as f:
        src = f.read()
    if _VJEPA_BAD_URL not in src:
        return False
    with open(_VJEPA_HUB_CACHED_CONFIG, "w") as f:
        f.write(src.replace(_VJEPA_BAD_URL, _VJEPA_GOOD_URL))
    log.warning("Patched facebookresearch/vjepa2 hubconf: localhost:8300 → public CDN")
    return True

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
    precision = cfg.get("precision", "fp16")
    if precision not in ("fp16", "fp32"):
        raise ValueError("config.precision must be 'fp16' or 'fp32'")
    return {
        "name": name,
        "input_topic": cfg["input_topic"],
        "model": cfg.get("model", "vjepa2_1_vit_base_384"),
        "device": cfg.get("device", "cuda"),
        "clip_frames": clip_frames,
        "target_hz": target_hz,
        "precision": precision,
        "compile": bool(cfg.get("compile", True)),
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
    """RGBA bytes -> (3, 384, 384) ImageNet-normalized float32 tensor.

    Pure numpy + torch pipeline (no PIL). Steps: frombuffer the raw RGBA bytes
    into a (H, W, 4) uint8 view, drop alpha, permute to (C, H, W), cast to
    float, resize bilinearly with F.interpolate, ImageNet-normalize.
    """
    arr = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape(height, width, 4)
    rgb = torch.from_numpy(arr[:, :, :3].copy())  # (H, W, 3) uint8 — copy so torch owns it
    chw = rgb.permute(2, 0, 1).unsqueeze(0).float() / 255.0  # (1, 3, H, W) float32 in [0,1]
    resized = F.interpolate(chw, size=_RESIZE, mode="bilinear", align_corners=False)
    return (resized.squeeze(0) - _IMAGENET_MEAN) / _IMAGENET_STD


class VJepa21Model:
    """V-JEPA 2.1 encoder loaded via torch.hub from facebookresearch/vjepa2.

    First instantiation triggers a ~hundreds-of-MB download into the torch hub
    cache. Subsequent runs reuse it.
    """

    def __init__(
        self,
        entrypoint: str,
        device: str = "cuda",
        precision: str = "fp16",
        compile_model: bool = True,
    ):
        self.entrypoint = entrypoint
        self.device = device
        # "fp16" = autocast activations to float16 on cuda (weights stay fp32,
        # tensor-core path active on Ampere+). "fp32" = no autocast.
        self.precision = precision
        self.compile_model = compile_model
        log.info(
            "Loading V-JEPA 2.1 via torch.hub: %s on %s (precision=%s, compile=%s)...",
            entrypoint, device, precision, compile_model,
        )
        t0 = time.monotonic()

        # Preemptively patch if the repo is already cached from a previous run.
        _patch_vjepa2_hub_url()
        try:
            loaded = torch.hub.load(
                "facebookresearch/vjepa2", entrypoint, trust_repo=True,
            )
        except Exception as first_exc:
            # First run: hub just cloned the repo but hit the placeholder URL
            # during state-dict download. Patch the now-cached config and retry.
            if not _patch_vjepa2_hub_url():
                raise
            log.warning("First torch.hub load failed (%s); retrying with patched URL", first_exc)
            loaded = torch.hub.load(
                "facebookresearch/vjepa2", entrypoint, trust_repo=True,
            )
        # vjepa2 torch.hub entrypoints historically return (encoder, predictor).
        # For embedding extraction we only need the encoder.
        self._model = loaded[0] if isinstance(loaded, tuple) else loaded
        self._model.to(device)
        self._model.train(False)  # inference mode: no dropout, frozen BN
        log.info("Model loaded in %.1fs", time.monotonic() - t0)

        # torch.compile traces the forward graph on the first call and caches a
        # fused/optimized version. First real inference is slow (compile cost,
        # ~20-60s on Orin); steady-state is typically 15-30% faster. Graceful
        # fallback to eager if Triton / TorchInductor isn't available on this
        # platform (common on older Jetson Torch wheels).
        if compile_model and device.startswith("cuda"):
            try:
                self._model = torch.compile(self._model, mode="default", dynamic=False)
                log.info("torch.compile enabled (mode=default)")
            except Exception as exc:
                log.warning("torch.compile unavailable, falling back to eager: %s", exc)

        self.embedding_dim: Optional[int] = None

    @torch.inference_mode()
    def encode(self, clip: torch.Tensor) -> torch.Tensor:
        """Run clip through V-JEPA 2.1, return a single pooled embedding vector.

        Args:
            clip: Tensor of shape (1, 3, T, 384, 384), ImageNet-normalized.

        Returns:
            1D tensor of shape (embedding_dim,) -- mean of patch features.
        """
        clip = clip.to(self.device, non_blocking=True)
        use_autocast = self.precision == "fp16" and self.device.startswith("cuda")
        if use_autocast:
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                out = self._model(clip)
        else:
            out = self._model(clip)
        # Output is either (B, N_tokens, D) tensor or an object with .last_hidden_state.
        tokens = getattr(out, "last_hidden_state", out)
        # Cast back to fp32 for the pool + cpu transfer (no-op if already fp32).
        embedding = tokens.float().mean(dim=1).squeeze(0)  # (D,)
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


class JepaVideoEmbedderNode:
    name = "jepa-video-embedder"

    def __init__(self, ctx: NodeContext, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)
        self._ring = FrameRing(self._cfg["clip_frames"])
        self._model = VJepa21Model(
            self._cfg["model"],
            self._cfg["device"],
            self._cfg["precision"],
            self._cfg["compile"],
        )
        self._interval = 1.0 / self._cfg["target_hz"]
        self._seq = 0

        self._sub = ctx.subscribe(self._cfg["input_topic"], local=True)
        self._pub = ctx.publisher_json("embeddings")

        log.info(
            "Ready: input=%s model=%s device=%s precision=%s compile=%s clip_frames=%d target_hz=%.2f",
            self._cfg["input_topic"],
            self._cfg["model"],
            self._cfg["device"],
            self._cfg["precision"],
            self._cfg["compile"],
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
    run_node(JepaVideoEmbedderNode)
