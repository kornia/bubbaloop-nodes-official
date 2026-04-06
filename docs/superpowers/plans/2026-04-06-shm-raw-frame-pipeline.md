# SHM Raw Frame Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate double GPU H264 decode by having the camera node publish raw RGBA frames over Zenoh SHM (`{topic_key}/raw`), while the detector subscribes directly without any GStreamer decode.

**Architecture:** GStreamer `tee` in `rtsp-camera` forks the pipeline — H264 branch unchanged, new RGBA branch decodes once via `nvv4l2decoder → nvvidconv → RGBA` and publishes via Zenoh SHM. Detector deletes `H264DecoderCUDA` entirely and reads proto bytes from SHM into a torch tensor. Both nodes derive their shared topic key by stripping the `_camera`/`_detector` suffix from their `name` config field — zero cross-references between configs.

**Tech Stack:** zenoh 1.8 (`shared-memory` + `unstable` features), GStreamer 0.24 (Rust), `PosixShmProviderBackend` + `ShmProviderBuilder`, Python `google.protobuf`, `RawImage` proto (already in `rtsp-camera/protos/camera.proto`).

**Repos:** `~/bubbaloop` (SDK changes — Tasks 1–2) and `~/bubbaloop-nodes-official` (node changes — Tasks 3–8).

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `~/bubbaloop/crates/bubbaloop-node/Cargo.toml` | Modify | Add zenoh `shared-memory` + `unstable` features |
| `~/bubbaloop/crates/bubbaloop-node/src/zenoh_session.rs` | Modify | Add `shm: bool` param, enable SHM transport when true |
| `~/bubbaloop/crates/bubbaloop-node/src/lib.rs` | Modify | Add `fn shm() -> bool { false }` to `Node` trait; pass to session builder |
| `~/bubbaloop/python-sdk/bubbaloop_sdk/context.py` | Modify | Add `shm: bool = False` to `NodeContext.connect()` |
| `~/bubbaloop/python-sdk/bubbaloop_sdk/node.py` | Modify | Check `getattr(node_class, 'shm', False)` in `run_node()` |
| `rtsp-camera/Cargo.toml` | Modify | Add zenoh with SHM features directly (needed for import) |
| `rtsp-camera/src/config.rs` | Modify | Remove `publish_topic`; add `_camera` suffix validation; add `topic_key()` |
| `rtsp-camera/src/h264_capture.rs` | Modify | Add `RgbaFrame`; tee pipeline; second RGBA appsink |
| `rtsp-camera/src/rtsp_camera_node.rs` | Modify | Override `fn shm()`; add SHM provider + raw publisher |
| `rtsp-camera/src/proto.rs` | Modify | Add `MessageTypeName` impl for `RawImage` |
| `rtsp-camera/configs/terrace.yaml` | Modify | `name: tapo_terrace_camera`, remove `publish_topic` |
| `rtsp-camera/configs/entrance.yaml` | Modify | `name: tapo_entrance_camera`, remove `publish_topic` |
| `rfdetr-detector/protos/camera.proto` | Create | Copy from rtsp-camera (RawImage schema) |
| `rfdetr-detector/protos/header.proto` | Create | Copy from bubbaloop-node (Header schema) |
| `rfdetr-detector/protos/camera_pb2.py` | Create | Generated Python protobuf bindings |
| `rfdetr-detector/pixi.toml` | Modify | Add explicit `protobuf` dep |
| `rfdetr-detector/main.py` | Modify | Delete `H264DecoderCUDA`; subscribe to raw; parse `RawImage` |
| `rfdetr-detector/configs/tapo_terrace.yaml` | Modify | Remove `subscribe_topic`/`publish_topic` |

---

## Task 1: Rust SDK — SHM session support

**Repo:** `~/bubbaloop`
**Files:** `crates/bubbaloop-node/Cargo.toml`, `src/zenoh_session.rs`, `src/lib.rs`

- [ ] **Step 1: Add zenoh SHM features to SDK Cargo.toml**

  In `~/bubbaloop/crates/bubbaloop-node/Cargo.toml`, change:
  ```toml
  zenoh = "1.7"
  ```
  to:
  ```toml
  zenoh = { version = "1.7", features = ["shared-memory", "unstable"] }
  ```

- [ ] **Step 2: Add `shm` param to `open_zenoh_session`**

  Replace the signature and add SHM config in `crates/bubbaloop-node/src/zenoh_session.rs`:
  ```rust
  pub async fn open_zenoh_session(endpoint: &Option<String>, shm: bool) -> Result<Arc<zenoh::Session>> {
      let endpoint = std::env::var("ZENOH_ENDPOINT")
          .or_else(|_| std::env::var("BUBBALOOP_ZENOH_ENDPOINT"))
          .ok()
          .or_else(|| endpoint.clone())
          .unwrap_or_else(|| "tcp/127.0.0.1:7447".to_string());

      log::info!("Connecting to Zenoh at: {}", endpoint);

      let mut config = zenoh::Config::default();
      config.insert_json5("mode", r#""client""#).map_err(|e| NodeError::ZenohConfig { key: "mode", source: e })?;
      config.insert_json5("connect/endpoints", &format!(r#"["{}"]"#, endpoint)).map_err(|e| NodeError::ZenohConfig { key: "connect/endpoints", source: e })?;
      config.insert_json5("scouting/multicast/enabled", "false").map_err(|e| NodeError::ZenohConfig { key: "scouting/multicast/enabled", source: e })?;
      config.insert_json5("scouting/gossip/enabled", "false").map_err(|e| NodeError::ZenohConfig { key: "scouting/gossip/enabled", source: e })?;
      if shm {
          config.insert_json5("transport/shared_memory/enabled", "true").map_err(|e| NodeError::ZenohConfig { key: "transport/shared_memory/enabled", source: e })?;
          log::info!("Zenoh SHM transport enabled");
      }

      let session = zenoh::open(config).await.map_err(NodeError::ZenohSession)?;
      log::info!("Connected to Zenoh");
      Ok(Arc::new(session))
  }
  ```

- [ ] **Step 3: Add `fn shm()` to `Node` trait and wire into `run_node`**

  In `crates/bubbaloop-node/src/lib.rs`:

  Add to the `Node` trait (after `fn descriptor()`):
  ```rust
  /// Return true if this node requires a Zenoh SHM-enabled session.
  /// Default: false. Override to true for nodes that publish/subscribe over SHM.
  fn shm() -> bool {
      false
  }
  ```

  In `run_node()`, change the session line from:
  ```rust
  let session = zenoh_session::open_zenoh_session(&args.endpoint).await?;
  ```
  to:
  ```rust
  let session = zenoh_session::open_zenoh_session(&args.endpoint, N::shm()).await?;
  ```

