use anyhow::Result;
use argh::FromArgs;
use openmeteo::{config::Config, openmeteo_node::OpenMeteoNode, resolve_location};
use std::sync::Arc;

/// FileDescriptorSet for this node's protobuf schemas
const DESCRIPTOR: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/descriptor.bin"));

#[derive(FromArgs)]
/// Open-Meteo weather data publisher for Zenoh
struct Args {
    /// path to the configuration file (optional, uses defaults with auto-discovery)
    #[argh(option, short = 'c')]
    config: Option<String>,

    /// zenoh router endpoint to connect to
    /// Default: tcp/127.0.0.1:7447 (local zenohd router)
    #[argh(option, short = 'e', default = "String::from(\"tcp/127.0.0.1:7447\")")]
    endpoint: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize logging
    let env = env_logger::Env::default().default_filter_or("info");
    env_logger::init_from_env(env);

    let args: Args = argh::from_env();

    // Load configuration (or use defaults)
    let config = if let Some(config_path) = &args.config {
        match Config::from_file(config_path) {
            Ok(c) => c,
            Err(e) => {
                log::error!("Failed to load config from '{}': {}", config_path, e);
                std::process::exit(1);
            }
        }
    } else {
        log::info!("No config file specified, using defaults with auto-discovery");
        Config::default()
    };

    // Resolve location (auto-discover if needed)
    let resolved_location = match resolve_location(&config.location).await {
        Ok(loc) => loc,
        Err(e) => {
            log::error!("Failed to resolve location: {}", e);
            std::process::exit(1);
        }
    };

    log::info!(
        "Using location: ({:.4}, {:.4}){}",
        resolved_location.latitude,
        resolved_location.longitude,
        resolved_location
            .city
            .as_ref()
            .map(|c| format!(" - {}", c))
            .unwrap_or_default()
    );

    // Create shutdown channel
    let shutdown_tx = tokio::sync::watch::Sender::new(());

    // Set up Ctrl+C handler
    {
        let shutdown_tx = shutdown_tx.clone();
        ctrlc::set_handler(move || {
            log::info!("Received Ctrl+C, shutting down gracefully...");
            let _ = shutdown_tx.send(());
        })
        .expect("Error setting Ctrl+C handler");
    }

    // Read scope/machine env vars for health heartbeat
    let scope = std::env::var("BUBBALOOP_SCOPE").unwrap_or_else(|_| "local".to_string());
    let machine_id = std::env::var("BUBBALOOP_MACHINE_ID").unwrap_or_else(|_| {
        hostname::get()
            .map(|h| h.to_string_lossy().to_string())
            .unwrap_or_else(|_| "unknown".to_string())
    });
    log::info!("Scope: {}, Machine ID: {}", scope, machine_id);

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

    // Declare schema queryable so dashboard/tools can discover this node's protobuf schemas
    let schema_key = format!(
        "bubbaloop/{}/{}/openmeteo/schema",
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

    // Create and run the weather node
    let node = OpenMeteoNode::new(zenoh_session.clone(), resolved_location, config.fetch, machine_id.clone())?;
    node.run(shutdown_tx, zenoh_session, scope, machine_id)
        .await?;

    log::info!("Weather node shut down, exiting");

    Ok(())
}
