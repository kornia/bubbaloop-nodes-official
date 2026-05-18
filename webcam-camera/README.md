# webcam-camera

Bubbaloop node that captures frames from a USB/V4L2 webcam using OpenCV and publishes compressed JPEG over Zenoh.

**Published topic:** `bubbaloop/global/{machine}/{name}/compressed`
Body: `{width, height, encoding: "jpeg", data: <bytes>}` (CBOR-wrapped by the SDK)

## Quick start

```bash
# 1. Install environment
pixi install

# 2. Edit config (at minimum, set your device index)
$EDITOR config.yaml

# 3. Run directly for iteration
pixi run main -c config.yaml

# 4. Register with the daemon (three steps — all required)
bubbaloop node add $(pwd) -n webcam_primary -c $(pwd)/config.yaml
bubbaloop node install webcam_primary
bubbaloop node start webcam_primary

# 5. Verify
bubbaloop node list
bubbaloop node logs webcam_primary -f
```

## Config reference

| Field | Default | Description |
|-------|---------|-------------|
| `name` | required | Per-instance topic namespace. Must match `^[a-zA-Z0-9/_\-\.]+$`. |
| `device` | `0` | Camera index (int) or device path (`"/dev/video0"`). |
| `width` | `1280` | Capture width. Must be a multiple of 16, in \[16, 4096\]. |
| `height` | `720` | Capture height. Must be a multiple of 16, in \[16, 4096\]. |
| `fps` | `30` | Target frame rate in \[1, 120\]. Actual rate is driver-determined. |
| `jpeg_quality` | `80` | JPEG encoding quality in \[1, 100\]. |
| `warn_on_param_mismatch` | `true` | Log a warning if the driver silently ignores the requested width/height/fps. |

## Multi-instance (multiple webcams)

Each camera needs its own config and daemon registration:

```bash
# Create per-camera configs
cp config.yaml configs/left.yaml && sed -i 's/device: 0/device: 0/' configs/left.yaml
cp config.yaml configs/right.yaml && sed -i 's/device: 0/device: 1/' configs/right.yaml
# Edit name field in each config too

bubbaloop node add $(pwd) -n webcam_left  -c $(pwd)/configs/left.yaml
bubbaloop node install webcam_left && bubbaloop node start webcam_left

bubbaloop node add $(pwd) -n webcam_right -c $(pwd)/configs/right.yaml
bubbaloop node install webcam_right && bubbaloop node start webcam_right
```

## Tests

Unit tests cover `_validate()` only — no camera required.

```bash
pixi run -- python -m pytest tests/ -v
```

## Compatible consumers

Any node that subscribes to `bubbaloop://compressed/v1` works:
- `mcap-recorder` — records JPEG frames to `.mcap`
- `frame-embedder` — DINOv3 embeddings from camera frames
- `camera-object-detector` — YOLO11 detection on frames
- `camera-vlm` — vision-language model inference