- [ ] **Step 4: Verify compile**
  ```bash
  cd ~/bubbaloop
  pixi run check
  ```
  Expected: no errors.

- [ ] **Step 5: Commit and push to main**
  ```bash
  cd ~/bubbaloop
  git add crates/bubbaloop-node/Cargo.toml crates/bubbaloop-node/src/zenoh_session.rs crates/bubbaloop-node/src/lib.rs
  git commit -m "feat(node-sdk): add SHM session support via Node::shm() trait method"
  git push origin main
  ```

---

## Task 2: Python SDK — SHM session support

**Repo:** `~/bubbaloop`
**Files:** `python-sdk/bubbaloop_sdk/context.py`, `python-sdk/bubbaloop_sdk/node.py`

- [ ] **Step 1: Add `shm` param to `NodeContext.connect()`**

  In `python-sdk/bubbaloop_sdk/context.py`, change the `connect` classmethod signature and body:
  ```python
  @classmethod
  def connect(
      cls,
      endpoint: str | None = None,
      instance_name: str | None = None,
      shm: bool = False,
  ) -> "NodeContext":
      scope = os.environ.get("BUBBALOOP_SCOPE", "local")
      machine_id = os.environ.get("BUBBALOOP_MACHINE_ID", _hostname())
      ep = endpoint or os.environ.get("BUBBALOOP_ZENOH_ENDPOINT", "tcp/127.0.0.1:7447")
      name = instance_name or machine_id

      conf = zenoh.Config()
      conf.insert_json5("mode", '"client"')
      conf.insert_json5("connect/endpoints", f'["{ep}"]')
      conf.insert_json5("scouting/multicast/enabled", "false")
      conf.insert_json5("scouting/gossip/enabled", "false")
      if shm:
          conf.insert_json5("transport/shared_memory/enabled", "true")
      session = zenoh.open(conf)

      return cls(session, scope, machine_id, name)
  ```

- [ ] **Step 2: Check node class `shm` attribute in `run_node()`**

  In `python-sdk/bubbaloop_sdk/node.py`, change the `ctx = NodeContext.connect(...)` line to:
  ```python
  shm_enabled = getattr(node_class, "shm", False)
  ctx = NodeContext.connect(endpoint=args.endpoint, instance_name=instance_name, shm=shm_enabled)
  ```

- [ ] **Step 3: Commit and push**
  ```bash
  cd ~/bubbaloop
  git add python-sdk/bubbaloop_sdk/context.py python-sdk/bubbaloop_sdk/node.py
  git commit -m "feat(python-sdk): add SHM session support via node class 'shm' attribute"
  git push origin main
  ```

---

## Task 3: Camera config refactor — naming convention

**Repo:** `~/bubbaloop-nodes-official`
**Files:** `rtsp-camera/src/config.rs`, `rtsp-camera/configs/terrace.yaml`, `rtsp-camera/configs/entrance.yaml`

- [ ] **Step 1: Update camera configs**

  `rtsp-camera/configs/terrace.yaml`:
  ```yaml
  name: tapo_terrace_camera
  url: "rtsp://tapo_terrace:clawd2026@192.168.1.151:554/stream1"
  latency: 50
  frame_rate: 10
  ```

  `rtsp-camera/configs/entrance.yaml`:
  ```yaml
  name: tapo_entrance_camera
  url: "rtsp://tapo_entrance:clawd2026@192.168.1.141:554/stream1"
  latency: 50
  frame_rate: 10
  ```

- [ ] **Step 2: Rewrite `config.rs`**

  Replace the full contents of `rtsp-camera/src/config.rs`:
  ```rust
  use serde::{Deserialize, Serialize};
  use std::path::Path;

  /// Configuration for a single RTSP camera instance.
  /// `name` must end in `_camera` — the topic key is derived by stripping that suffix.
  #[derive(Debug, Clone, Serialize, Deserialize)]
  pub struct Config {
      /// Instance name, e.g. `tapo_terrace_camera`.
      /// Must end in `_camera`. Topic key = name without `_camera`.
      pub name: String,
      /// RTSP URL (or set via RTSP_URL env var)
      pub url: String,
      /// Latency in milliseconds for the RTSP stream
      #[serde(default = "default_latency")]
      pub latency: u32,
      /// Optional frame rate limit
      #[serde(default)]
      pub frame_rate: Option<u32>,
  }

  fn default_latency() -> u32 {
      200
  }

  impl Config {
      pub fn from_file(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
          let contents = std::fs::read_to_string(path.as_ref())
              .map_err(|e| ConfigError::IoError(e.to_string()))?;
          Self::parse(&contents)
      }

      pub fn parse(yaml: &str) -> Result<Self, ConfigError> {
          let config: Config =
              serde_yaml::from_str(yaml).map_err(|e| ConfigError::ParseError(e.to_string()))?;
          config.validate()?;
          Ok(config)
      }

      pub fn validate(&self) -> Result<(), ConfigError> {
          if !self.name.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-') || self.name.is_empty() {
              return Err(ConfigError::ValidationError(format!(
                  "name '{}': only [a-zA-Z0-9_-] allowed", self.name
              )));
          }
          if !self.name.ends_with("_camera") {
              return Err(ConfigError::ValidationError(format!(
                  "name '{}' must end in '_camera' (e.g. tapo_terrace_camera)", self.name
              )));
          }
          if self.url.is_empty() {
              return Err(ConfigError::ValidationError("url must not be empty".to_string()));
          }
          if self.latency == 0 || self.latency > 10000 {
              return Err(ConfigError::ValidationError(format!(
                  "latency {} out of range (1–10000 ms)", self.latency
              )));
          }
          Ok(())
      }

      /// Returns the shared topic key by stripping `_camera` from the name.
      /// `tapo_terrace_camera` → `tapo_terrace`
      pub fn topic_key(&self) -> &str {
          self.name.strip_suffix("_camera").unwrap()
      }
  }

  #[derive(Debug, thiserror::Error)]
  pub enum ConfigError {
      #[error("IO error: {0}")]
      IoError(String),
      #[error("Parse error: {0}")]
      ParseError(String),
      #[error("Validation error: {0}")]
      ValidationError(String),
  }

  #[cfg(test)]
  mod tests {
      use super::*;

      #[test]
      fn test_topic_key_derivation() {
          let yaml = r#"
  name: tapo_terrace_camera
  url: "rtsp://192.168.1.10:554/stream"
  "#;
          let config = Config::parse(yaml).unwrap();
          assert_eq!(config.topic_key(), "tapo_terrace");
      }

      #[test]
      fn test_rejects_name_without_camera_suffix() {
          let yaml = r#"
  name: tapo_terrace
  url: "rtsp://192.168.1.10:554/stream"
  "#;
          let err = Config::parse(yaml).unwrap_err().to_string();
          assert!(err.contains("_camera"), "error should mention _camera: {err}");
      }

      #[test]
      fn test_rejects_empty_url() {
          let yaml = r#"
  name: tapo_terrace_camera
  url: ""
  "#;
          assert!(Config::parse(yaml).is_err());
      }

      #[test]
      fn test_default_latency() {
          let yaml = r#"
  name: tapo_terrace_camera
  url: "rtsp://192.168.1.10:554/stream"
  "#;
          let config = Config::parse(yaml).unwrap();
          assert_eq!(config.latency, 200);
          assert_eq!(config.topic_key(), "tapo_terrace");
      }

      #[test]
      fn test_backward_compat_ignores_unknown_fields() {
          let yaml = r#"
  name: tapo_terrace_camera
  url: "rtsp://192.168.1.10:554/stream"
  publish_topic: camera/old/topic
  "#;
          let config = Config::parse(yaml).unwrap();
          assert_eq!(config.name, "tapo_terrace_camera");
      }
  }
  ```

