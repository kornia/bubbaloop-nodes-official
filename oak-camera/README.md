# oak-camera

OAK (Luxonis / Movidius MyriadX) camera node for Bubbaloop.

## Topics

Scoped under the `name` from `config.yaml` (default `oak_primary`):

| Topic | Scope | Encoding | Body |
|---|---|---|---|
| `{name}/compressed` | global | CBOR envelope | `{width, height, encoding:"jpeg", data}` |
| `{name}/raw` | local (SHM) | CBOR envelope | `{width, height, encoding:"rgba8", data}` |
| `{name}/depth_at_bbox` | global queryable | JSON | request/reply below |

The `raw` wire shape matches `rtsp-camera`, so `camera-object-detector` can consume it unchanged by pointing `input_topic` at `oak_primary/raw`.

## Depth query

Send JSON over `session.get()`:

```json
{"x1": 400, "y1": 200, "x2": 700, "y2": 500, "op": "median"}
```

Reply:

```json
{"depth_mm": 1834, "valid_pixels": 14221, "total_pixels": 90000, "ts_ns": 1713372000000000000}
```

`op` ∈ `{"median", "mean", "min"}` (default `"median"`). If the device has no stereo cameras, the queryable is not registered and the topic stays silent.

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
