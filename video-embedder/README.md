# video-embedder

V-JEPA 2.1 video-clip embeddings for Bubbaloop. Subscribes to a local-SHM
camera frame stream, buffers the last N frames, and publishes a pooled
embedding vector as JSON once per configurable interval.

Weights are not on HuggingFace yet (Meta has only published V-JEPA 2.1 to the
[facebookresearch/vjepa2](https://github.com/facebookresearch/vjepa2) repo);
the node loads via `torch.hub.load` on first run.

## Topics

| Topic | Scope | Encoding | Body |
|---|---|---|---|
| `{name}/embeddings` | global | `application/json` | pooled clip embedding (see below) |

Input is subscribed from the absolute topic suffix in `config.yaml` →
`bubbaloop/local/{machine}/{input_topic}`. Both legacy RGBA (body root) and
oak-camera rgbd (`body.rgb.data`) wire formats are accepted.

## Output body

```json
{
  "timestamp": "2026-04-24T06:48:42Z",
  "embedding": [0.0123, -0.0456, ...],
  "dim": 768,
  "model": "vjepa2_1_vit_base_384",
  "clip_frames": 16,
  "resolution": 384,
  "inference_ms": 182.4
}
```

The SDK wraps this dict in its standard `{header, body}` provenance envelope
(see `python-sdk/bubbaloop_sdk/publisher.py::JsonPublisher`).

## Configuration

```yaml
# config.yaml
name: video_embedder
role: processor

input_topic: tapo_terrace_camera/raw   # or oak_primary/rgbd

model: vjepa2_1_vit_base_384           # 80M params, 768-dim
device: cuda
clip_frames: 16                         # ring buffer size → (1,3,16,384,384)
target_hz: 0.5                          # one clip embedding every 2 s
```

Available model entrypoints (torch.hub names):

| Entrypoint | Params | Embed dim | Jetson Orin notes |
|---|---|---|---|
| `vjepa2_1_vit_base_384` | 80M | 768 | **default**, safe |
| `vjepa2_1_vit_large_384` | 300M | 1024 | realistic @ ≤ 0.5 Hz |
| `vjepa2_1_vit_giant_384` | 1B | — | heavy, try cautiously |
| `vjepa2_1_vit_gigantic_384` | 2B | — | probably too heavy |

## Install & run

```bash
cd video-embedder
pixi install
pixi run main
```

First `pixi run main` downloads the V-JEPA 2.1 checkpoint into
`~/.cache/torch/hub/`. No `HF_TOKEN` or license required (torch.hub fetches
from Meta's GitHub release artifacts).

## Tests

```bash
pixi run test
```

The tests exercise config validation, frame preprocessing, and the ring
buffer — they do **not** load the model (which requires internet + CUDA).