- [ ] **Step 3: Update `rtsp_camera_node.rs` to use `topic_key()` instead of `publish_topic`**

  In `rtsp_camera_node.rs`, change:
  ```rust
  let camera_name = self.config.name.clone();
  let publish_topic = self.config.publish_topic.clone();
  // ...
  let compressed_pub: ProtoPublisher<CompressedImage> =
      ctx.publisher_proto(&publish_topic).await?;
  log::info!("[{}] Publishing to: {}", camera_name, ctx.topic(&publish_topic));
  ```
  to:
  ```rust
  let camera_name = self.config.name.clone();
  let topic_key = self.config.topic_key().to_string();
  let compressed_topic = format!("{}/compressed", topic_key);
  // ...
  let compressed_pub: ProtoPublisher<CompressedImage> =
      ctx.publisher_proto(&compressed_topic).await?;
  log::info!("[{}] Publishing compressed to: {}", camera_name, ctx.topic(&compressed_topic));
  ```

- [ ] **Step 4: Check compile**
  ```bash
  cd ~/bubbaloop-nodes-official/rtsp-camera
  cargo check 2>&1 | head -30
  ```
  Expected: no errors.

- [ ] **Step 5: Run tests**
  ```bash
  cd ~/bubbaloop-nodes-official/rtsp-camera
  cargo test --lib 2>&1 | tail -20
  ```
  Expected: all tests pass.

- [ ] **Step 6: Commit**
  ```bash
  cd ~/bubbaloop-nodes-official
  git add rtsp-camera/src/config.rs rtsp-camera/src/rtsp_camera_node.rs rtsp-camera/configs/
  git commit -m "refactor(rtsp-camera): derive topic key from _camera suffix, remove publish_topic config field"
  ```

---

## Task 4: Camera — GStreamer tee + RGBA appsink

**Files:** `rtsp-camera/src/h264_capture.rs`

`★ Insight ─────────────────────────────────────`
GStreamer's `tee` element is a fan-out — it duplicates buffers to N downstream pads. Each branch gets a `queue` to decouple the branches: without it, a slow downstream stalls the entire pipeline. `leaky=downstream` on the queues means when full, the NEWEST buffer is dropped (not oldest), keeping latency low for live video.
`─────────────────────────────────────────────────`

- [ ] **Step 1: Write failing test for RgbaFrame**

  Add to the bottom of `rtsp-camera/src/h264_capture.rs`:
  ```rust
  #[cfg(test)]
  mod tests {
      use super::*;

      #[test]
      fn rgba_frame_size() {
          let frame = RgbaFrame {
              data: vec![0u8; 1920 * 1080 * 4],
              width: 1920,
              height: 1080,
              sequence: 0,
              pts: 0,
          };
          assert_eq!(frame.data.len(), (frame.width * frame.height * 4) as usize);
      }
  }
  ```

- [ ] **Step 2: Run test — expect compile error (RgbaFrame not defined yet)**
  ```bash
  cd ~/bubbaloop-nodes-official/rtsp-camera
  cargo test rgba_frame_size 2>&1 | head -20
  ```

