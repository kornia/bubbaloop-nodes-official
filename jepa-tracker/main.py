"""jepa-tracker -- zero-training object detection + tracking from V-JEPA 2.1 features.

Pipeline (every 1/target_hz seconds, on the most recent N-frame clip):

    forward pass            -> dense tokens X in R^(T, H, W, D)         [V-JEPA 2.1]
    temporal variance       -> per-token "moving" saliency               [pure ops on X]
    feature-aware union-find-> labelled blobs in (H, W) at token grid    [zero-train]
    per-blob descriptors    -> mask, mean signature, area, velocity      [pure ops on X]
    Hungarian re-ID         -> assign track IDs across clips             [scipy]
    overlay render          -> blob-colored JPEG via torchvision.io       [GPU encode]
    publish                 -> JSON tracks + CBOR{jpeg} overlay over Zenoh

Image ops use torch + kornia + torchvision.io. No opencv, no PIL. JPEG encoding
goes through torchvision.io.encode_jpeg (libjpeg-turbo on Jetson).
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
from torchvision.io import encode_jpeg
from scipy.optimize import linear_sum_assignment
from bubbaloop_sdk import NodeContext, run_node

# Fixed clip shape lets cuDNN cache its best conv plan after the first call.
torch.backends.cudnn.benchmark = True

log = logging.getLogger("jepa-tracker")

# --- upstream-bug workaround (same as jepa-video-embedder) -------------------
_VJEPA_HUB_CACHED_CONFIG = os.path.expanduser(
    "~/.cache/torch/hub/facebookresearch_vjepa2_main/src/hub/backbones.py"
)
_VJEPA_BAD_URL = 'VJEPA_BASE_URL = "http://localhost:8300"'
_VJEPA_GOOD_URL = 'VJEPA_BASE_URL = "https://dl.fbaipublicfiles.com/vjepa2"'


def _patch_vjepa2_hub_url() -> bool:
    if not os.path.exists(_VJEPA_HUB_CACHED_CONFIG):
        return False
    with open(_VJEPA_HUB_CACHED_CONFIG) as f:
        src = f.read()
    if _VJEPA_BAD_URL not in src:
        return False
    with open(_VJEPA_HUB_CACHED_CONFIG, "w") as f:
        f.write(src.replace(_VJEPA_BAD_URL, _VJEPA_GOOD_URL))
    log.warning("Patched facebookresearch/vjepa2 hubconf: localhost:8300 -> public CDN")
    return True


# --- preprocessing & shape helpers -------------------------------------------
_NAME_RE = re.compile(r"^[a-zA-Z0-9/_\-\.]+$")
_RESIZE = (384, 384)
_PATCH = 16  # ViT-B/16 spatial patch
_GRID_HW = (_RESIZE[0] // _PATCH, _RESIZE[1] // _PATCH)  # (24, 24)
_IMAGENET_MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
_IMAGENET_STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)


def preprocess_frame(rgba_bytes: bytes, w: int, h: int) -> tuple[torch.Tensor, torch.Tensor]:
    """RGBA bytes -> (raw_uint8 (3, 384, 384), normalized_float (3, 384, 384)).

    Both share the same resize so the overlay aligns 1:1 with the model input.
    """
    arr = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape(h, w, 4)
    rgb = torch.from_numpy(arr[:, :, :3].copy())               # (h, w, 3) uint8
    chw = rgb.permute(2, 0, 1).unsqueeze(0).float() / 255.0    # (1, 3, h, w) [0,1]
    resized = F.interpolate(chw, size=_RESIZE, mode="bilinear", align_corners=False)
    raw_uint8 = (resized.squeeze(0) * 255.0).clamp(0, 255).byte()  # (3, 384, 384)
    norm = (resized.squeeze(0) - _IMAGENET_MEAN) / _IMAGENET_STD
    return raw_uint8, norm


def _extract_rgba(msg) -> Optional[tuple[bytes, int, int]]:
    """oak-camera (body.rgb.{...}) or legacy RGBA (body.{...})."""
    rgb = getattr(msg, "rgb", None)
    if rgb is not None:
        return bytes(rgb.data), int(rgb.width), int(rgb.height)
    data = getattr(msg, "data", None)
    width = getattr(msg, "width", None)
    height = getattr(msg, "height", None)
    if data is not None and width is not None and height is not None:
        return bytes(data), int(width), int(height)
    return None


# --- V-JEPA 2.1 dense feature extractor --------------------------------------
class VJepa21Dense:
    """Loads V-JEPA 2.1 and exposes a forward that returns dense token features."""

    def __init__(self, entrypoint: str, device: str = "cuda", precision: str = "fp16"):
        self.entrypoint = entrypoint
        self.device = device
        self.precision = precision
        log.info("Loading V-JEPA 2.1 (%s, device=%s, precision=%s)", entrypoint, device, precision)
        t0 = time.monotonic()
        _patch_vjepa2_hub_url()
        try:
            loaded = torch.hub.load("facebookresearch/vjepa2", entrypoint, trust_repo=True)
        except Exception as exc:
            if not _patch_vjepa2_hub_url():
                raise
            log.warning("First load failed (%s); retrying after URL patch", exc)
            loaded = torch.hub.load("facebookresearch/vjepa2", entrypoint, trust_repo=True)
        self._model = loaded[0] if isinstance(loaded, tuple) else loaded
        self._model.to(device)
        self._model.train(False)
        log.info("Model loaded in %.1fs", time.monotonic() - t0)
        self.embedding_dim: Optional[int] = None
        self._tubelet: Optional[int] = None  # discovered on first forward

    @torch.inference_mode()
    def encode_dense(self, clip: torch.Tensor) -> torch.Tensor:
        """clip: (1, 3, T, 384, 384) -> tokens (T_tokens, H, W, D) on cpu, fp32."""
        clip = clip.to(self.device, non_blocking=True)
        if self.precision == "fp16" and self.device.startswith("cuda"):
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                out = self._model(clip)
        else:
            out = self._model(clip)
        # V-JEPA returns either a tensor (B, N, D) or an object with .last_hidden_state.
        tokens = getattr(out, "last_hidden_state", out)  # (1, N, D)
        N = tokens.shape[1]
        Hg, Wg = _GRID_HW
        # Infer temporal-token count from N = T_tokens * Hg * Wg
        if N % (Hg * Wg) != 0:
            raise RuntimeError(f"Unexpected token count {N}; expected multiple of {Hg*Wg}")
        T_tokens = N // (Hg * Wg)
        if self._tubelet is None:
            self._tubelet = clip.shape[2] // T_tokens
            log.info("Token grid: T_tokens=%d, H=%d, W=%d (tubelet=%d)", T_tokens, Hg, Wg, self._tubelet)
        D = tokens.shape[-1]
        if self.embedding_dim is None:
            self.embedding_dim = D
        return tokens.float().reshape(T_tokens, Hg, Wg, D).cpu()


# --- frame ring buffer (keeps both raw uint8 and normalized) ------------------
class FrameRing:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._raw: Deque[torch.Tensor] = deque(maxlen=capacity)
        self._norm: Deque[torch.Tensor] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def push(self, raw_uint8: torch.Tensor, norm: torch.Tensor) -> None:
        with self._lock:
            self._raw.append(raw_uint8)
            self._norm.append(norm)

    def snapshot(self) -> Optional[tuple[torch.Tensor, torch.Tensor]]:
        """Returns (clip_tensor (1, 3, T, H, W), latest_raw_uint8 (3, H, W)) or None."""
        with self._lock:
            if len(self._norm) < self.capacity:
                return None
            frames_norm = list(self._norm)
            latest_raw = self._raw[-1].clone()
        clip = torch.stack(frames_norm, dim=1).unsqueeze(0)
        return clip, latest_raw


# --- algorithms (zero-training, pure feature-space) ---------------------------
def temporal_variance_map(tokens: torch.Tensor) -> torch.Tensor:
    """tokens (T, H, W, D) -> variance norm per spatial token (H, W)."""
    return tokens.std(dim=0).norm(dim=-1)


def feature_aware_cc(features: torch.Tensor, mask: np.ndarray, sim_threshold: float) -> np.ndarray:
    """
    features : (H, W, D) cpu float32 tensor (e.g., temporal mean of dense tokens)
    mask     : (H, W) bool numpy array — only consider tokens where mask is True
    Returns labels (H, W) int32, 0 = background; 1..K = blob ids.

    Connection rule: 4-neighbors AND cosine similarity > sim_threshold.
    O(H*W) via union-find.
    """
    H, W, D = features.shape
    feats = F.normalize(features, dim=-1).numpy()  # (H, W, D)
    parent = np.arange(H * W, dtype=np.int32)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    moving = mask.astype(bool)
    # E and S neighbors only — unions cover W and N directions transitively.
    for y in range(H):
        for x in range(W):
            if not moving[y, x]:
                continue
            i = y * W + x
            if x + 1 < W and moving[y, x + 1]:
                if float(feats[y, x] @ feats[y, x + 1]) > sim_threshold:
                    union(i, y * W + (x + 1))
            if y + 1 < H and moving[y + 1, x]:
                if float(feats[y, x] @ feats[y + 1, x]) > sim_threshold:
                    union(i, (y + 1) * W + x)

    labels = np.zeros((H, W), dtype=np.int32)
    next_id = 1
    id_map: dict[int, int] = {}
    for y in range(H):
        for x in range(W):
            if not moving[y, x]:
                continue
            root = find(y * W + x)
            if root not in id_map:
                id_map[root] = next_id
                next_id += 1
            labels[y, x] = id_map[root]
    return labels


def token_flow_argmax(tokens_t: torch.Tensor, tokens_tp1: torch.Tensor, window: int = 5) -> torch.Tensor:
    """
    tokens_t, tokens_tp1 : (H, W, D)
    Returns flow (H, W, 2) in token-grid units (dy, dx), float32 per-token argmax-similarity match.
    """
    H, W, D = tokens_t.shape
    a = F.normalize(tokens_t, dim=-1)
    b = F.normalize(tokens_tp1, dim=-1)
    flow = torch.zeros(H, W, 2)
    r = window // 2
    for y in range(H):
        for x in range(W):
            y0, y1 = max(0, y - r), min(H, y + r + 1)
            x0, x1 = max(0, x - r), min(W, x + r + 1)
            patch = b[y0:y1, x0:x1]                       # (h, w, D)
            sims = (patch @ a[y, x]).flatten()            # (h*w,)
            best = int(sims.argmax().item())
            dy = (y0 + best // (x1 - x0)) - y
            dx = (x0 + best % (x1 - x0)) - x
            flow[y, x, 0] = dy
            flow[y, x, 1] = dx
    return flow


def build_blob_descriptors(
    tokens: torch.Tensor,
    labels: np.ndarray,
    flow: Optional[torch.Tensor],
    min_blob_tokens: int,
) -> list[dict]:
    """For each blob id > 0, return {id, mask, signature, area, velocity_2d}."""
    tokens_mean = tokens.mean(dim=0)  # (H, W, D)
    out: list[dict] = []
    for b in range(1, int(labels.max()) + 1):
        mask = labels == b
        area = int(mask.sum())
        if area < min_blob_tokens:
            continue
        sig = tokens_mean[torch.from_numpy(mask)].mean(dim=0)
        sig_n = F.normalize(sig, dim=-1)
        vel = (0.0, 0.0)
        if flow is not None:
            mask_t = torch.from_numpy(mask)
            flow_in_blob = flow[mask_t]
            vel = (float(flow_in_blob[:, 0].mean()), float(flow_in_blob[:, 1].mean()))
        ys, xs = np.where(mask)
        out.append({
            "blob_id_in_clip": b,
            "mask": mask,
            "signature": sig_n,                     # torch (D,)
            "area": area,
            "centroid_yx": (float(ys.mean()), float(xs.mean())),
            "velocity_yx": vel,                     # tokens / clip
            "bbox_yx": (int(ys.min()), int(xs.min()), int(ys.max()), int(xs.max())),
        })
    return out


# --- cross-clip tracker (Hungarian on signature similarity) -------------------
class TrackStore:
    def __init__(self, sig_match_threshold: float, max_age: int):
        self._tracks: dict[int, dict] = {}   # track_id -> latest descriptor + age
        self._next_id = 1
        self._tau = sig_match_threshold
        self._max_age = max_age

    def step(self, blobs: list[dict]) -> list[int]:
        """Return parallel list of track ids assigned to each blob, in input order."""
        if not self._tracks or not blobs:
            ids = [self._spawn(b) for b in blobs]
            self._age_unmatched(set(ids))
            return ids

        track_ids = list(self._tracks.keys())
        prev_sigs = torch.stack([self._tracks[t]["signature"] for t in track_ids])  # (T, D)
        curr_sigs = torch.stack([b["signature"] for b in blobs])                   # (B, D)
        cost = 1.0 - (prev_sigs @ curr_sigs.T).numpy()                              # (T, B)

        row_ind, col_ind = linear_sum_assignment(cost)
        assigned: dict[int, int] = {}     # blob_idx -> track_id
        used: set[int] = set()
        for r, c in zip(row_ind, col_ind):
            if cost[r, c] <= 1.0 - self._tau:
                tid = track_ids[r]
                assigned[c] = tid
                used.add(tid)
                self._tracks[tid].update({"signature": blobs[c]["signature"], "age": 0})

        # Spawn new tracks for unmatched blobs.
        out_ids: list[int] = []
        for i, b in enumerate(blobs):
            if i in assigned:
                out_ids.append(assigned[i])
            else:
                out_ids.append(self._spawn(b))

        self._age_unmatched(set(out_ids))
        return out_ids

    def _spawn(self, blob: dict) -> int:
        tid = self._next_id
        self._next_id += 1
        self._tracks[tid] = {"signature": blob["signature"], "age": 0}
        return tid

    def _age_unmatched(self, fresh_ids: set[int]) -> None:
        for tid in list(self._tracks.keys()):
            if tid not in fresh_ids:
                self._tracks[tid]["age"] += 1
                if self._tracks[tid]["age"] > self._max_age:
                    del self._tracks[tid]


# --- overlay rendering --------------------------------------------------------
def hsv_palette(n: int) -> torch.Tensor:
    """Return (n, 3) uint8 palette via HSV cycling. Index 0 reserved for background (black)."""
    if n <= 0:
        return torch.zeros(1, 3, dtype=torch.uint8)
    hues = torch.linspace(0, 1, max(n, 1) + 1)[:-1]
    sat = torch.full_like(hues, 0.85)
    val = torch.full_like(hues, 0.95)
    # HSV->RGB (manual to keep deps minimal)
    h60 = hues * 6
    c = val * sat
    x = c * (1 - (h60 % 2 - 1).abs())
    m = val - c
    r = torch.zeros_like(hues); g = torch.zeros_like(hues); b = torch.zeros_like(hues)
    for k in range(6):
        sel = (h60.floor().long() == k)
        if k == 0: r[sel], g[sel], b[sel] = c[sel], x[sel], 0
        if k == 1: r[sel], g[sel], b[sel] = x[sel], c[sel], 0
        if k == 2: r[sel], g[sel], b[sel] = 0, c[sel], x[sel]
        if k == 3: r[sel], g[sel], b[sel] = 0, x[sel], c[sel]
        if k == 4: r[sel], g[sel], b[sel] = x[sel], 0, c[sel]
        if k == 5: r[sel], g[sel], b[sel] = c[sel], 0, x[sel]
    rgb = torch.stack([(r + m), (g + m), (b + m)], dim=-1)
    return (rgb * 255).clamp(0, 255).byte()


def render_overlay(
    raw_chw_uint8: torch.Tensor,
    labels: np.ndarray,
    track_ids: list[int],
    blob_id_per_label: dict[int, int],
    alpha: float,
) -> torch.Tensor:
    """
    raw_chw_uint8 : (3, H, W) uint8 RGB
    labels        : (Ht, Wt) int — output of feature_aware_cc
    track_ids     : list of stable IDs assigned to blobs (in blob order)
    blob_id_per_label: maps clip-local blob id -> index into track_ids (or -1 if filtered out)
    Returns chw uint8 RGB blended overlay.
    """
    C, H, W = raw_chw_uint8.shape
    # Recolor the label map by track_id so that consistent IDs get consistent colors.
    recolored = np.zeros_like(labels)
    for clip_local, idx in blob_id_per_label.items():
        if idx >= 0:
            recolored[labels == clip_local] = track_ids[idx]
    n_unique = int(recolored.max()) + 1
    palette = hsv_palette(max(n_unique, 1))
    # Index 0 -> black (already in zeros)
    palette = torch.cat([torch.zeros(1, 3, dtype=torch.uint8), palette[:n_unique]], dim=0)

    # Upsample label grid HW (24x24) -> (H, W) NEAREST (preserves discrete labels)
    lbl_t = torch.from_numpy(recolored).long().unsqueeze(0).unsqueeze(0).float()
    lbl_full = F.interpolate(lbl_t, size=(H, W), mode="nearest").long().squeeze(0).squeeze(0)
    color_full = palette[lbl_full]                # (H, W, 3) uint8
    mask_full = (lbl_full > 0).unsqueeze(-1)       # (H, W, 1)

    base = raw_chw_uint8.permute(1, 2, 0).float() / 255.0    # (H, W, 3) [0,1]
    over = color_full.float() / 255.0
    blended = torch.where(mask_full, alpha * over + (1 - alpha) * base, base)
    out = (blended * 255).clamp(0, 255).byte().permute(2, 0, 1).contiguous()
    return out  # (3, H, W) uint8


def encode_jpeg_bytes(rgb_chw_uint8: torch.Tensor, quality: int) -> bytes:
    """torchvision.io.encode_jpeg: returns a (N,) uint8 tensor; convert to bytes."""
    enc = encode_jpeg(rgb_chw_uint8, quality=int(quality))
    return enc.numpy().tobytes()


# --- config + node class ------------------------------------------------------
def _validate(cfg: dict) -> dict:
    name = cfg.get("name")
    if not name or not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"config.name required and matching ^[a-zA-Z0-9/_\\-\\.]+$: {name!r}")
    if "input_topic" not in cfg:
        raise ValueError("config.input_topic required")
    clip_frames = int(cfg.get("clip_frames", 16))
    if not 2 <= clip_frames <= 64:
        raise ValueError("clip_frames in [2, 64]")
    target_hz = float(cfg.get("target_hz", 0.5))
    if not 0.01 <= target_hz <= 30.0:
        raise ValueError("target_hz in [0.01, 30]")
    precision = cfg.get("precision", "fp16")
    if precision not in ("fp16", "fp32"):
        raise ValueError("precision in {fp16, fp32}")
    sim_threshold = float(cfg.get("sim_threshold", 0.6))
    if not 0.0 <= sim_threshold <= 1.0:
        raise ValueError("sim_threshold in [0,1]")
    variance_k = float(cfg.get("variance_k", 1.5))
    if variance_k <= 0:
        raise ValueError("variance_k > 0")
    return {
        "name": name,
        "input_topic": cfg["input_topic"],
        "model": cfg.get("model", "vjepa2_1_vit_base_384"),
        "device": cfg.get("device", "cuda"),
        "precision": precision,
        "clip_frames": clip_frames,
        "target_hz": target_hz,
        "sim_threshold": sim_threshold,
        "variance_k": variance_k,
        "min_blob_tokens": int(cfg.get("min_blob_tokens", 8)),
        "sig_match_threshold": float(cfg.get("sig_match_threshold", 0.65)),
        "max_track_age_clips": int(cfg.get("max_track_age_clips", 3)),
        "publish_overlay": bool(cfg.get("publish_overlay", True)),
        "overlay_alpha": float(cfg.get("overlay_alpha", 0.45)),
        "overlay_jpeg_quality": int(cfg.get("overlay_jpeg_quality", 80)),
    }


class JepaTrackerNode:
    name = "jepa-tracker"

    def __init__(self, ctx: NodeContext, config: dict) -> None:
        self._ctx = ctx
        self._cfg = _validate(config)
        self._ring = FrameRing(self._cfg["clip_frames"])
        self._model = VJepa21Dense(self._cfg["model"], self._cfg["device"], self._cfg["precision"])
        self._tracks = TrackStore(self._cfg["sig_match_threshold"], self._cfg["max_track_age_clips"])
        self._interval = 1.0 / self._cfg["target_hz"]
        self._seq = 0

        self._sub = ctx.subscribe(self._cfg["input_topic"], local=True)
        self._tracks_pub = ctx.publisher_json("tracks", schema_uri="bubbaloop://jepa-tracks/v1")
        self._overlay_pub = (
            ctx.publisher_cbor("blobs_overlay", schema_uri="bubbaloop://blobs-overlay/v1")
            if self._cfg["publish_overlay"] else None
        )

        log.info(
            "Ready: input=%s model=%s precision=%s clip_frames=%d target_hz=%.2f overlay=%s",
            self._cfg["input_topic"], self._cfg["model"], self._cfg["precision"],
            self._cfg["clip_frames"], self._cfg["target_hz"], self._cfg["publish_overlay"],
        )

    def run(self) -> None:
        receive = threading.Thread(target=self._receive_loop, daemon=True, name="receive")
        receive.start()
        try:
            self._inference_loop()
        finally:
            self._sub.undeclare()
            self._tracks_pub.undeclare()
            if self._overlay_pub is not None:
                self._overlay_pub.undeclare()
            receive.join(timeout=2.0)
            log.info("Shutdown complete (clips=%d)", self._seq)

    def _receive_loop(self) -> None:
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
                raw, norm = preprocess_frame(data, w, h)
            except Exception as exc:
                log.warning("preprocess failed: %s", exc)
                continue
            self._ring.push(raw, norm)

    def _inference_loop(self) -> None:
        next_run = time.monotonic()
        while not self._ctx.is_shutdown():
            now = time.monotonic()
            if now < next_run:
                self._ctx._shutdown.wait(timeout=next_run - now)
                continue
            snap = self._ring.snapshot()
            if snap is None:
                self._ctx._shutdown.wait(timeout=0.1)
                continue
            clip, latest_raw = snap
            next_run = time.monotonic() + self._interval

            t0 = time.monotonic()
            tokens = self._model.encode_dense(clip)             # (T_tokens, H, W, D)
            t_forward = (time.monotonic() - t0) * 1000

            t1 = time.monotonic()
            var_map = temporal_variance_map(tokens).numpy()      # (H, W)
            mov_thresh = self._cfg["variance_k"] * float(np.median(var_map))
            moving_mask = var_map > mov_thresh
            features_mean = tokens.mean(dim=0)                   # (H, W, D)
            labels = feature_aware_cc(features_mean, moving_mask, self._cfg["sim_threshold"])

            # Optional: token-flow between first and last temporal token for velocity
            if tokens.shape[0] >= 2:
                flow = token_flow_argmax(tokens[0], tokens[-1])
            else:
                flow = None

            blobs = build_blob_descriptors(tokens, labels, flow, self._cfg["min_blob_tokens"])
            track_ids = self._tracks.step(blobs)
            t_post = (time.monotonic() - t1) * 1000

            # Publish JSON tracks
            self._tracks_pub.put({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "clip_seq": self._seq,
                "model": self._cfg["model"],
                "grid_hw": list(_GRID_HW),
                "blobs": [
                    {
                        "track_id": tid,
                        "area_tokens": b["area"],
                        "centroid_yx": list(b["centroid_yx"]),
                        "bbox_yx": list(b["bbox_yx"]),
                        "velocity_yx_tokens_per_clip": list(b["velocity_yx"]),
                    }
                    for tid, b in zip(track_ids, blobs)
                ],
            })

            # Publish overlay JPEG
            if self._overlay_pub is not None:
                blob_id_to_idx = {b["blob_id_in_clip"]: i for i, b in enumerate(blobs)}
                overlay = render_overlay(
                    latest_raw, labels, track_ids, blob_id_to_idx, self._cfg["overlay_alpha"],
                )
                jpeg_bytes = encode_jpeg_bytes(overlay, self._cfg["overlay_jpeg_quality"])
                self._overlay_pub.put({
                    "width": _RESIZE[1],
                    "height": _RESIZE[0],
                    "encoding": "jpeg",
                    "data": jpeg_bytes,
                })

            self._seq += 1
            log.info(
                "seq=%d blobs=%d tracks=%d forward=%.1fms post=%.1fms",
                self._seq, len(blobs), len({i for i in track_ids}), t_forward, t_post,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.parse_known_args()
    run_node(JepaTrackerNode)
