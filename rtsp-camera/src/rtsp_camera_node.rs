use crate::cbor_wire::{CompressedImageCborRef, HeaderCbor, RawImageCborRef};
use crate::config::Config;
use crate::h264_capture::H264StreamCapture;
use bubbaloop_node::{CborPublisher, CborPublisherShm, NodeContext};
use std::sync::Arc;

fn make_header(ctx: &NodeContext, camera_name: &str, acq_time: u64, sequence: u32) -> HeaderCbor {
    let pub_time = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    HeaderCbor {
        acq_time,
        pub_time,
        sequence,
        frame_id: camera_name.to_owned(),
        machine_id: ctx.machine_id.clone(),
    }
}

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

pub struct RtspCameraNode {
    config: Config,
}

#[bubbaloop_node::async_trait::async_trait]
impl bubbaloop_node::Node for RtspCameraNode {
    type Config = Config;

    fn name() -> &'static str {
        "rtsp-camera"
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
        let compressed_suffix = "compressed";
        let raw_suffix = "raw";
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

        let compressed_pub: CborPublisher =
            ctx.publisher_cbor(compressed_suffix).await?;

        let raw_slot_size = frame_size + RawImageCborRef::HEADER_OVERHEAD_BYTES;
        let raw_pub: CborPublisherShm =
            ctx.publisher_cbor_shm(raw_suffix, 4, raw_slot_size).await?;

        log::info!(
            "[{}] compressed (CBOR) → {} | raw (CBOR SHM {}×{}, slot={}B) → {}",
            camera_name,
            ctx.topic(compressed_suffix),
            raw_width,
            raw_height,
            raw_slot_size,
            ctx.local_topic(raw_suffix),
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
        let mut total_encode_ns: u64 = 0;
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

                            let header = make_header(&ctx, &camera_name, h264_frame.pts, h264_frame.sequence);
                            let cbor_msg = CompressedImageCborRef {
                                header: &header,
                                format: "h264",
                                data: h264_frame.as_slice(),
                            };

                            let enc_t0 = std::time::Instant::now();
                            if let Err(e) = compressed_pub.put(&cbor_msg).await {
                                if published == 0 {
                                    log::warn!("[{}] CBOR put failed: {}", camera_name, e);
                                }
                            } else {
                                total_encode_ns += enc_t0.elapsed().as_nanos() as u64;
                                published += 1;
                            }

                            let elapsed = last_log.elapsed();
                            if elapsed.as_secs() >= 1 {
                                let frames_this_period = published - last_published_count;
                                let fps = frames_this_period as f64 / elapsed.as_secs_f64();
                                let enc_us_avg = if published > 0 {
                                    (total_encode_ns / published) / 1000
                                } else {
                                    0
                                };
                                log::info!(
                                    "[{}] seq={}, published={} raw={} fps={:.1} dropped={} \
                                     cbor_enc≈{}µs",
                                    camera_name, sequence, published,
                                    raw_published, fps, dropped, enc_us_avg
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
                            let header = make_header(&ctx, &camera_name, rgba_frame.pts, rgba_frame.sequence);
                            let raw_msg = RawImageCborRef {
                                header: &header,
                                width: raw_width,
                                height: raw_height,
                                encoding: "rgba8",
                                step: raw_width * 4,
                                data: &rgba_frame.data,
                            };
                            if let Err(e) = raw_pub.put(&raw_msg).await {
                                if raw_published == 0 {
                                    log::warn!("[{}] Raw CBOR SHM put failed: {}", camera_name, e);
                                }
                            } else {
                                raw_published += 1;
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