- [ ] **Step 3: Replace `h264_capture.rs` with tee version**

  Full replacement of `rtsp-camera/src/h264_capture.rs`:
  ```rust
  use gstreamer::prelude::*;
  use thiserror::Error;

  #[derive(Debug, Error)]
  pub enum H264CaptureError {
      #[error("GStreamer error: {0}")]
      GStreamer(#[from] gstreamer::glib::Error),
      #[error("GStreamer state change error: {0}")]
      StateChange(#[from] gstreamer::StateChangeError),
      #[error("Element not found: {0}")]
      ElementNotFound(&'static str),
      #[error("Failed to downcast")]
      DowncastError,
      #[error("Buffer error")]
      BufferError,
  }

  /// H264 frame (zero-copy from GStreamer)
  pub struct H264Frame {
      buffer: gstreamer::MappedBuffer<gstreamer::buffer::Readable>,
      pub pts: u64,
      pub keyframe: bool,
      pub sequence: u32,
  }

  impl H264Frame {
      pub fn as_slice(&self) -> &[u8] { self.buffer.as_slice() }
      pub fn len(&self) -> usize { self.buffer.len() }
      pub fn is_empty(&self) -> bool { self.buffer.is_empty() }
  }

  /// Decoded RGBA frame from the GPU (nvv4l2decoder → nvvidconv)
  pub struct RgbaFrame {
      pub data: Vec<u8>,
      pub width: u32,
      pub height: u32,
      pub sequence: u32,
      pub pts: u64,
  }

  /// Captures H264 and decoded RGBA from RTSP via a GStreamer tee pipeline.
  ///
  /// Pipeline:
  ///   rtspsrc → rtph264depay → h264parse → tee
  ///     → queue → appsink (H264, raw bytes)
  ///     → queue → nvv4l2decoder → nvvidconv → RGBA → appsink (decoded frames)
  pub struct H264StreamCapture {
      pipeline: gstreamer::Pipeline,
      h264_rx: flume::Receiver<H264Frame>,
      rgba_rx: flume::Receiver<RgbaFrame>,
  }

  impl H264StreamCapture {
      pub fn new(url: &str, latency: u32) -> Result<Self, H264CaptureError> {
          if !gstreamer::INITIALIZED.load(std::sync::atomic::Ordering::Relaxed) {
              gstreamer::init()?;
          }

          let pipeline_desc = format!(
              "rtspsrc location={url} latency={latency} ! \
               rtph264depay ! h264parse config-interval=-1 ! \
               video/x-h264,stream-format=byte-stream,alignment=au ! \
               tee name=t \
               t. ! queue max-size-buffers=2 leaky=downstream ! \
                   appsink name=h264sink emit-signals=true sync=false max-buffers=2 drop=true \
               t. ! queue max-size-buffers=2 leaky=downstream ! \
                   nvv4l2decoder ! nvvidconv ! video/x-raw,format=RGBA ! \
                   appsink name=rgbasink emit-signals=true sync=false max-buffers=2 drop=true"
          );

          let pipeline = gstreamer::parse::launch(&pipeline_desc)?
              .dynamic_cast::<gstreamer::Pipeline>()
              .map_err(|_| H264CaptureError::DowncastError)?;

          let (h264_tx, h264_rx) = flume::unbounded::<H264Frame>();
          let (rgba_tx, rgba_rx) = flume::unbounded::<RgbaFrame>();

          // Wire H264 appsink
          let h264_sink = pipeline
              .by_name("h264sink")
              .ok_or(H264CaptureError::ElementNotFound("h264sink"))?
              .dynamic_cast::<gstreamer_app::AppSink>()
              .map_err(|_| H264CaptureError::DowncastError)?;

          h264_sink.set_callbacks(
              gstreamer_app::AppSinkCallbacks::builder()
                  .new_sample({
                      let mut sequence: u32 = 0;
                      move |sink| {
                          if let Ok(frame) = Self::handle_h264_sample(sink, sequence) {
                              sequence = sequence.wrapping_add(1);
                              let _ = h264_tx.try_send(frame);
                          }
                          Ok(gstreamer::FlowSuccess::Ok)
                      }
                  })
                  .build(),
          );

          // Wire RGBA appsink
          let rgba_sink = pipeline
              .by_name("rgbasink")
              .ok_or(H264CaptureError::ElementNotFound("rgbasink"))?
              .dynamic_cast::<gstreamer_app::AppSink>()
              .map_err(|_| H264CaptureError::DowncastError)?;

          rgba_sink.set_callbacks(
              gstreamer_app::AppSinkCallbacks::builder()
                  .new_sample({
                      let mut sequence: u32 = 0;
                      move |sink| {
                          if let Ok(frame) = Self::handle_rgba_sample(sink, sequence) {
                              sequence = sequence.wrapping_add(1);
                              let _ = rgba_tx.try_send(frame);
                          }
                          Ok(gstreamer::FlowSuccess::Ok)
                      }
                  })
                  .build(),
          );

          Ok(Self { pipeline, h264_rx, rgba_rx })
      }

      fn handle_h264_sample(
          sink: &gstreamer_app::AppSink,
          sequence: u32,
      ) -> Result<H264Frame, H264CaptureError> {
          let sample = sink.pull_sample().map_err(|_| H264CaptureError::BufferError)?;
          let buffer = sample.buffer_owned().ok_or(H264CaptureError::BufferError)?;
          let pts = buffer.pts().or_else(|| buffer.dts()).map(|t| t.nseconds()).unwrap_or(0);
          let keyframe = !buffer.flags().contains(gstreamer::BufferFlags::DELTA_UNIT);
          let mapped = buffer.into_mapped_buffer_readable().map_err(|_| H264CaptureError::BufferError)?;
          Ok(H264Frame { buffer: mapped, pts, keyframe, sequence })
      }

      fn handle_rgba_sample(
          sink: &gstreamer_app::AppSink,
          sequence: u32,
      ) -> Result<RgbaFrame, H264CaptureError> {
          let sample = sink.pull_sample().map_err(|_| H264CaptureError::BufferError)?;

          // Extract width and height from caps
          let caps = sample.caps().ok_or(H264CaptureError::BufferError)?;
          let structure = caps.structure(0).ok_or(H264CaptureError::BufferError)?;
          let width = structure.get::<i32>("width").map_err(|_| H264CaptureError::BufferError)? as u32;
          let height = structure.get::<i32>("height").map_err(|_| H264CaptureError::BufferError)? as u32;

          let buffer = sample.buffer_owned().ok_or(H264CaptureError::BufferError)?;
          let pts = buffer.pts().or_else(|| buffer.dts()).map(|t| t.nseconds()).unwrap_or(0);
          let mapped = buffer.into_mapped_buffer_readable().map_err(|_| H264CaptureError::BufferError)?;

          Ok(RgbaFrame {
              data: mapped.as_slice().to_vec(),
              width,
              height,
              sequence,
              pts,
          })
      }

      pub fn start(&self) -> Result<(), H264CaptureError> {
          self.pipeline.set_state(gstreamer::State::Playing)?;
          Ok(())
      }

      pub fn h264_receiver(&self) -> &flume::Receiver<H264Frame> { &self.h264_rx }
      pub fn rgba_receiver(&self) -> &flume::Receiver<RgbaFrame> { &self.rgba_rx }

      pub fn close(&self) -> Result<(), H264CaptureError> {
          let _ = self.pipeline.send_event(gstreamer::event::Eos::new());
          self.pipeline.set_state(gstreamer::State::Null)?;
          Ok(())
      }
  }

  impl Drop for H264StreamCapture {
      fn drop(&mut self) { let _ = self.close(); }
  }

  #[cfg(test)]
  mod tests {
      use super::*;

      #[test]
      fn rgba_frame_size() {
          let frame = RgbaFrame {
              data: vec![0u8; 1920 * 1080 * 4],
              width: 1920,
              height: 1080,
              sequence: 0,
              pts: 0,
          };
          assert_eq!(frame.data.len(), (frame.width * frame.height * 4) as usize);
      }
  }
  ```

