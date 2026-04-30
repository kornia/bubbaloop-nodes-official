# oak-camera

OAK (Luxonis / Movidius MyriadX) camera node for Bubbaloop. Publishes RGB and
aligned stereo depth as a single RGBD message so downstream processors can run
detection on the image and sample depth at the detected bboxes without a second
round-trip.

## Topics

Scoped under the `name` from `config.yaml` (default `oak_primary`):

| Topic | Scope | Encoding | Body |
|---|---|---|---|
| `{name}/compressed` | global | CBOR envelope | JPEG RGB + PNG-16 depth (recordable RGBD) |
| `{name}/rgbd` | local (SHM) | CBOR envelope | raw RGBA + aligned depth16 |
| `{name}/calibration` | global | CBOR envelope | Camera intrinsics (re-published periodically — see "Calibration block") |

**Same body schema for the two image topics** — only `rgb["encoding"]` (`"jpeg"` vs `"rgba8"`) and `depth["encoding"]` (`"png16_mm"` vs `"depth16_mm"`) differ. One decoder works for both.

## RGB ↔ depth synchronization

`dai.node.Sync` (running on the OAK's Leon coprocessor — zero host CPU) pairs RGB and depth frames whose mid-shutter timestamps fall within `sync_threshold_ms` (default ≈ half a frame interval). Only matched pairs are emitted; unsynced frames are dropped silently when `sync_attempts: -1`.

The body's `header.acq_time` is the **mid-shutter device clock** of the RGB frame — the depth shares this time within the sync threshold. `header.sync_interval_ns` carries the actual RGB↔depth gap as measured by Sync, so downstream consumers can filter on it:

```python
if body["header"].get("sync_interval_ns", 0) > 5_000_000:  # >5ms drift
    skip_frame()
```

When stereo is disabled (`enable_depth: false`), `sync_interval_ns` is omitted and `acq_time` falls back to the host wall-clock at body-build time.

## Wire envelope

Every payload is wrapped by the SDK in:

```
{
  "header": { schema_uri, source_instance, monotonic_seq, ts_ns },
  "body":   <the dict described below>
}
```

with wire encoding `application/cbor`.

## RGBD body

```
{
  "header": { acq_time, pub_time, sequence, frame_id, machine_id },
  "rgb":   { "width": W, "height": H, "encoding": "rgba8",      "step": W*4, "data": <RGBA bytes> },
  "depth": { "width": W, "height": H, "encoding": "depth16_mm", "step": W*2, "data": <uint16 LE mm> }   // optional
}
```

RGB and depth planes share the shape `{width, height, encoding, step, data}` — generic code can iterate `for kind in ('rgb', 'depth'): plane = body.get(kind)`.

Depth is aligned to `CAM_A` (RGB) by the OAK's `StereoDepth` node, so pixel
`(x, y)` in the RGBA buffer maps to the uint16 at offset
`y * depth["step"] + x * 2` in `depth["data"]`. A value of `0` means "no valid depth
at this pixel". To sample depth for a bbox `(x1, y1, x2, y2)`, slice the depth
plane and pick a reducer (median is robust to holes):

```python
import numpy as np
depth_plane = body["depth"]
depth = np.frombuffer(depth_plane["data"], dtype=np.uint16).reshape(
    depth_plane["height"], depth_plane["width"]
)
roi = depth[y1:y2, x1:x2]
valid = roi[roi > 0]
depth_mm = int(np.median(valid)) if valid.size else None
```

If the device has no stereo cameras, the `depth` key is omitted entirely — check `"depth" in body` to branch.

## Calibration block

Camera intrinsics ride on a **separate topic** `{name}/calibration` (global, CBOR envelope):

```
"body": {
  "model": "perspective",   // or "fisheye", "equirectangular", "radial_division"
  "width": W, "height": H,  // resolution these intrinsics apply to
  "fx": ..., "fy": ..., "cx": ..., "cy": ...,
  "distortion": [k1, k2, p1, p2, k3, ...]
}
```

The node re-publishes this static value at `calibration_publish_interval_secs`
(default 1 Hz, ~200 B/s overhead). Static-data semantics: any subscriber that
joins within one interval — including the recorder — sees the current
calibration without an explicit query. Approximates an MQTT retained message.

If the device's EEPROM has no calibration (rare on factory OAKs, possible on
dev boards), no `calibration` topic message is published.

Depth is hardware-aligned to RGB at the same resolution (`stereo.setDepthAlign(CAM_A)`),
so the single calibration block applies to BOTH planes. Project depth to a pointcloud:

```python
import numpy as np
# `calib` here is the body of the latest `{name}/calibration` message.
depth_plane = image_body["depth"]
depth = np.frombuffer(depth_plane["data"], dtype=np.uint16).reshape(
    depth_plane["height"], depth_plane["width"]
)
v, u = np.indices(depth.shape, dtype=np.float32)
Z = depth.astype(np.float32) / 1000.0  # mm → m
X = (u - calib["cx"]) * Z / calib["fx"]
Y = (v - calib["cy"]) * Z / calib["fy"]
points = np.stack([X, Y, Z], axis=-1)[depth > 0]
```

For PNG-16 depth from the `compressed` topic, decode first:
```python
import io
from PIL import Image
depth = np.array(Image.open(io.BytesIO(depth_plane["data"])))   # uint16 HxW
```

For the JPEG RGB plane:
```python
import kornia_rs as kr
rgb = kr.decode_image_jpeg(depth_plane["data"])  # uint8 HxWx3
```

For non-perspective models (fisheye), un-distort first via
`cv2.fisheye.undistortPoints` or `kornia.geometry.calibration` using
`calib["distortion"]`.

## compressed body (recordable RGBD)

```
{
  "header": {
    acq_time,                  // RGB mid-shutter device clock (ns)
    pub_time,                  // host wall-clock at publish
    sequence, frame_id, machine_id,
    sync_interval_ns?          // RGB↔depth gap measured by Sync (omitted RGB-only)
  },
  "rgb":   { "width": W, "height": H, "encoding": "jpeg",     "data": <JPEG bytes> },
  "depth": { "width": W, "height": H, "encoding": "png16_mm", "data": <PNG-16 bytes> }   // optional
}
```

- RGB: JPEG via [`kornia_rs.ImageEncoder`](https://github.com/kornia/kornia-rs)
  (libjpeg-turbo backend), quality from `jpeg_quality`.
- Depth: PNG-16 lossless on uint16 mm via Pillow — never JPEG depth (DCT
  smears across the hard discontinuities at object boundaries → meters-deep
  holes). Decode via `Image.open(io.BytesIO(data))`.
- Cadence: every `jpeg_every_n` source frames.
- Sized for ~3–5 MB/s = 12–18 GB/hr at 1280×720@10 fps.

Calibration is **not** in the body — see the `{name}/calibration` topic.

## Prerequisites

Luxonis udev rule (one-time, as root):

```
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0666"' > /etc/udev/rules.d/80-movidius.rules
udevadm control --reload-rules && udevadm trigger
```

## Run

```
pixi run main -c config.yaml
```
