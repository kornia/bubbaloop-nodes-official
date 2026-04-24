"""frame-embedder -- subscribes to camera frames, publishes embeddings as JSON."""

import logging
import threading
import time
from datetime import datetime, timezone

import numpy as np
import torch
from bubbaloop_sdk import run_node, NodeContext

from preprocessing import preprocess_frame
from model import DinoModel

log = logging.getLogger("frame-embedder")


class FrameEmbedderNode:
    name = "frame-embedder"

    def __init__(self, ctx: NodeContext, config: dict):
        self.ctx = ctx
        self.config = config

        device = config.get("device", "cuda")
        model_name = config.get("model", "facebook/dinov3-vitb16-pretrain-lvd1689m")
        self.model = DinoModel(model_name=model_name, device=device)

        input_topic = config["input_topic"]
        # input_topic is an absolute key (relative to bubbaloop/local/{machine}/)
        # pointing at the upstream camera node's raw frame topic.
        self.sub = ctx.subscribe(input_topic, local=True)
        # Auto-scoped under our instance_name → tapo_terrace_embedder/embeddings
        self.pub = ctx.publisher_json("embeddings")

        target_fps = config.get("target_hz", 2.0)
        self._interval = 1.0 / target_fps

        self._latest_frame = None
        self._frame_lock = threading.Lock()
        self._seq = 0

        log.info(
            "Ready: input=%s, model=%s, device=%s, dim=%d, fps=%.1f",
            input_topic, model_name, device, self.model.embedding_dim, target_fps,
        )

    def run(self):
        receive_thread = threading.Thread(target=self._receive_loop, daemon=True, name="receive")
        receive_thread.start()

        self._inference_loop()

        self.sub.undeclare()
        receive_thread.join(timeout=2.0)
        log.info("Shutdown complete (processed %d frames)", self._seq)

    def _receive_loop(self):
        """Buffer the latest decoded frame. Runs in a daemon thread."""
        for env in self.sub:
            # SDK >=Apr2026 wraps CBOR payloads in a {header, body} Envelope.
            # `getattr(env, 'body', env)` keeps us compatible with non-enveloped upstreams.
            msg = getattr(env, "body", env)
            if isinstance(msg, (bytes, bytearray)):
                continue
            with self._frame_lock:
                self._latest_frame = msg

    def _inference_loop(self):
        """Pick up latest frame, run inference, publish. Runs on main thread."""
        next_run = time.monotonic()
        while not self.ctx.is_shutdown():
            now = time.monotonic()
            if now < next_run:
                self.ctx._shutdown.wait(timeout=next_run - now)
                continue

            with self._frame_lock:
                msg = self._latest_frame
                self._latest_frame = None

            if msg is None:
                self.ctx._shutdown.wait(timeout=0.01)
                continue

            next_run = time.monotonic() + self._interval
            t0 = time.monotonic()

            tensor = preprocess_frame(
                rgba_bytes=bytes(msg.data),
                width=msg.width,
                height=msg.height,
            )
            embedding = self.model.encode(tensor)
            inference_ms = (time.monotonic() - t0) * 1000

            self.pub.put({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "embedding": embedding.tolist(),
                "dim": self.model.embedding_dim,
                "model": self.model.model_name,
                "width": 224,
                "height": 224,
                "inference_ms": round(inference_ms, 1),
            })

            self._seq += 1
            log.info("seq=%d dim=%d infer=%.1fms", self._seq, self.model.embedding_dim, inference_ms)


if __name__ == "__main__":
    run_node(FrameEmbedderNode)
