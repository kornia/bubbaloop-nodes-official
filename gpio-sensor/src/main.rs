//! gpio-sensor node — GPIO/edge device sensor with simulation and hardware support
//!
//! Reads digital/analog/temperature/motion pins and publishes JSON readings over Zenoh.
//! Runs in simulation mode by default (no hardware required).
//!
//! Run: ./target/release/gpio_sensor_node -c configs/temperature.yaml

use argh::FromArgs;
use gpio_sensor::{config::Config, gpio_sensor_node::GpioSensorNode};
use std::path::PathBuf;
use std::sync::Arc;

/// GPIO/edge device sensor node
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
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args: Args = argh::from_env();

    let config = Config::from_file(&args.config)
        .map_err(|e| anyhow::anyhow!("Failed to load config from '{}': {}", args.config.display(), e))?;

    log::info!(
        "Loaded config: sensor='{}' topic='{}' pin={} type={:?} interval={}s simulation={}",
        config.name,
        config.publish_topic,
        config.pin,
        config.sensor_type,
        config.interval_secs,
        config.simulation,
    );

    // Create shutdown channel
    let shutdown_tx = tokio::sync::watch::Sender::new(());

    {
        let shutdown_tx = shutdown_tx.clone();
        ctrlc::set_handler(move || {
            log::info!("Shutdown signal received");
            let _ = shutdown_tx.send(());
        })?;
    }

    // Initialize Zenoh session in client mode — MUST be client to route through zenohd
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

    // Schema queryable — reply with empty bytes (no protobuf schema for JSON output)
    // NOTE: Do NOT use .complete(true) — blocks wildcard queries like bubbaloop/**/schema
    let schema_key = format!("bubbaloop/{}/{}/gpio-sensor/schema", scope, machine_id);
    let _schema_queryable = zenoh_session
        .declare_queryable(&schema_key)
        .callback(move |query| {
            let _ = query.reply(&query.key_expr().clone(), &[] as &[u8]);
        })
        .await
        .map_err(|e| anyhow::anyhow!("Failed to create schema queryable: {}", e))?;
    log::info!("Schema queryable: {}", schema_key);

    let node = GpioSensorNode::new(zenoh_session, config, machine_id)
        .map_err(|e| anyhow::anyhow!("Failed to create GPIO sensor node: {}", e))?;
    log::info!("gpio-sensor node started");

    node.run(shutdown_tx, scope)
        .await
        .map_err(|e| anyhow::anyhow!("GPIO sensor node failed: {}", e))?;

    log::info!("gpio-sensor node stopped");
    Ok(())
}