- [ ] **Step 4: Fix existing code that calls `capture.receiver()`**

  In `rtsp_camera_node.rs`, change `capture.receiver().recv_async()` to `capture.h264_receiver().recv_async()`.

- [ ] **Step 5: Run test**
  ```bash
  cd ~/bubbaloop-nodes-official/rtsp-camera
  cargo test rgba_frame_size 2>&1 | tail -10
  ```
  Expected: `test rgba_frame_size ... ok`

- [ ] **Step 6: Compile check**
  ```bash
  cargo check 2>&1 | head -30
  ```
  Expected: no errors.

- [ ] **Step 7: Commit**
  ```bash
  cd ~/bubbaloop-nodes-official
  git add rtsp-camera/src/h264_capture.rs rtsp-camera/src/rtsp_camera_node.rs
  git commit -m "feat(rtsp-camera): add GStreamer tee + RGBA appsink for decoded raw frames"
  ```

---

## Task 5: Camera — SHM publisher for raw frames

**Files:** `rtsp-camera/Cargo.toml`, `rtsp-camera/src/proto.rs`, `rtsp-camera/src/rtsp_camera_node.rs`

`★ Insight ─────────────────────────────────────`
Zenoh SHM works at the **transport layer**: the publisher allocates in a POSIX SHM pool (`/dev/shm/`), puts a reference (not a copy) into the Zenoh message, and the subscriber on the same machine gets direct pointer access — zero kernel copies. The `BlockOn<GarbageCollect>` allocation policy means: if the pool is full, run GC (reclaim buffers the subscriber has finished with) before blocking. Without GC, a 64MB pool at ~8MB/frame would fill in 8 frames if subscribers are slow.
`─────────────────────────────────────────────────`

