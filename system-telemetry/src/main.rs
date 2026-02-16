//! system-telemetry node - System telemetry metrics

use anyhow::Result;
use argh::FromArgs;
use std::path::PathBuf;
use std::sync::Arc;

mod node;
mod proto;

use node::SystemTelemetryNode;

/// FileDescriptorSet for this node's protobuf schemas
pub const DESCRIPTOR: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"));

/// System telemetry metrics (CPU, memory, disk, network, load)
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
async fn main() -> Result<()> {
    // Initialize logging
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args: Args = argh::from_env();

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
    let mut config = zenoh::Config::default();
    config.insert_json5("mode", r#""client""#).unwrap();
    config
        .insert_json5("connect/endpoints", &format!(r#"["{}"]"#, endpoint))
        .unwrap();
    let session = Arc::new(
        zenoh::open(config)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to open Zenoh session: {}", e))?,
    );

    // Create and run the node
    let node = SystemTelemetryNode::new(session, &args.config).await?;

    log::info!("system-telemetry node started");

    node.run(shutdown_tx).await?;

    log::info!("system-telemetry node stopped");
    Ok(())
}
