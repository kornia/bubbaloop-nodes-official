//! Inference node - subscribes to raw images via SHM and computes statistics
//!
//! This is a validation node to test raw image transfer via Zenoh SHM.
//! It subscribes to `camera/*/raw_shm` topics and computes mean/std for each frame.
//!
//! Based on: https://github.com/eclipse-zenoh/zenoh/blob/main/examples/examples/z_sub_shm.rs

use argh::FromArgs;
use bubbaloop_schemas::RawImage;
use prost::Message;
use serde::Deserialize;
use std::path::{Path, PathBuf};
use zenoh::Wait;

/// Inference node configuration
#[derive(Debug, Clone, Deserialize)]
struct Config {
    /// Topic pattern to subscribe to
    subscribe_topic: String,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            subscribe_topic: "camera/*/raw_shm".to_string(),
        }
    }
}

/// Inference node for camera stream processing (SHM subscriber)
#[derive(FromArgs)]
struct Args {
    /// path to configuration file
    #[argh(option, short = 'c', default = "default_config_path()")]
    config: PathBuf,

    /// zenoh endpoint to connect to
    #[argh(option, short = 'e', default = "default_endpoint()")]
    endpoint: String,
}

fn default_config_path() -> PathBuf {
    PathBuf::from("config.yaml")
}

fn default_endpoint() -> String {
    String::from("tcp/127.0.0.1:7447")
}

fn load_config(path: &Path) -> Config {
    if path.exists() {
        match std::fs::read_to_string(path) {
            Ok(content) => match serde_yaml::from_str(&content) {
                Ok(config) => return config,
                Err(e) => log::warn!("Failed to parse config file: {}, using defaults", e),
            },
            Err(e) => log::warn!("Failed to read config file: {}, using defaults", e),
        }
    } else {
        log::warn!("Config file not found: {:?}, using defaults", path);
    }
    Config::default()
}

/// Compute mean and standard deviation of pixel values
fn compute_image_stats(data: &[u8]) -> (f64, f64) {
    if data.is_empty() {
        return (0.0, 0.0);
    }

    let n = data.len() as f64;
    let sum: f64 = data.iter().map(|&x| x as f64).sum();
    let mean = sum / n;

    let variance: f64 = data.iter().map(|&x| (x as f64 - mean).powi(2)).sum::<f64>() / n;
    let std = variance.sqrt();

    (mean, std)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    zenoh::init_log_from_env_or("error");
    env_logger::init();

    let args: Args = argh::from_env();

    log::info!("Starting inference node (SHM subscriber)...");

    // Load and validate config
    let config = load_config(&args.config);

    let topic_re = regex_lite::Regex::new(r"^[a-zA-Z0-9/_\-\.\*]+$").unwrap();
    if !topic_re.is_match(&config.subscribe_topic) {
        log::error!(
            "subscribe_topic '{}' contains invalid characters",
            config.subscribe_topic
        );
        std::process::exit(1);
    }

    // Read scope/machine env vars for health heartbeat
    let scope = std::env::var("BUBBALOOP_SCOPE").unwrap_or_else(|_| "local".to_string());
    let machine_id = std::env::var("BUBBALOOP_MACHINE_ID").unwrap_or_else(|_| {
        hostname::get()
            .map(|h| h.to_string_lossy().to_string())
            .unwrap_or_else(|_| "unknown".to_string())
    });
    log::info!("Scope: {}, Machine ID: {}", scope, machine_id);

    // Create shutdown channel
    let shutdown_tx = tokio::sync::watch::Sender::new(());

    // Set up Ctrl+C handler
    {
        let shutdown_tx = shutdown_tx.clone();
        ctrlc::set_handler(move || {
            log::info!("Received Ctrl+C, shutting down gracefully...");
            let _ = shutdown_tx.send(());
        })?;
    }

    // Create Zenoh session with SHM enabled
    let endpoint = std::env::var("ZENOH_ENDPOINT").unwrap_or(args.endpoint);
    log::info!("Connecting to Zenoh at: {}", endpoint);

    let mut zenoh_config = zenoh::Config::default();
    zenoh_config.insert_json5("transport/shared_memory/enabled", "true")?;
    zenoh_config.insert_json5("connect/endpoints", &format!(r#"["{}"]"#, endpoint))?;

    let session = zenoh::open(zenoh_config).wait()?;

    // Create health heartbeat publisher
    let health_topic = format!("bubbaloop/{}/{}/health/inference", scope, machine_id);
    let health_publisher = session.declare_publisher(health_topic.clone()).await?;
    log::info!("Health heartbeat topic: {}", health_topic);

    // Subscribe using scoped topic
    let full_topic = format!(
        "bubbaloop/{}/{}/{}",
        scope, machine_id, config.subscribe_topic
    );
    let subscriber = session.declare_subscriber(&full_topic).wait()?;

    log::info!(
        "Inference node subscribed to '{}', waiting for SHM images...",
        full_topic
    );

    let mut frame_count = 0u64;
    let mut total_dropped = 0u64;
    let mut last_log_time = std::time::Instant::now();
    let mut shutdown_rx = shutdown_tx.subscribe();
    let mut health_interval = tokio::time::interval(std::time::Duration::from_secs(5));

    // SHM-agnostic receive loop - handles both SHM and RAW data transparently
    // Uses "latest only" strategy: drain queue and process only the most recent sample
    loop {
        tokio::select! {
            biased;

            _ = shutdown_rx.changed() => {
                log::info!("Inference node received shutdown signal");
                break;
            }

            _ = health_interval.tick() => {
                if let Err(e) = health_publisher.put("ok").await {
                    log::warn!("Failed to publish health heartbeat: {}", e);
                }
            }

            result = subscriber.recv_async() => {
                let mut sample = match result {
                    Ok(s) => s,
                    Err(_) => break,
                };

                // Drain any queued samples, keep only the latest
                let mut batch_dropped = 0u64;
                while let Ok(Some(newer)) = subscriber.try_recv() {
                    sample = newer;
                    batch_dropped += 1;
                }
                total_dropped += batch_dropped;

                frame_count += 1;

                let key_str = sample.key_expr().as_str();

                // Get payload bytes - works transparently for both SHM and RAW
                let payload = sample.payload();
                let bytes = payload.to_bytes();

                // Decode protobuf
                let msg = match RawImage::decode(bytes.as_ref()) {
                    Ok(m) => m,
                    Err(e) => {
                        log::warn!("Failed to decode RawImage proto from '{}': {}", key_str, e);
                        continue;
                    }
                };

                // Compute stats
                let (mean, std) = compute_image_stats(&msg.data);

                // Get header info
                let (seq, frame_id) = msg
                    .header
                    .as_ref()
                    .map(|h| (h.sequence, h.frame_id.as_str()))
                    .unwrap_or((0, "unknown"));

                // Log every frame initially, then throttle to 1/sec
                let elapsed = last_log_time.elapsed();
                if elapsed.as_secs_f32() >= 1.0 || frame_count <= 10 {
                    log::info!(
                        "[{}] Frame {} ({}x{} {}): mean={:.2}, std={:.2}, size={} bytes (skipped {} this batch)",
                        frame_id,
                        seq,
                        msg.width,
                        msg.height,
                        msg.encoding,
                        mean,
                        std,
                        msg.data.len(),
                        batch_dropped
                    );
                    last_log_time = std::time::Instant::now();
                }
            }
        }
    }

    log::info!(
        "Inference node stopped after {} frames ({} total skipped)",
        frame_count,
        total_dropped
    );

    Ok(())
}
