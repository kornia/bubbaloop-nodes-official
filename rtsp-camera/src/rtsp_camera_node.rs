use crate::config::Config;
use crate::h264_capture::{H264Frame, H264StreamCapture};
use crate::proto::CompressedImage;
use bubbaloop_node::publisher::ProtoPublisher;
use bubbaloop_node::schemas::Header;
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

/// RTSP Camera node -- captures a single H264 stream and publishes compressed frames.
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
        let publish_topic = self.config.publish_topic.clone();

        let url = std::env::var("RTSP_URL").unwrap_or_else(|_| self.config.url.clone());
        let capture = Arc::new(H264StreamCapture::new(&url, self.config.latency)?);
        capture.start()?;

        log::info!(
            "Camera '{}' capturing from RTSP (latency={}ms)",
            camera_name,
            self.config.latency
        );

        let compressed_pub: ProtoPublisher<CompressedImage> =
            ctx.publisher_proto(&publish_topic).await?;

        log::info!("[{}] Publishing to: {}", camera_name, ctx.topic(&publish_topic));

        // Keyframes are always published to avoid breaking the H264 decode chain.
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

                result = capture.receiver().recv_async() => {
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

                            // Log NAL types for early frames to verify stream health
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

                            // Periodic FPS stats
                            let elapsed = last_log.elapsed();
                            if elapsed.as_secs() >= 1 {
                                let frames_this_period = published - last_published_count;
                                let fps = frames_this_period as f64 / elapsed.as_secs_f64();
                                log::info!(
                                    "[{}] seq={}, total={}, fps={:.1}, dropped={}",
                                    camera_name, sequence, published, fps, dropped
                                );
                                last_published_count = published;
                                last_log = std::time::Instant::now();
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

        log::info!("Camera '{}' shutdown complete (published: {})", camera_name, published);
        Ok(())
    }
}