- [ ] **Step 1: Add zenoh SHM deps to camera's Cargo.toml**

  In `rtsp-camera/Cargo.toml`, add directly (Cargo unifies with the SDK's zenoh):
  ```toml
  zenoh = { version = "1.8", features = ["shared-memory", "unstable"] }
  ```

- [ ] **Step 2: Add `MessageTypeName` for `RawImage` in `proto.rs`**

  Append to `rtsp-camera/src/proto.rs`:
  ```rust
  impl bubbaloop_node::MessageTypeName for RawImage {
      fn type_name() -> &'static str {
          "bubbaloop.camera.v1.RawImage"
      }
  }
  ```

- [ ] **Step 3: Add SHM publisher to `rtsp_camera_node.rs`**

  Full replacement of `rtsp_camera_node.rs`:
  ```rust
  use crate::config::Config;
  use crate::h264_capture::{H264Frame, H264StreamCapture};
  use crate::proto::{CompressedImage, RawImage};
  use bubbaloop_node::publisher::ProtoPublisher;
  use bubbaloop_node::schemas::Header;
  use std::sync::Arc;
  use zenoh::Wait;
  use zenoh::bytes::{Encoding, ZBytes};
  use zenoh::shm::{BlockOn, GarbageCollect, PosixShmProviderBackend, ShmProviderBuilder};
  use prost::Message;

  // 64 MB SHM pool — room for ~8 frames at 1080p RGBA (~8.3 MB each with proto overhead)
  const SHM_POOL_BYTES: usize = 64 * 1024 * 1024;

  fn extract_nal_types(data: &[u8]) -> Vec<u8> {
      let mut nal_types = Vec::new();
      let mut i = 0;
      while i + 4 < data.len() {
          if data[i..i + 4] == [0, 0, 0, 1] {
              nal_types.push(data[i + 4] & 0x1F);
              i += 5;
          } else if data[i..i + 3] == [0, 0, 1] {
              nal_types.push(data[i + 3] & 0x1F);
              i += 4;
          } else {
              i += 1;
          }
      }
      nal_types
  }

  fn get_pub_time() -> u64 {
      std::time::SystemTime::now()
          .duration_since(std::time::UNIX_EPOCH)
          .map(|d| d.as_nanos() as u64)
          .unwrap_or(0)
  }

  fn make_header(acq_time: u64, sequence: u32, frame_id: &str, machine_id: &str, scope: &str) -> Header {
      Header {
          acq_time,
          pub_time: get_pub_time(),
          sequence,
          frame_id: frame_id.to_string(),
          machine_id: machine_id.to_string(),
          scope: scope.to_string(),
      }
  }

  pub struct RtspCameraNode {
      config: Config,
  }

  #[bubbaloop_node::async_trait::async_trait]
  impl bubbaloop_node::Node for RtspCameraNode {
      type Config = Config;

      fn name() -> &'static str { "rtsp-camera" }

      fn descriptor() -> &'static [u8] {
          include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"))
      }

      fn shm() -> bool { true }

      async fn init(_ctx: &bubbaloop_node::NodeContext, config: &Config) -> anyhow::Result<Self> {
          Ok(Self { config: config.clone() })
      }

      async fn run(self, ctx: bubbaloop_node::NodeContext) -> anyhow::Result<()> {
          let camera_name = self.config.name.clone();
          let topic_key = self.config.topic_key().to_string();
          let compressed_topic = format!("{}/compressed", topic_key);
          let raw_topic = ctx.topic(&format!("{}/raw", topic_key));

          let url = std::env::var("RTSP_URL").unwrap_or_else(|_| self.config.url.clone());
          let capture = Arc::new(H264StreamCapture::new(&url, self.config.latency)?);
          capture.start()?;

          log::info!("[{}] RTSP capture started (latency={}ms)", camera_name, self.config.latency);

          let compressed_pub: ProtoPublisher<CompressedImage> =
              ctx.publisher_proto(&compressed_topic).await?;
          log::info!("[{}] Compressed → {}", camera_name, ctx.topic(&compressed_topic));

          // SHM provider for raw RGBA frames
          let shm_backend = PosixShmProviderBackend::builder(SHM_POOL_BYTES)
              .wait()
              .map_err(|e| anyhow::anyhow!("SHM backend init failed: {:?}", e))?;
          let shm_provider = ShmProviderBuilder::backend(shm_backend).wait();

          let raw_pub = ctx.session
              .declare_publisher(&raw_topic)
              .encoding(Encoding::APPLICATION_PROTOBUF.with_schema("bubbaloop.camera.v1.RawImage"))
              .await?;
          log::info!("[{}] Raw RGBA → {}", camera_name, raw_topic);

          let frame_interval = self.config.frame_rate.map(|fps| {
              std::time::Duration::from_secs_f64(1.0 / fps as f64)
          });

          let mut shutdown_rx = ctx.shutdown_rx.clone();
          let mut published_compressed: u64 = 0;
          let mut published_raw: u64 = 0;
          let mut dropped: u64 = 0;
          let mut last_log = std::time::Instant::now();
          let mut last_compressed_count: u64 = 0;
          let mut next_frame_time = std::time::Instant::now();

          loop {
              tokio::select! {
                  biased;

                  _ = shutdown_rx.changed() => {
                      log::info!("[{}] Shutdown received", camera_name);
                      break;
                  }

                  result = capture.h264_receiver().recv_async() => {
                      match result {
                          Ok(h264_frame) => {
                              let now = std::time::Instant::now();
                              if let Some(interval) = frame_interval {
                                  if !h264_frame.keyframe && now < next_frame_time {
                                      dropped += 1;
                                      continue;
                                  }
                                  next_frame_time = std::cmp::max(next_frame_time + interval, now);
                              }

                              let sequence = h264_frame.sequence;

                              if published_compressed < 10 {
                                  let nal_types = extract_nal_types(h264_frame.as_slice());
                                  log::info!("[{}] pub={} seq={} size={} keyframe={} NALs={:?}",
                                      camera_name, published_compressed, sequence,
                                      h264_frame.len(), h264_frame.keyframe, nal_types);
                              }

                              let msg = CompressedImage {
                                  header: Some(make_header(h264_frame.pts, sequence, &camera_name, &ctx.machine_id, &ctx.scope)),
                                  format: "h264".to_string(),
                                  data: h264_frame.as_slice().into(),
                              };
                              if compressed_pub.put(&msg).await.is_ok() {
                                  published_compressed += 1;
                              }

                              let elapsed = last_log.elapsed();
                              if elapsed.as_secs() >= 1 {
                                  let fps = (published_compressed - last_compressed_count) as f64 / elapsed.as_secs_f64();
                                  log::info!("[{}] seq={} compressed={} raw={} fps={:.1} dropped={}",
                                      camera_name, sequence, published_compressed, published_raw, fps, dropped);
                                  last_compressed_count = published_compressed;
                                  last_log = std::time::Instant::now();
                              }
                          }
                          Err(_) => break,
                      }
                  }

                  result = capture.rgba_receiver().recv_async() => {
                      match result {
                          Ok(rgba_frame) => {
                              let raw_image = RawImage {
                                  header: Some(make_header(rgba_frame.pts, rgba_frame.sequence, &camera_name, &ctx.machine_id, &ctx.scope)),
                                  width: rgba_frame.width,
                                  height: rgba_frame.height,
                                  encoding: "rgba8".to_string(),
                                  step: rgba_frame.width * 4,
                                  data: rgba_frame.data,
                              };
                              let proto_bytes = raw_image.encode_to_vec();
                              match shm_provider.alloc_layout(proto_bytes.len()) {
                                  Ok(layout) => {
                                      match layout.alloc().with_policy::<BlockOn<GarbageCollect>>().await {
                                          Ok(mut sbuf) => {
                                              sbuf.as_mut().copy_from_slice(&proto_bytes);
                                              if raw_pub.put(ZBytes::from(sbuf)).await.is_ok() {
                                                  published_raw += 1;
                                              }
                                          }
                                          Err(e) => log::warn!("[{}] SHM alloc failed: {:?}", camera_name, e),
                                      }
                                  }
                                  Err(e) => log::warn!("[{}] SHM layout failed: {:?}", camera_name, e),
                              }
                          }
                          Err(_) => break,
                      }
                  }
              }
          }

          if let Err(e) = capture.close() {
              log::error!("[{}] Failed to close capture: {}", camera_name, e);
          }
          log::info!("[{}] Shutdown complete (compressed={} raw={})", camera_name, published_compressed, published_raw);
          Ok(())
      }
  }
  ```

- [ ] **Step 4: Compile check**
  ```bash
  cd ~/bubbaloop-nodes-official/rtsp-camera
  cargo check 2>&1 | head -40
  ```
  Expected: no errors.

- [ ] **Step 5: Commit**
  ```bash
  cd ~/bubbaloop-nodes-official
  git add rtsp-camera/Cargo.toml rtsp-camera/src/proto.rs rtsp-camera/src/rtsp_camera_node.rs
  git commit -m "feat(rtsp-camera): publish raw RGBA frames over Zenoh SHM at {topic_key}/raw"
  ```

---

## Task 6: Detector — proto setup

**Files:** `rfdetr-detector/protos/`, `rfdetr-detector/pixi.toml`

`★ Insight ─────────────────────────────────────`
Protobuf's Python bindings (`_pb2.py`) are generated once from `.proto` files and committed to the repo — they're just code. The generated file uses `google.protobuf` at runtime for serialization. Committing generated files is acceptable here because the proto schema rarely changes; any change to `camera.proto` must also regenerate `camera_pb2.py`.
`─────────────────────────────────────────────────`

- [ ] **Step 1: Create `rfdetr-detector/protos/` and copy proto files**
  ```bash
  mkdir -p ~/bubbaloop-nodes-official/rfdetr-detector/protos
  cp ~/bubbaloop-nodes-official/rtsp-camera/protos/camera.proto ~/bubbaloop-nodes-official/rfdetr-detector/protos/
  cp ~/bubbaloop/crates/bubbaloop-node/protos/header.proto ~/bubbaloop-nodes-official/rfdetr-detector/protos/
  ```

- [ ] **Step 2: Generate Python bindings**
  ```bash
  cd ~/bubbaloop-nodes-official/rfdetr-detector
  /home/nvidia/.pixi/bin/protoc \
    --python_out=protos \
    --pyi_out=protos \
    -I protos \
    protos/header.proto protos/camera.proto
  ```
  Expected: creates `protos/header_pb2.py`, `protos/header_pb2.pyi`, `protos/camera_pb2.py`, `protos/camera_pb2.pyi`.

- [ ] **Step 3: Create `protos/__init__.py`**
  ```bash
  touch ~/bubbaloop-nodes-official/rfdetr-detector/protos/__init__.py
  ```

- [ ] **Step 4: Verify import**
  ```bash
  cd ~/bubbaloop-nodes-official/rfdetr-detector
  .pixi/envs/default/bin/python -c "from protos.camera_pb2 import RawImage; m = RawImage(); m.width = 1920; print('RawImage OK, width =', m.width)"
  ```
  Expected: `RawImage OK, width = 1920`

- [ ] **Step 5: Add explicit protobuf dep to pixi.toml**

  In `rfdetr-detector/pixi.toml`, add under `[pypi-dependencies]`:
  ```toml
  protobuf = ">=4.21,<6.0"
  ```

- [ ] **Step 6: Commit proto files**
  ```bash
  cd ~/bubbaloop-nodes-official
  git add rfdetr-detector/protos/ rfdetr-detector/pixi.toml
  git commit -m "feat(rfdetr-detector): add camera proto + generated Python bindings"
  ```

---

## Task 7: Detector — wire raw subscriber, delete H264DecoderCUDA

**Files:** `rfdetr-detector/main.py`, `rfdetr-detector/configs/tapo_terrace.yaml`

- [ ] **Step 1: Update detector config**

  Replace `rfdetr-detector/configs/tapo_terrace.yaml`:
  ```yaml
  name: tapo_terrace_detector
  confidence_threshold: 0.7
  model: large
  target_fps: 1.0
  ```

- [ ] **Step 2: Write failing test for `_detector` suffix validation**

  In `rfdetr-detector/tests/test_main.py`, add:
  ```python
  def test_topic_key_from_detector_name():
      """Detector name must end in _detector; topic key is derived by stripping it."""
      config = load_config.__wrapped__({"name": "tapo_terrace_detector", "confidence_threshold": 0.5, "model": "base", "target_fps": 1.0})
      assert config["topic_key"] == "tapo_terrace"

  def test_rejects_name_without_detector_suffix():
      import pytest
      with pytest.raises(ValueError, match="_detector"):
          load_config.__wrapped__({"name": "tapo_terrace", "confidence_threshold": 0.5, "model": "base", "target_fps": 1.0})
  ```
  *(We'll add `__wrapped__` support in the next step.)*

- [ ] **Step 3: Rewrite `main.py`**

  Full replacement of `rfdetr-detector/main.py`:

  ```python
  #!/usr/bin/env python3
  """rfdetr-detector — RF-DETR object detection on decoded RGBA frames from Zenoh SHM.

  Subscribes to {topic_key}/raw (RawImage proto over SHM), runs RF-DETR inference
  at target_fps, and publishes detections as JSON to {topic_key}/detections.
  Topic key is derived from config `name` by stripping the `_detector` suffix.
  """

  import base64
  import json
  import logging
  import threading
  import time
  from typing import Optional

  import numpy as np
  import torch
  from bubbaloop_sdk import run_node
  from protos.camera_pb2 import RawImage
  from rfdetr import RFDETRBase, RFDETRLarge, RFDETRMedium, RFDETRNano, RFDETRSmall

  log = logging.getLogger("rf-detr-detector")

  _REQUIRED_FIELDS = ("name", "confidence_threshold", "model", "target_fps")
  _MODEL_CLASSES = {
      "nano": RFDETRNano,
      "small": RFDETRSmall,
      "base": RFDETRBase,
      "medium": RFDETRMedium,
      "large": RFDETRLarge,
  }


  def load_config(raw: dict) -> dict:
      """Validate config dict and inject derived fields."""
      for field in _REQUIRED_FIELDS:
          if field not in raw:
              raise ValueError(f"Missing required config field: {field!r}")
      name: str = raw["name"]
      if not name.endswith("_detector"):
          raise ValueError(f"name {name!r} must end in '_detector' (e.g. tapo_terrace_detector)")
      model_name = raw["model"]
      if model_name not in _MODEL_CLASSES:
          raise ValueError(f"model {model_name!r} must be one of {list(_MODEL_CLASSES)}")
      target_fps = float(raw["target_fps"])
      if target_fps <= 0:
          raise ValueError(f"target_fps must be > 0, got {target_fps}")
      return {
          **raw,
          "topic_key": name.removesuffix("_detector"),
          "target_fps": target_fps,
      }


  def build_payload(
      *,
      frame_id: str,
      machine_id: str,
      scope: str,
      sequence: int,
      detections: list[dict],
  ) -> str:
      return json.dumps({
          "header": {
              "frame_id": frame_id,
              "machine_id": machine_id,
              "scope": scope,
              "sequence": sequence,
              "pub_time": int(time.time_ns()),
          },
          "detections": detections,
      })


  class Detector:
      """RF-DETR inference wrapper. Runs on CUDA, optimised for inference."""

      def __init__(self, confidence_threshold: float = 0.5, model: str = "base") -> None:
          model_cls = _MODEL_CLASSES[model]
          log.info("Loading RF-DETR %s model on CUDA...", model)
          self._model = model_cls(device="cuda")
          self._model.optimize_for_inference()
          self._threshold = confidence_threshold
          log.info("RF-DETR %s ready (threshold=%.2f)", model, confidence_threshold)

      def detect(self, image: torch.Tensor) -> list[dict]:
          """Run inference on a CHW float32 CUDA tensor normalised to [0, 1].

          Returns list of dicts with keys: label, confidence, bbox (x1,y1,x2,y2 in pixels).
          """
          result = self._model.predict(image, threshold=self._threshold)
          detections = []
          for label, confidence, bbox in zip(result.labels, result.scores, result.boxes):
              detections.append({
                  "label": int(label),
                  "confidence": float(confidence),
                  "bbox": [float(v) for v in bbox],
              })
          return detections


  class RfDetrDetectorNode:
      """Subscribes to raw RGBA frames from SHM, runs RF-DETR inference at target_fps."""

      name = "rf-detr-detector"
      shm = True  # Enables SHM-backed Zenoh session for zero-copy receive

      def __init__(self, ctx, config: dict) -> None:
          cfg = load_config(config)
          self._ctx = ctx
          self._topic_key = cfg["topic_key"]
          self._target_fps = cfg["target_fps"]

          self._detector = Detector(
              confidence_threshold=cfg["confidence_threshold"],
              model=cfg["model"],
          )
          self._pub = ctx.publisher_json(f"{self._topic_key}/detections")
          log.info(
              "Publishing detections → %s",
              ctx.topic(f"{self._topic_key}/detections"),
          )

          self._latest_frame: Optional[torch.Tensor] = None
          self._frame_lock = threading.Lock()
          self._seq = 0

      def run(self) -> None:
          ctx = self._ctx
          raw_topic = ctx.topic(f"{self._topic_key}/raw")
          log.info("Subscribing to raw frames ← %s", raw_topic)

          def _on_raw_frame(sample) -> None:
              raw_bytes = bytes(sample.payload)
              msg = RawImage()
              msg.ParseFromString(raw_bytes)
              expected = msg.width * msg.height * 4
              if len(msg.data) != expected:
                  log.warning("RawImage size mismatch: got %d expected %d, dropping", len(msg.data), expected)
                  return
              rgba = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, 4).copy()
              tensor = (
                  torch.from_numpy(rgba[:, :, :3])
                  .permute(2, 0, 1)
                  .to(dtype=torch.float32, device="cuda", non_blocking=True)
                  .div_(255.0)
              )
              with self._frame_lock:
                  self._latest_frame = tensor

          def _inference_loop() -> None:
              interval = 1.0 / self._target_fps
              next_run = time.monotonic()
              while not ctx.is_shutdown():
                  now = time.monotonic()
                  if now < next_run:
                      time.sleep(min(0.05, next_run - now))
                      continue

                  with self._frame_lock:
                      frame = self._latest_frame
                      self._latest_frame = None

                  if frame is None:
                      time.sleep(0.05)
                      continue

                  next_run = time.monotonic() + interval
                  t0 = time.monotonic()
                  detections = self._detector.detect(frame)
                  t1 = time.monotonic()

                  payload = build_payload(
                      frame_id=ctx.instance_name,
                      machine_id=ctx.machine_id,
                      scope=ctx.scope,
                      sequence=self._seq,
                      detections=detections,
                  )
                  self._pub.put(payload)
                  self._seq += 1
                  log.info(
                      "seq=%d detections=%d infer=%.1fms",
                      self._seq,
                      len(detections),
                      (t1 - t0) * 1000,
                  )

          inference_thread = threading.Thread(target=_inference_loop, daemon=True, name="inference")
          inference_thread.start()

          sub = ctx.session.declare_subscriber(raw_topic, _on_raw_frame)  # noqa: F841
          log.info("Ready — waiting for raw frames at 1/%g Hz inference", self._target_fps)
          ctx.wait_shutdown()

          inference_thread.join(timeout=5.0)
          log.info("Detector shutdown complete (seq=%d)", self._seq)


  if __name__ == "__main__":
      run_node(RfDetrDetectorNode)
  ```

- [ ] **Step 4: Run tests**
  ```bash
  cd ~/bubbaloop-nodes-official/rfdetr-detector
  .pixi/envs/default/bin/python -m pytest tests/ -v 2>&1 | tail -20
  ```
  Expected: all pass.

- [ ] **Step 5: Commit**
  ```bash
  cd ~/bubbaloop-nodes-official
  git add rfdetr-detector/main.py rfdetr-detector/configs/tapo_terrace.yaml
  git commit -m "feat(rfdetr-detector): subscribe to SHM raw frames, delete H264DecoderCUDA"
  ```

---

## Task 8: Update daemon registry + live test

**Files:** `~/.bubbaloop/nodes.json`, `~/.config/systemd/user/` service files

- [ ] **Step 1: Update daemon registry**

  Edit `~/.bubbaloop/nodes.json` — change the camera entries' `name_override`:
  ```json
  { "name_override": "tapo-terrace" }  →  { "name_override": "tapo-terrace-camera" }
  { "name_override": "tapo-entrance" }  →  { "name_override": "tapo-entrance-camera" }
  ```
  Also update `config_override` paths if they contain `rf-detr-detector` to `rfdetr-detector` (already done).

- [ ] **Step 2: Rename camera service files**
  ```bash
  # Stop old services
  systemctl --user stop bubbaloop-tapo-terrace.service bubbaloop-tapo-entrance.service 2>/dev/null || true

  # Create new service files with updated Description
  # (copy old files, change Description line only — ExecStart + WorkingDirectory unchanged)
  cp ~/.config/systemd/user/bubbaloop-tapo-terrace.service \
     ~/.config/systemd/user/bubbaloop-tapo-terrace-camera.service
  sed -i 's/Description=Bubbaloop Node: tapo-terrace$/Description=Bubbaloop Node: tapo-terrace-camera/' \
     ~/.config/systemd/user/bubbaloop-tapo-terrace-camera.service

  cp ~/.config/systemd/user/bubbaloop-tapo-entrance.service \
     ~/.config/systemd/user/bubbaloop-tapo-entrance-camera.service
  sed -i 's/Description=Bubbaloop Node: tapo-entrance$/Description=Bubbaloop Node: tapo-entrance-camera/' \
     ~/.config/systemd/user/bubbaloop-tapo-entrance-camera.service

  # Enable new, disable old
  systemctl --user daemon-reload
  systemctl --user enable bubbaloop-tapo-terrace-camera.service bubbaloop-tapo-entrance-camera.service
  systemctl --user disable bubbaloop-tapo-terrace.service bubbaloop-tapo-entrance.service
  ```

- [ ] **Step 3: Build updated camera node binary**
  ```bash
  cd ~/bubbaloop-nodes-official/rtsp-camera
  cargo build --release 2>&1 | tail -5
  ```
  Expected: `Compiling rtsp_camera ... Finished release`

- [ ] **Step 4: Restart all services and daemon**
  ```bash
  systemctl --user restart bubbaloop-tapo-terrace-camera.service
  systemctl --user restart bubbaloop-tapo-entrance-camera.service
  systemctl --user restart bubbaloop-tapo-terrace-detector.service
  systemctl --user restart bubbaloop-daemon.service
  sleep 5
  ```

- [ ] **Step 5: Verify node list**
  ```bash
  ~/bubbaloop/target/release/bubbaloop node list
  ```
  Expected output includes `tapo-terrace-camera`, `tapo-entrance-camera`, `tapo-terrace-detector`.

- [ ] **Step 6: Verify SHM segments exist**
  ```bash
  ls -la /dev/shm/ | grep zenoh
  ```
  Expected: zenoh SHM segments present.

- [ ] **Step 7: Verify detector output at 1Hz**
  ```bash
  systemctl --user status bubbaloop-tapo-terrace-detector.service --no-pager | grep "seq=" | tail -5
  ```
  Expected: one log line per second, no H264 decoder log lines.

- [ ] **Step 8: Final commit + push**
  ```bash
  cd ~/bubbaloop-nodes-official
  git add .
  git commit -m "chore: update daemon registry and service files for _camera naming convention"
  git push origin feat/rf-detr-detector
  ```
