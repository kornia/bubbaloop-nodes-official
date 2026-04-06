use crate::config::Config;
use crate::h264_capture::{H264Frame, H264StreamCapture};
use crate::proto::CompressedImage;
use bubbaloop_node::publisher::ProtoPublisher;
use bubbaloop_node::schemas::Header;
use bubbaloop_node::zenoh::bytes::ZBytes;
use bubbaloop_node::zenoh::shm::{
    BlockOn, GarbageCollect, PosixShmProviderBackend, ShmProviderBuilder,
};
use bubbaloop_node::zenoh::Wait;
use bubbaloop_node::ShmPublisher;
use std::sync::Arc;

/// Extract NAL unit types from an H264 byte-stream (Annex B format).
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

fn frame_to_compressed_image(
    frame: H264Frame,
    camera_name: &str,
    machine_id: &str,
    scope: &str,
) -> CompressedImage {
    CompressedImage {
        header: Some(Header {
            acq_time: frame.pts,
            pub_time: get_pub_time(),
            sequence: frame.sequence,
            frame_id: camera_name.to_string(),
            machine_id: machine_id.to_string(),
            scope: scope.to_string(),
        }),
        format: "h264".to_string(),
        data: frame.as_slice().into(),
    }
}

/// RTSP Camera node — captures a single H264 RTSP stream and publishes:
///   - `{key}/compressed` : H264 byte-stream (Annex-B, protobuf)
///   - `{key}/raw`        : RGBA raw bytes resized to `raw_width × raw_height`,
///                          published directly into a Zenoh SHM buffer (no protobuf).
///                          Payload = `raw_width * raw_height * 4` raw RGBA bytes.
pub struct RtspCameraNode {
    config: Config,
}

#[bubbaloop_node::async_trait::async_trait]
impl bubbaloop_node::Node for RtspCameraNode {
    type Config = Config;

    fn name() -> &'static str {
        "rtsp-camera"
    }

    fn descriptor() -> &'static [u8] {
        include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"))
    }

    /// SHM transport required — raw RGBA frames are published zero-copy.
    fn shm() -> bool {
        true
    }

    async fn init(
        _ctx: &bubbaloop_node::NodeContext,
        config: &Config,
    ) -> anyhow::Result<Self> {
        Ok(Self {
            config: config.clone(),
        })
    }

    async fn run(self, ctx: bubbaloop_node::NodeContext) -> anyhow::Result<()> {
        let camera_name = self.config.name.clone();
        let key = self.config.topic_key().to_string();
        let compressed_suffix = format!("{key}/compressed");
        let raw_suffix = format!("{key}/raw");
        let raw_width = self.config.raw_width;
        let raw_height = self.config.raw_height;
        let frame_size = (raw_width * raw_height * 4) as usize;

        let url = std::env::var("RTSP_URL").unwrap_or_else(|_| self.config.url.clone());
        let capture = Arc::new(H264StreamCapture::new(
            &url,
            self.config.latency,
            raw_width,
            raw_height,
        )?);
        capture.start()?;

        log::info!(
            "Camera '{}' capturing from RTSP (latency={}ms, raw={}x{})",
            camera_name,
            self.config.latency,
            raw_width,
            raw_height,
        );

        // Compressed topic: protobuf publisher (existing path, unchanged)
        let compressed_pub: ProtoPublisher<CompressedImage> =
            ctx.publisher_proto(&compressed_suffix).await?;

        // Raw topic: raw ZBytes publisher — payload is Zenoh SHM buffer, no protobuf wrapper.
        let raw_pub: ShmPublisher = ctx.publisher_shm(&raw_suffix).await?;

        // SHM pool: 4 × frame_size gives enough room for BlockOn<GarbageCollect>
        // to reclaim buffers the subscriber has already consumed.
        let shm_pool_size = frame_size * 4;
        let shm_backend = PosixShmProviderBackend::builder(shm_pool_size)
            .wait()
            .map_err(|e| bubbaloop_node::NodeError::Decode(format!("SHM backend: {e:?}")))?;
        let shm_provider = ShmProviderBuilder::backend(shm_backend).wait();
        let shm_layout = shm_provider
            .alloc_layout(frame_size)
            .map_err(|e| bubbaloop_node::NodeError::Decode(format!("SHM layout: {e:?}")))?;

        log::info!(
            "[{}] compressed → {} | raw (SHM {}×{}) → {}",
            camera_name,
            ctx.topic(&compressed_suffix),
            raw_width,
            raw_height,
            ctx.topic(&raw_suffix),
        );

        let frame_interval = self.config.frame_rate.map(|fps| {
            std::time::Duration::from_secs_f64(1.0 / fps as f64)
        });

        if let Some(interval) = frame_interval {
            log::info!(
                "[{}] Frame rate limit: {} fps (interval: {:.1}ms)",
                camera_name,
                self.config.frame_rate.unwrap(),
                interval.as_secs_f64() * 1000.0
            );
        }

        let mut shutdown_rx = ctx.shutdown_rx.clone();
        let mut published: u64 = 0;
        let mut raw_published: u64 = 0;
        let mut dropped: u64 = 0;
        let mut last_log = std::time::Instant::now();
        let mut last_published_count: u64 = 0;
        let mut next_frame_time = std::time::Instant::now();

        loop {
            tokio::select! {
                biased;

                _ = shutdown_rx.changed() => {
                    log::info!("[{}] Shutdown signal received", camera_name);
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
                                next_frame_time = std::cmp::max(
                                    next_frame_time + interval,
                                    now,
                                );
                            }

                            let sequence = h264_frame.sequence;

                            if published < 10 {
                                let nal_types = extract_nal_types(h264_frame.as_slice());
                                log::info!(
                                    "[{}] pub={} seq={} size={} keyframe={} NALs={:?}",
                                    camera_name, published, sequence,
                                    h264_frame.len(), h264_frame.keyframe, nal_types
                                );
                            }
                            let msg = frame_to_compressed_image(
                                h264_frame,
                                &camera_name,
                                &ctx.machine_id,
                                &ctx.scope,
                            );
                            if compressed_pub.put(&msg).await.is_ok() {
                                published += 1;
                            }

                            let elapsed = last_log.elapsed();
                            if elapsed.as_secs() >= 1 {
                                let frames_this_period = published - last_published_count;
                                let fps = frames_this_period as f64 / elapsed.as_secs_f64();
                                log::info!(
                                    "[{}] seq={}, compressed={}, raw={}, fps={:.1}, dropped={}",
                                    camera_name, sequence, published, raw_published, fps, dropped
                                );
                                last_published_count = published;
                                last_log = std::time::Instant::now();
                            }
                        }
                        Err(_) => break,
                    }
                }

                result = capture.rgba_receiver().recv_async() => {
                    match result {
                        Ok(rgba_frame) => {
                            // Alloc SHM buffer, copy RGBA bytes, publish zero-copy to subscriber.
                            match shm_layout.alloc().with_policy::<BlockOn<GarbageCollect>>().await {
                                Ok(mut sbuf) => {
                                    sbuf[..frame_size].copy_from_slice(&rgba_frame.data);
                                    if raw_pub.put(ZBytes::from(sbuf)).await.is_ok() {
                                        raw_published += 1;
                                    }
                                }
                                Err(e) => {
                                    log::warn!("[{}] SHM alloc failed: {:?}", camera_name, e);
                                }
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

        log::info!(
            "Camera '{}' shutdown complete (compressed={}, raw={})",
            camera_name, published, raw_published
        );
        Ok(())
    }
}
