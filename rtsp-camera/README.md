# rtsp-camera

Bubbaloop node that captures H.264 from an RTSP camera and republishes:
- compressed H.264 (Annex-B byte-stream) on `bubbaloop/global/{machine}/{instance}/compressed`
- decoded RGBA frames (resized) on `bubbaloop/local/{machine}/{instance}/raw` (SHM, same-machine only)

Single-camera-per-process. Multi-camera deployments register one instance per camera (see [Multi-instance](#multi-instance)).

---

## Decision flow: how to pick `hw_accel` and configure the GStreamer ABI

This node uses GStreamer for the RTSP → H.264 → RGBA pipeline. The runtime needs decoder + scaler plugins to be present in the **same GStreamer ABI** the binary was compiled against. Mixing host and pixi-installed plugins crashes on duplicate type registration. Pick **one** ABI per deployment.

### Step 1 — Pick the decoder

| `hw_accel` | When to use | Plugin needed |
|---|---|---|
| `cpu` (default, portable) | Anywhere — laptop, server, container, Jetson without NVIDIA stack | `avdec_h264` (always shipped with `gst-libav`) |
| `nvidia` (Jetson VIC, fast) | NVIDIA Jetson (Orin, Xavier, Nano) with the Jetson GStreamer stack installed | `nvv4l2decoder`, `nvvidconv` (only in the host `gstreamer-1.0/` dir on Jetson images) |

**Runtime fallback**: if the config asks for `nvidia` but `nvv4l2decoder` is not registered, the node logs a warning and falls back to `cpu` automatically. So an agent can safely set `hw_accel: nvidia` when the platform might be Jetson — non-Jetson hosts will still run.

### Step 2 — Pick the GStreamer ABI for the binary

Two builds are possible. They look identical from the user, but resolve plugins from different paths:

#### Path A — Pixi-only (default for `pixi run build`)

The Cargo build links against pixi's `gstreamer-1.0` (`.pixi/envs/default/lib/`). The pixi env ships `gst-libav` (so `avdec_h264` works) but not the NVIDIA Jetson decoders.

```bash
pixi install            # one-time
pixi run build          # cargo build --release
```

Use this when:
- You don't need Jetson hardware decode, OR
- You're on a non-Jetson host (Mac, x86 server, container without `/usr/lib/.../nvv4l2decoder`)

Result: `hw_accel: cpu` works out of the box. `hw_accel: nvidia` falls back to CPU with a warning.

#### Path B — Host GStreamer (Jetson with NVIDIA accelerators)

Drop pixi for this node and build against the system GStreamer:

```bash
sudo apt install -y libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
                    gstreamer1.0-tools gstreamer1.0-plugins-{base,good,bad,ugly} \
                    gstreamer1.0-libav
# On Jetson, the NVIDIA plugins (nvv4l2decoder, nvvidconv) are pre-installed
# in /usr/lib/aarch64-linux-gnu/gstreamer-1.0/ — no extra install needed.

cargo build --release --bin rtsp_camera_node    # NOT under `pixi run`
```

Then bake the host plugin path into the systemd unit using bubbaloop's `environment:` field on `node.yaml` (kornia/bubbaloop PR #85):

```yaml
# rtsp-camera/node.yaml
environment:
  GST_PLUGIN_PATH: /usr/lib/aarch64-linux-gnu/gstreamer-1.0
```

Use this when you're on a Jetson and want true hardware-accelerated decode (5-10× lower CPU than software path).

#### Anti-pattern (DO NOT)

Setting `GST_PLUGIN_PATH` to **both** the host *and* pixi paths:

```yaml
environment:
  GST_PLUGIN_PATH: /usr/lib/aarch64-linux-gnu/gstreamer-1.0:/home/nvidia/.pixi/.../gstreamer-1.0  # ❌
```

This causes `cannot register existing type 'GstVideoScale'` and wedges the pipeline in init. The two GStreamer plugin sets have ABI-incompatible symbols (e.g. `gst_h264_picture_get_user_data` differs between the host gst-bad and pixi gst-bad versions).

### Step 3 — Verify which ABI a binary loaded

```bash
ldd ./target/release/rtsp_camera_node | grep gstreamer
```

If the path starts with `.pixi/envs/default/lib/` → Path A (pixi).
If the path starts with `/usr/lib/...` → Path B (host).

This decides what you can put in `GST_PLUGIN_PATH`.

### Quick agent decision tree

```
Are you targeting a Jetson and want NVIDIA hardware decode?
├── No → Path A (pixi). Set hw_accel: cpu (or nvidia — it'll fall back).
│       Don't set GST_PLUGIN_PATH.
└── Yes → Path B (host). Build cargo without pixi.
         Set hw_accel: nvidia.
         Bake GST_PLUGIN_PATH=/usr/lib/<arch>/gstreamer-1.0 in node.yaml.
```

---

## Configure

```yaml
# configs/<your-camera>.yaml
name: <unique_instance_name>     # Zenoh prefix; underscores not hyphens
url: rtsp://user:pass@host:554/stream1
latency: 50                       # rtspsrc jitter buffer ms
frame_rate: 10                    # for log/throttle reporting only
hw_accel: cpu                     # cpu | nvidia (see decision flow above)
raw_width: 560                    # RGBA target (pre-resize for inference)
raw_height: 560
```

`name` is the Zenoh prefix the camera publishes under — distinct from the bubbaloop registered name (which can be a friendly alias like `tapo-terrace`).

## Register and run via bubbaloop

```bash
# One-shot: build + register + install + start
bubbaloop node create rtsp-camera   # NOT for instances; use `add` per camera

# Per-camera instance
bubbaloop node add /path/to/rtsp-camera \
  -n tapo-terrace \
  -c configs/terrace.yaml \
  --install
bubbaloop node start tapo-terrace
```

## Topics

| Topic | Encoding | Notes |
|---|---|---|
| `bubbaloop/global/{machine}/{name}/compressed` | CBOR envelope; `body.format=h264`, `body.data=<NAL>` | Cross-machine consumable |
| `bubbaloop/local/{machine}/{name}/raw` | raw RGBA bytes (SHM zero-copy) | Same-machine only |
| `bubbaloop/global/{machine}/{name}/health` | JSON heartbeat | Drives health monitor |

## Multi-instance

One process per camera. To run N cameras, register N instances against the same base node:

```bash
bubbaloop node add /path/to/rtsp-camera -n tapo-terrace  -c configs/terrace.yaml  --install
bubbaloop node add /path/to/rtsp-camera -n tapo-entrance -c configs/entrance.yaml --install
bubbaloop node start tapo-terrace tapo-entrance
```

Each instance gets its own systemd unit and its own Zenoh prefix (driven by the `name` field in its config).

## Troubleshooting

**"no element 'nvv4l2decoder'"**
You're using Path A (pixi) but requested `hw_accel: nvidia`. The runtime fallback handles this — you'll see a `WARN` line and the node continues with CPU. Either accept it or switch to Path B.

**"cannot register existing type 'GstVideoScale'" / pipeline wedges in init**
Anti-pattern from Step 2 — `GST_PLUGIN_PATH` is mixing two ABIs. Pick one.

**Camera shows `(Running, Starting)` health forever**
The published Zenoh `name` (config) doesn't match what the daemon expects. The daemon (kornia/bubbaloop ≥ PR #85) matches heartbeats against both the registered name *and* the published instance name, so this should resolve. If it persists, check `bubbaloop node logs <name>` for a heartbeat-publish failure.
