use crate::config::Config;
use crate::h264_capture::{H264Frame, H264StreamCapture};
use crate::proto::{CompressedImage, RawImage};
use bubbaloop_node::publisher::ProtoPublisher;
use bubbaloop_node::schemas::Header;
use bubbaloop_node::zenoh::bytes::ZBytes;
use bubbaloop_node::zenoh::shm::{
    BlockOn, GarbageCollect, OwnedShmBuf, PosixShmProviderBackend, ShmProviderBuilder,
};
use bubbaloop_node::zenoh::Wait;
use bubbaloop_node::RawPublisher;
use prost::Message;
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
) -> CompressedImage {
    CompressedImage {
        header: Some(Header {
            acq_time: frame.pts,
            pub_time: get_pub_time(),
            sequence: frame.sequence,
            frame_id: camera_name.to_string(),
            machine_id: machine_id.to_string(),
            ..Default::default()
        }),
        format: "h264".to_string(),
        data: frame.as_slice().into(),
    }
}

/// RTSP Camera node — captures a single H264 RTSP stream and publishes:
///   - `{key}/compressed`: H264 byte-stream (Annex-B, `CompressedImage` protobuf)
///   - `{key}/raw`: RGBA frames as `RawImage` protobuf over Zenoh SHM.
///     Encoding = "rgba8", step = width * 4. Transport is SHM for low latency.
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
            self.config.hw_accel,
        )?);
        capture.start()?;

        log::info!(
            "Camera '{}' capturing from RTSP (latency={}ms, raw={}x{}, hw_accel={:?})",
            camera_name,
            self.config.latency,
            raw_width,
            raw_height,
            self.config.hw_accel,
        );

        // Compressed topic: protobuf publisher (existing path, unchanged)
        let compressed_pub: ProtoPublisher<CompressedImage> =
            ctx.publisher_proto(&compressed_suffix).await?;

        // SHM + protobuf encoding: subscribers auto-decode RawImage by encoding header.
        // Local topic + CongestionControl::Block for SHM backpressure.
        let raw_pub: RawPublisher = ctx.publisher_raw_proto::<RawImage>(&raw_suffix).await?;

        // SHM pool: each slot holds a serialized RawImage proto.
        // Proto overhead over raw pixels is ~128 bytes (header + field tags/lengths).
        // 4 slots gives BlockOn<GarbageCollect> room to reclaim consumed buffers.
        let proto_overhead = 128usize;
        let shm_slot_size = frame_size + proto_overhead;
        let shm_pool_size = shm_slot_size * 4;
        let shm_backend = PosixShmProviderBackend::builder(shm_pool_size)
            .wait()
            .map_err(|e| bubbaloop_node::NodeError::Decode(format!("SHM backend: {e:?}")))?;
        let shm_provider = ShmProviderBuilder::backend(shm_backend).wait();
        let shm_layout = shm_provider
            .alloc_layout(shm_slot_size)
            .map_err(|e| bubbaloop_node::NodeError::Decode(format!("SHM layout: {e:?}")))?;

        log::info!(
            "[{}] compressed → {} | raw (SHM {}×{}) → {}",
            camera_name,
            ctx.topic(&compressed_suffix),
            raw_width,
            raw_height,
            ctx.local_topic(&raw_suffix),
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
                            // Serialize RawImage proto into SHM buffer and publish.
                            let msg = RawImage {
                                header: Some(Header {
                                    acq_time: rgba_frame.pts,
                                    pub_time: get_pub_time(),
                                    sequence: rgba_frame.sequence,
                                    frame_id: camera_name.clone(),
                                    machine_id: ctx.machine_id.clone(),
                                    ..Default::default()
                                }),
                                width: raw_width,
                                height: raw_height,
                                encoding: "rgba8".to_string(),
                                step: raw_width * 4,
                                data: rgba_frame.data,
                            };
                            let encoded_len = msg.encoded_len();
                            match shm_layout.alloc().with_policy::<BlockOn<GarbageCollect>>().await {
                                Ok(mut sbuf) => {
                                    msg.encode(&mut &mut sbuf[..encoded_len])
                                        .expect("SHM slot large enough");
                                    // Truncate SHM buffer to actual proto size — trailing
                                    // zeros from the oversized slot break protobuf parsing.
                                    if sbuf.try_resize(std::num::NonZeroUsize::new(encoded_len).unwrap()).is_none() {
                                        log::warn!("[{}] SHM try_resize failed, proto may have trailing zeros", camera_name);
                                    }
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
