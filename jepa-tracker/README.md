# jepa-tracker

Zero-training object detection + tracking from V-JEPA 2.1 dense embeddings.
Subscribes to a camera frame stream, segments moving things by feature-aware
connected components on the temporal-variance map, and publishes:

- `{instance}/tracks` — JSON, structured per-blob descriptors (mask bbox,
  centroid, velocity, stable track id) at the configured `target_hz`.
- `{instance}/blobs_overlay` — CBOR `{width, height, encoding:"jpeg", data}`,
  the clip-center frame with each blob alpha-tinted in a stable color per
  track id. Renders directly in the dashboard's encoding-first JPEG path —
  no dashboard changes required.

No supervised labels, no calibration, no monocular-depth model. The whole
pipeline is one V-JEPA 2.1 forward pass plus matrix ops + a tiny union-find.

## Pipeline

```
clip (1, 3, T, 384, 384) ──► V-JEPA 2.1 ──► dense tokens X ∈ ℝ^(T_tokens, H, W, D)
                                                  │
                              ┌───────────────────┼───────────────────┐
                              ▼                   ▼                   ▼
                    temporal variance       feature-aware CC     argmax token-flow
                       (H, W) saliency       on adjacency         (T_tokens-1, H, W, 2)
                                             graph thresholded
                                             by cos-sim
                                                  │                   │
                                                  └───── descriptors ─┘
                                                  │ (mask, signature, area,
                                                  │  centroid, velocity)
                                                  ▼
                                       Hungarian re-ID against last clip
                                                  │
                                                  ▼
                                       JSON tracks + JPEG overlay
```

## Image ops: kornia / torch ecosystem only

This node deliberately avoids OpenCV. Image preprocessing uses
`torch.nn.functional.interpolate`, label-grid upsampling uses the same
(with `mode="nearest"` to preserve discrete blob IDs), JPEG encoding uses
`torchvision.io.encode_jpeg` (libjpeg-turbo). `kornia` is in the env for
future filter / geometric ops.

## Config

```yaml
name: tapo_terrace_tracker
input_topic: tapo_terrace_camera/raw

model: vjepa2_1_vit_base_384
device: cuda
precision: fp16          # autocast on CUDA, weights stay fp32
clip_frames: 16
target_hz: 0.5

sim_threshold: 0.6        # cosine for connecting two adjacent tokens
variance_k: 1.5           # moving threshold = k * median(variance)
min_blob_tokens: 8        # drop tiny blobs

sig_match_threshold: 0.65 # cosine to associate with prior track
max_track_age_clips: 3    # forget tracks not seen for this many clips

publish_overlay: true
overlay_alpha: 0.45
overlay_jpeg_quality: 80
```

## Outputs

### `{instance}/tracks` (JSON)

```json
{
  "timestamp": "2026-04-25T07:00:00Z",
  "clip_seq": 42,
  "model": "vjepa2_1_vit_base_384",
  "grid_hw": [24, 24],
  "blobs": [
    {
      "track_id": 7,
      "area_tokens": 41,
      "centroid_yx": [12.4, 9.1],
      "bbox_yx": [10, 6, 14, 12],
      "velocity_yx_tokens_per_clip": [0.1, -0.3]
    }
  ]
}
```

`track_id` persists across clips when the blob signature matches above
`sig_match_threshold`. Coordinates are in the 24×24 token grid (multiply
by 16 for the 384² image-space).

### `{instance}/blobs_overlay` (CBOR-wrapped JPEG)

The same body shape as `oak-camera/compressed`. The dashboard's existing
JPEG decoder picks it up natively.

## Run

```bash
cd jepa-tracker
pixi install
pixi run main           # standalone
# or via the daemon:
bubbaloop node add /path/to/jepa-tracker -n tapo-terrace-jepa-tracker -c /path/to/config.yaml
bubbaloop node install tapo-terrace-jepa-tracker
bubbaloop node start tapo-terrace-jepa-tracker
```

First run downloads V-JEPA 2.1 weights (~1.5 GB) into `~/.cache/torch/hub/`.

## Tests

```bash
pixi run test
```

Tests cover config validation, the feature-aware connected-components
algorithm on synthetic data, blob descriptor extraction, palette
construction, and the cross-clip Hungarian tracker. Tests do **not**
load V-JEPA (which needs internet + CUDA).
