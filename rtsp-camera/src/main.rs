//! rtsp-camera node - Single RTSP camera capture with H264 decode
//!
//! Each process handles one camera. For multiple cameras, register
//! multiple instances with different names and configs via the daemon.

use argh::FromArgs;
use rtsp_camera::{config::Config, rtsp_camera_node::RtspCameraNode};
use std::path::PathBuf;
use std::sync::Arc;

/// FileDescriptorSet for this node's protobuf schemas
const DESCRIPTOR: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"));

/// RTSP camera capture with hardware H264 decode
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

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Initialize logging
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args: Args = argh::from_env();

    // Load configuration
    let config = Config::from_file(&args.config)
        .map_err(|e| anyhow::anyhow!("Failed to load config from '{}': {}", args.config.display(), e))?;

    log::info!(
        "Loaded config: camera='{}', topic='{}', {}x{}",
        config.name,
        config.publish_topic,
        config.width,
        config.height
    );

    // Create shutdown channel
    let shutdown_tx = tokio::sync::watch::Sender::new(());

    // Set up Ctrl+C handler
    {
        let shutdown_tx = shutdown_tx.clone();
        ctrlc::set_handler(move || {
            log::info!("Shutdown signal received");
            let _ = shutdown_tx.send(());
        })?;
    }

    // Initialize Zenoh session in client mode
    let endpoint = std::env::var("ZENOH_ENDPOINT").unwrap_or(args.endpoint);
    log::info!("Connecting to Zenoh at: {}", endpoint);
    let mut zenoh_config = zenoh::Config::default();
    zenoh_config.insert_json5("mode", r#""client""#).unwrap();
    zenoh_config
        .insert_json5("connect/endpoints", &format!(r#"["{}"]"#, endpoint))
        .unwrap();
    let zenoh_session = Arc::new(
        zenoh::open(zenoh_config)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to open Zenoh session: {}", e))?,
    );

    // Read scope/machine env vars
    let scope = std::env::var("BUBBALOOP_SCOPE").unwrap_or_else(|_| "local".to_string());
    let machine_id = std::env::var("BUBBALOOP_MACHINE_ID").unwrap_or_else(|_| {
        hostname::get()
            .map(|h| h.to_string_lossy().to_string())
            .unwrap_or_else(|_| "unknown".to_string())
    })
    // Sanitize hostname for topic compatibility (hyphens can cause issues)
    .replace('-', "_");
    log::info!("Scope: {}, Machine ID: {}", scope, machine_id);

    // Declare schema queryable so dashboard/tools can discover this node's protobuf schemas
    let schema_key = format!(
        "bubbaloop/{}/{}/rtsp-camera/schema",
        scope, machine_id
    );
    let _schema_queryable = zenoh_session
        .declare_queryable(&schema_key)
        .callback({
            let descriptor = DESCRIPTOR.to_vec();
            move |query| {
                let _ = query.reply(&query.key_expr().clone(), descriptor.as_slice());
            }
        })
        .await
        .map_err(|e| anyhow::anyhow!("Failed to create schema queryable: {}", e))?;
    log::info!("Schema queryable: {}", schema_key);

    // Create and run the node
    let node = RtspCameraNode::new(zenoh_session.clone(), config, machine_id.clone())
        .map_err(|e| anyhow::anyhow!("Failed to create camera node: {}", e))?;

    log::info!("rtsp-camera node started");

    node.run(shutdown_tx, zenoh_session, scope, machine_id)
        .await
        .map_err(|e| anyhow::anyhow!("Camera node failed: {}", e))?;

    log::info!("rtsp-camera node stopped");
    Ok(())
}
