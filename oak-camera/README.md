# oak-camera

OAK (Luxonis / Movidius MyriadX) camera node for Bubbaloop. Publishes RGB and
aligned stereo depth as a single RGBD message so downstream processors can run
detection on the image and sample depth at the detected bboxes without a second
round-trip.

## Topics

Scoped under the `name` from `config.yaml` (default `oak_primary`):

| Topic | Scope | Encoding | Body |
|---|---|---|---|
| `{name}/compressed` | global | CBOR envelope | `{width, height, encoding:"jpeg", data}` |
| `{name}/rgbd` | local (SHM) | CBOR envelope | RGBA + aligned depth16 (see below) |

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
