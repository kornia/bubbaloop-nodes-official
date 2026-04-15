#!/usr/bin/env python3
"""camera-vlm — Scene description on camera raw frames via Zenoh SHM.

Subscribes to `{key}/raw` (CBOR RawImage, encoding="rgba8", over Zenoh SHM
published by the rtsp-camera node) and publishes JSON scene descriptions to
`{key}/description`.

Topic key is derived from the instance name: `tapo_terrace_vlm` -> `tapo_terrace`.
"""

import logging
import threading
import time
from datetime import datetime, timezone

import numpy as np
import torch
import yaml
from PIL import Image

log = logging.getLogger("camera-vlm")


def load_config(path: str) -> dict:
    """Load and validate config YAML."""
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("name"):
        raise ValueError("Missing required config field: name")

    target_fps = float(cfg.get("target_fps", 0.1))
    if not (0.01 <= target_fps <= 1.0):
        raise ValueError(f"target_fps {target_fps} out of range (0.01-1.0)")
    cfg["target_fps"] = target_fps

    device = cfg.get("device", "cuda")
    if device not in ("cuda", "cpu"):
        raise ValueError(f"device {device!r} must be 'cuda' or 'cpu'")
    cfg["device"] = device

    model = cfg.get("model", "Qwen/Qwen2.5-VL-3B-Instruct")
    cfg["model"] = model

    prompt = cfg.get("prompt", "Describe this scene in one or two sentences.")
    cfg["prompt"] = prompt

    max_tokens = int(cfg.get("max_tokens", 128))
    if not (16 <= max_tokens <= 512):
        raise ValueError(f"max_tokens {max_tokens} out of range (16-512)")
    cfg["max_tokens"] = max_tokens

    return cfg


def build_payload(
    frame_id: str,
    machine_id: str,
    sequence: int,
    description: str,
    inference_ms: float,
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "frame_id": frame_id,
        "machine_id": machine_id,
        "sequence": sequence,
        "description": description,
        "inference_ms": round(inference_ms, 1),
    }


class Describer:
    """Vision-language model inference wrapper. Accepts a PIL Image, returns text.

    Supports Qwen2.5-VL and other HuggingFace VLMs that use the chat template API.
    """

    def __init__(
        self,
        model_id: str = "Qwen/Qwen2.5-VL-3B-Instruct",
        device: str = "cuda",
        max_tokens: int = 128,
    ) -> None:
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self._device = device
        self._max_tokens = max_tokens
        self._is_qwen = "qwen" in model_id.lower()
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16

        log.info("Loading model %s (device=%s, dtype=%s)...", model_id, device, dtype)
        self._processor = AutoProcessor.from_pretrained(model_id)
        self._model = AutoModelForImageTextToText.from_pretrained(
            model_id,
            device_map=device,
            torch_dtype=dtype,
        )
        self._model.eval()
        if device == "cpu":
            self._model = self._model.to("cpu").float()
        log.info("Model loaded: %s", model_id)

    def describe(self, image: Image.Image, prompt: str) -> str:
        """Run vision inference on a PIL Image. Returns description text."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": prompt},
                ],
            }
        ]

        if self._is_qwen:
            from qwen_vl_utils import process_vision_info
            text = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = self._processor(
                text=[text], images=image_inputs, videos=video_inputs,
                return_tensors="pt",
            ).to(self._model.device)
        else:
            inputs = self._processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            ).to(self._model.device)

        input_len = inputs["input_ids"].shape[-1]

        with torch.inference_mode():
            output_ids = self._model.generate(
                **inputs,
                max_new_tokens=self._max_tokens,
                do_sample=False,
            )
            output_ids = output_ids[0][input_len:]

        return self._processor.decode(output_ids, skip_special_tokens=True)


class CameraVlm:
    """Bubbaloop node: subscribe to raw RGBA camera frames, describe scene, publish JSON.

    Topic derivation from instance name:
      tapo_terrace_vlm -> topic key: tapo_terrace
        subscribe:  tapo_terrace/raw            (CBOR, RGBA, SHM)
        publish:    tapo_terrace/description    (JSON)
    """

    name = "camera-vlm"

    def __init__(self, ctx, config: dict) -> None:
        self._ctx = ctx
        self._target_fps = config["target_fps"]
        self._prompt = config["prompt"]

        instance_name = config["name"]
        topic_key = instance_name.removesuffix("_vlm")

        self._topic_key = topic_key
        self._pub = ctx.publisher_json(f"{topic_key}/description")
        self._describer = Describer(
            model_id=config["model"],
            device=config["device"],
            max_tokens=config["max_tokens"],
        )

        self._latest_frame: "Image.Image | None" = None
        self._frame_lock = threading.Lock()
        self._seq = 0

        log.info(
            "Subscribing to %s/raw, publishing to %s at %.2f fps",
            topic_key,
            ctx.topic(f"{topic_key}/description"),
            self._target_fps,
        )

    def run(self) -> None:
        ctx = self._ctx
        sub = ctx.subscribe(f"{self._topic_key}/raw", local=True)

        def _receive_loop() -> None:
            for env in sub:
                # CBOR payloads arrive wrapped in a {header, body} Envelope.
                msg = getattr(env, "body", env)
                w, h = msg.width, msg.height
                rgba = np.frombuffer(msg.data, dtype=np.uint8).reshape(h, w, 4)
                pil_image = Image.fromarray(rgba[:, :, :3], mode="RGB")
                del rgba, msg, env

                with self._frame_lock:
                    self._latest_frame = pil_image

        def _inference_loop() -> None:
            interval = 1.0 / self._target_fps
            next_run = time.monotonic()
            while not ctx._shutdown.is_set():
                now = time.monotonic()
                if now < next_run:
                    time.sleep(min(0.5, next_run - now))
                    continue

                with self._frame_lock:
                    frame = self._latest_frame
                    self._latest_frame = None

                if frame is None:
                    time.sleep(0.5)
                    continue

                next_run = time.monotonic() + interval
                t0 = time.monotonic()
                description = self._describer.describe(frame, self._prompt)
                del frame
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                t1 = time.monotonic()
                inference_ms = (t1 - t0) * 1000

                payload = build_payload(
                    frame_id=ctx.instance_name,
                    machine_id=ctx.machine_id,
                    sequence=self._seq,
                    description=description,
                    inference_ms=inference_ms,
                )
                self._pub.put(payload)
                self._seq += 1

                log.info(
                    "seq=%d infer=%.0fms desc=%s",
                    self._seq,
                    inference_ms,
                    description[:80] + ("..." if len(description) > 80 else ""),
                )

        receive_thread = threading.Thread(target=_receive_loop, daemon=True, name="receive")
        inference_thread = threading.Thread(target=_inference_loop, daemon=True, name="inference")
        receive_thread.start()
        inference_thread.start()

        ctx._shutdown.wait()
        sub.undeclare()
        inference_thread.join(timeout=5.0)
        log.info("camera-vlm shutdown complete (processed %d frames)", self._seq)


if __name__ == "__main__":
    from bubbaloop_sdk import run_node

    run_node(CameraVlm)
