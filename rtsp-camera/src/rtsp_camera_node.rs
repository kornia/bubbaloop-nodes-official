use crate::config::Config;
use crate::h264_capture::{H264Frame, H264StreamCapture};
use crate::h264_decode::{DecoderBackend, VideoH264Decoder};
use bubbaloop_schemas::{CompressedImage, Header};
use ros_z::{context::ZContext, msg::ProtobufSerdes, pubsub::ZPub, Builder, Result as ZResult};
use std::sync::Arc;
use tokio::task::JoinSet;

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

/// RTSP Camera node - captures a single H264 stream and publishes compressed frames
pub struct RtspCameraNode {
    ctx: Arc<ZContext>,
    config: Config,
    machine_id: String,
}

impl RtspCameraNode {
    pub fn new(
        ctx: Arc<ZContext>,
        config: Config,
        machine_id: String,
    ) -> ZResult<Self> {
        Ok(Self {
            ctx,
            config,
            machine_id,
        })
    }

    /// Compressed task: feeds decoder, publishes compressed images
    #[allow(clippy::too_many_arguments)]
    async fn compressed_task(
        ctx: Arc<ZContext>,
        capture: Arc<H264StreamCapture>,
        decoder: Arc<VideoH264Decoder>,
        publish_topic: String,
        camera_name: String,
        machine_id: String,
        scope: String,
        shutdown_tx: tokio::sync::watch::Sender<()>,
    ) -> ZResult<()> {
        let mut shutdown_rx = shutdown_tx.subscribe();

        // Create compressed publisher via ros-z
        let node = ctx
            .create_node(format!("camera_{}_compressed", camera_name))
            .build()?;

        let compressed_pub: ZPub<CompressedImage, ProtobufSerdes<CompressedImage>> = node
            .create_pub::<CompressedImage>(&publish_topic)
            .with_serdes::<ProtobufSerdes<CompressedImage>>()
            .build()?;

        log::info!(
            "[{}] Compressed task started -> '{}'",
            camera_name,
            publish_topic
        );

        let mut published: u64 = 0;
        let mut last_log = std::time::Instant::now();
        let mut last_published_count: u64 = 0;

        loop {
            tokio::select! {
                biased;

                _ = shutdown_rx.changed() => {
                    log::info!("[{}] Compressed task received shutdown", camera_name);
                    break;
                }

                result = capture.receiver().recv_async() => {
                    match result {
                        Ok(h264_frame) => {
                            // Feed decoder
                            if let Err(e) = decoder.push(h264_frame.as_slice(), h264_frame.pts, h264_frame.keyframe) {
                                log::warn!("[{}] Decoder push failed: {}", camera_name, e);
                            }

                            // Publish compressed
                            let sequence = h264_frame.sequence;
                            let msg = frame_to_compressed_image(h264_frame, &camera_name, &machine_id, &scope);
                            if compressed_pub.async_publish(&msg).await.is_ok() {
                                published += 1;
                            }

                            // Log stats with FPS every second
                            let elapsed = last_log.elapsed();
                            if elapsed.as_secs() >= 1 {
                                let frames_this_period = published - last_published_count;
                                let fps = frames_this_period as f64 / elapsed.as_secs_f64();
                                log::info!(
                                    "[{}] Compressed: seq={}, total={}, fps={:.1}",
                                    camera_name,
                                    sequence,
                                    published,
                                    fps
                                );
                                last_published_count = published;
                                last_log = std::time::Instant::now();
                            }
                        }
                        Err(_) => break, // Channel closed
                    }
                }
            }
        }

        log::info!(
            "[{}] Compressed task exiting (published: {})",
            camera_name,
            published
        );

        Ok(())
    }

    pub async fn run(
        self,
        shutdown_tx: tokio::sync::watch::Sender<()>,
        zenoh_session: std::sync::Arc<zenoh::Session>,
        scope: String,
        machine_id: String,
    ) -> ZResult<()> {
        let camera_name = self.config.name.clone();
        let publish_topic = self.config.publish_topic.clone();

        // Allow RTSP_URL env var to override config
        let url = std::env::var("RTSP_URL").unwrap_or_else(|_| self.config.url.clone());

        // Create H264 capture
        let capture = Arc::new(H264StreamCapture::new(
            &url,
            self.config.latency,
        )?);

        capture.start()?;

        // Create decoder (shared between tasks via its output channel)
        let decoder_backend: DecoderBackend = self.config.decoder.into();
        let decoder = Arc::new(VideoH264Decoder::new(
            decoder_backend,
            self.config.height,
            self.config.width,
        )?);

        log::info!(
            "Camera '{}' decoder: {:?} {}x{}",
            camera_name,
            decoder_backend,
            self.config.width,
            self.config.height
        );

        // Build scoped topics
        let full_data_topic = format!("bubbaloop/{}/{}/{}", scope, machine_id, publish_topic);
        let health_topic = format!(
            "bubbaloop/{}/{}/health/rtsp-camera-{}",
            scope, machine_id, camera_name
        );

        log::info!("[{}] Data topic: {}", camera_name, full_data_topic);
        log::info!("[{}] Health topic: {}", camera_name, health_topic);

        // Create health heartbeat publisher (vanilla zenoh)
        let health_publisher = zenoh_session
            .declare_publisher(health_topic.clone())
            .await
            .map_err(|e| {
                Box::<dyn std::error::Error + Send + Sync>::from(format!(
                    "Health publisher error: {}",
                    e
                ))
            })?;

        // Spawn tasks with shutdown receivers
        let mut tasks: JoinSet<()> = JoinSet::new();

        tasks.spawn({
            let ctx = self.ctx.clone();
            let camera_name = camera_name.clone();
            let capture = capture.clone();
            let decoder = decoder.clone();
            let machine_id = self.machine_id.clone();
            let scope = scope.clone();
            let shutdown_tx = shutdown_tx.clone();
            async move {
                if let Err(e) = Self::compressed_task(
                    ctx,
                    capture,
                    decoder,
                    full_data_topic,
                    camera_name.clone(),
                    machine_id,
                    scope,
                    shutdown_tx,
                )
                .await
                {
                    log::error!("[{}] Compressed task failed: {}", camera_name, e);
                }
            }
        });

        // Health heartbeat + shutdown loop
        let mut shutdown_rx = shutdown_tx.subscribe();
        let mut health_interval = tokio::time::interval(std::time::Duration::from_secs(5));

        loop {
            tokio::select! {
                biased;

                _ = shutdown_rx.changed() => {
                    log::info!("Shutting down camera '{}'...", camera_name);
                    break;
                }

                _ = health_interval.tick() => {
                    if let Err(e) = health_publisher.put("ok").await {
                        log::warn!("[{}] Failed to publish health heartbeat: {}", camera_name, e);
                    }
                }
            }
        }

        // Tasks will exit via their select! loops when they see shutdown
        while tasks.join_next().await.is_some() {}

        // Cleanup resources
        if let Err(e) = capture.close() {
            log::error!("[{}] Failed to close capture: {}", camera_name, e);
        }
        if let Err(e) = decoder.close() {
            log::error!("[{}] Failed to close decoder: {}", camera_name, e);
        }

        log::info!("Camera '{}' shutdown complete", camera_name);
        Ok(())
    }
}
