# SHM Raw Frame Pipeline Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate double GPU decode by having the camera node decode H264 once and publish raw RGBA frames over Zenoh SHM, while keeping the existing H264 compressed topic; the detector subscribes to the raw topic directly.

**Architecture:** A GStreamer `tee` in the camera node forks the pipeline — one branch outputs H264 as today, the other decodes via `nvv4l2decoder → nvvidconv → RGBA` and publishes to a Zenoh SHM topic. The detector drops its internal `H264DecoderCUDA` entirely and consumes the RGBA bytes directly as a torch tensor. Both nodes derive their shared topic key from their `name` by convention (`tapo_terrace_camera` → `tapo_terrace`). SHM is hardcoded — not a config option.

**Tech Stack:** Rust (rtsp-camera), Python (rfdetr-detector), GStreamer 1.0, zenoh 1.x Rust SHM API (`PosixShmProviderBuilder`), zenoh-python (SHM-enabled session), protobuf (`RawImage` message).

---

## Naming Convention

Topic key is derived from the node `name` by stripping the role suffix:

| Node name | Strip | Topic key | Topics |
|---|---|---|---|
| `tapo_terrace_camera` | `_camera` | `tapo_terrace` | publishes `tapo_terrace/compressed`, `tapo_terrace/raw` |
| `tapo_terrace_detector` | `_detector` | `tapo_terrace` | subscribes `tapo_terrace/raw`, publishes `tapo_terrace/detections` |

**Validation:** camera `name` must end in `_camera`; detector `name` must end in `_detector`. Config error otherwise.

The `publish_topic` and `subscribe_topic` fields are removed from both configs — all routing is implicit from the name.

---

## Config Files (final)

```yaml
# rtsp-camera/configs/terrace.yaml
name: tapo_terrace_camera
url: "rtsp://user:pass@192.168.x.x:554/stream"
latency: 200
```

```yaml
# rfdetr-detector/configs/tapo_terrace.yaml
name: tapo_terrace_detector
confidence_threshold: 0.7
model: large
target_fps: 1.0
```

No `publish_topic`, `subscribe_topic`, or `shm` fields — removed entirely.

---

## GStreamer Pipeline (camera node)

Current pipeline (single branch):
```
rtspsrc → rtph264depay → h264parse → appsink(H264)
```

New pipeline (tee):
```
rtspsrc → rtph264depay → h264parse → tee
  → queue → appsink(H264)              →  Zenoh: tapo_terrace/compressed
  → queue → nvv4l2decoder → nvvidconv
          → video/x-raw,format=RGBA
          → appsink(RGBA)              →  Zenoh SHM: tapo_terrace/raw
```

The RGBA appsink fires at the camera's native frame rate. The H264 branch is unchanged — same frame-rate limiting logic as today.

---

## Proto: RawImage

New message added to `rtsp-camera/protos/rtsp_camera.proto`:

```protobuf
message RawImage {
  bubbaloop.header.v1.Header header = 1;
  uint32 width    = 2;
  uint32 height   = 3;
  string encoding = 4;  // always "rgba8"
  bytes  data     = 5;  // width * height * 4 bytes, row-major
}
```

At 1080p: 1920 × 1080 × 4 = 8,294,400 bytes (~8 MB) per frame. Zenoh SHM delivers this zero-copy to same-machine subscribers.

---

## Zenoh SHM — Camera Node (Rust)

Session must have SHM enabled. The `bubbaloop-node` SDK's `zenoh_session.rs` gains a `with_shm()` builder method used by the camera node's `init()`:

```rust
// In H264StreamCapture or RtspCameraNode::init():
let provider = PosixShmProviderBuilder::builder()
    .size(SHM_POOL_SIZE)  // e.g. 64 MB — room for ~8 frames at 1080p
    .res()?;
```

Publishing a raw frame:
```rust
let mut shm_buf = provider.alloc(frame_bytes.len()).await?;
shm_buf.copy_from_slice(frame_bytes);
let payload = ZBytes::from(shm_buf);
raw_pub.put(payload).await?;
```

The existing `ProtoPublisher<CompressedImage>` is unchanged.

---

## Zenoh SHM — Detector Node (Python)

Session must have SHM enabled in the zenoh config:
```python
conf = zenoh.Config()
conf.insert_json5('transport/shared_memory/enabled', 'true')
```

The `bubbaloop_sdk.run_node()` must accept a way for the node to pass a custom config — or the detector calls `zenoh.open(conf)` directly before handing off to `run_node`. **This interface needs to be resolved during implementation** — check current `run_node()` signature in `python-sdk/`.

Subscribing is otherwise unchanged:
```python
def _on_raw_frame(sample) -> None:
    # sample.payload is a ZBytes backed by SHM — zero-copy on same machine
    raw_bytes = bytes(sample.payload)   # single memcpy into Python bytes
    msg = RawImage()
    msg.ParseFromString(raw_bytes)
    rgba = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 4).copy()
    tensor = (
        torch.from_numpy(rgba[:, :, :3])   # drop alpha
        .permute(2, 0, 1)
        .to(dtype=torch.float32, device="cuda", non_blocking=True)
        .div_(255.0)
    )
    with self._frame_lock:
        self._latest_frame = tensor
```

`H264DecoderCUDA` class is deleted entirely from `main.py`.

---

## Changes Summary

### rtsp-camera (Rust)

| File | Change |
|---|---|
| `protos/rtsp_camera.proto` | Add `RawImage` message |
| `src/config.rs` | Remove `publish_topic`; add `name` suffix validation (`_camera` required); derive topic key |
| `src/h264_capture.rs` | Add `RgbaFrame` struct; add tee branch with `nvv4l2decoder → nvvidconv → RGBA appsink` |
| `src/rtsp_camera_node.rs` | Add SHM provider + `raw_pub: ShmProtoPublisher<RawImage>`; derive topics from name |
| `Cargo.toml` | Verify zenoh SHM deps (feature `shared-memory`) |

### rfdetr-detector (Python)

| File | Change |
|---|---|
| `main.py` | Delete `H264DecoderCUDA`; subscribe to `{topic_key}/raw`; parse `RawImage` proto; enable SHM session |
| `configs/tapo_terrace.yaml` | Remove `subscribe_topic`, `publish_topic`; rename to `name: tapo_terrace_detector` |
| `pixi.toml` | Add protobuf dependency if not present for `RawImage` decode |

### bubbaloop-node SDK (Rust)

| File | Change |
|---|---|
| `crates/bubbaloop-node/src/zenoh_session.rs` | Add SHM-enabled session builder path |
| `crates/bubbaloop-node/src/context.rs` | Expose `shm_provider()` for nodes that need SHM publishing |

---

## Error Handling

- If the RGBA appsink produces no frames (e.g. nvv4l2decoder unavailable), the camera node logs an error and continues publishing H264 only — it does not crash.
- If the detector receives a `RawImage` with mismatched `width * height * 4 != len(data)`, it drops the frame and logs a warning.
- The existing inference error handling in `Detector.detect()` is unchanged.

---

## Testing

1. **Camera unit test:** `Config::parse()` rejects names not ending in `_camera`; verifies topic key derivation.
2. **Detector unit test:** `Config` rejects names not ending in `_detector`; verifies topic key derivation.
3. **Live integration:** `journalctl` shows detector `seq=N detections=M infer=Xms` at exactly 1 Hz with no H264 decode log lines.
4. **SHM verification:** `ls -la /dev/shm/` shows the zenoh SHM segments while both nodes are running.
