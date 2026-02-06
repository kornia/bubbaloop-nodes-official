//! system-telemetry node implementation

use anyhow::{Context, Result};
use ros_z::context::ZContext;
use ros_z::msg::ProtobufSerdes;
use ros_z::pubsub::ZPub;
use ros_z::Builder;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::sync::Arc;
use sysinfo::{Disks, Networks, System};

use bubbaloop_schemas::{
    CpuMetrics, DiskMetrics, LoadMetrics, MemoryMetrics, NetworkMetrics, SystemMetrics,
};

/// Which metrics to collect
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CollectConfig {
    #[serde(default = "default_true")]
    pub cpu: bool,
    #[serde(default = "default_true")]
    pub memory: bool,
    #[serde(default = "default_true")]
    pub disk: bool,
    #[serde(default = "default_true")]
    pub network: bool,
    #[serde(default = "default_true")]
    pub load: bool,
}

fn default_true() -> bool {
    true
}

impl Default for CollectConfig {
    fn default() -> Self {
        Self {
            cpu: true,
            memory: true,
            disk: true,
            network: true,
            load: true,
        }
    }
}

/// Node configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub publish_topic: String,
    pub rate_hz: f64,
    #[serde(default)]
    pub collect: CollectConfig,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            publish_topic: "system-telemetry/metrics".to_string(),
            rate_hz: 1.0,
            collect: CollectConfig::default(),
        }
    }
}

/// SystemTelemetry node
pub struct SystemTelemetryNode {
    config: Config,
    ctx: Arc<ZContext>,
    zenoh_session: zenoh::Session,
    system: System,
    disks: Disks,
    networks: Networks,
    scope: String,
    machine_id: String,
}

impl SystemTelemetryNode {
    /// Create a new node instance
    pub async fn new(ctx: Arc<ZContext>, config_path: &Path, endpoint: &str) -> Result<Self> {
        // Load configuration
        let config = if config_path.exists() {
            let content =
                std::fs::read_to_string(config_path).context("Failed to read config file")?;
            serde_yaml::from_str(&content).context("Failed to parse config file")?
        } else {
            log::warn!("Config file not found, using defaults");
            Config::default()
        };

        // Create a vanilla zenoh session for the health heartbeat
        let mut zenoh_config = zenoh::Config::default();
        zenoh_config
            .insert_json5("connect/endpoints", &format!(r#"["{}"]"#, endpoint))
            .unwrap();
        let zenoh_session = zenoh::open(zenoh_config)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to open zenoh session: {}", e))?;

        // Validate config
        let topic_re = regex_lite::Regex::new(r"^[a-zA-Z0-9/_\-\.]+$").unwrap();
        if !topic_re.is_match(&config.publish_topic) {
            anyhow::bail!(
                "publish_topic '{}' contains invalid characters (must match [a-zA-Z0-9/_\\-\\.]+)",
                config.publish_topic
            );
        }
        if config.rate_hz < 0.01 || config.rate_hz > 1000.0 {
            anyhow::bail!("rate_hz {} out of range (0.01-1000.0)", config.rate_hz);
        }

        let scope = std::env::var("BUBBALOOP_SCOPE").unwrap_or_else(|_| "local".to_string());
        let machine_id = std::env::var("BUBBALOOP_MACHINE_ID")
            .unwrap_or_else(|_| System::host_name().unwrap_or_else(|| "unknown".to_string()));

        let full_topic = format!("bubbaloop/{}/{}/{}", scope, machine_id, config.publish_topic);
        log::info!("Publishing to: {}", full_topic);

        let mut system = System::new();
        // Initial refresh to populate data
        if config.collect.cpu {
            system.refresh_cpu_all();
        }
        if config.collect.memory {
            system.refresh_memory();
        }

        let disks = Disks::new_with_refreshed_list();
        let networks = Networks::new_with_refreshed_list();

        Ok(Self {
            config,
            ctx,
            zenoh_session,
            system,
            disks,
            networks,
            scope,
            machine_id,
        })
    }

    /// Run the node main loop
    pub async fn run(mut self, shutdown_tx: tokio::sync::watch::Sender<()>) -> Result<()> {
        let mut shutdown_rx = shutdown_tx.subscribe();

        // Build scoped topic: bubbaloop/{scope}/{machine_id}/{publish_topic}
        let full_topic = format!(
            "bubbaloop/{}/{}/{}",
            self.scope, self.machine_id, self.config.publish_topic
        );

        // Create ros-z node and typed publisher
        let node = self
            .ctx
            .create_node("system_telemetry")
            .build()
            .map_err(|e| anyhow::anyhow!("Failed to create ros-z node: {}", e))?;

        let metrics_pub: ZPub<SystemMetrics, ProtobufSerdes<SystemMetrics>> = node
            .create_pub::<SystemMetrics>(&full_topic)
            .with_serdes::<ProtobufSerdes<SystemMetrics>>()
            .build()
            .map_err(|e| anyhow::anyhow!("Failed to create metrics publisher: {}", e))?;

        // Health heartbeat via vanilla zenoh (simple string, not protobuf)
        let health_topic = format!(
            "bubbaloop/{}/{}/health/system-telemetry",
            self.scope, self.machine_id
        );
        let health_publisher = self
            .zenoh_session
            .declare_publisher(&health_topic)
            .await
            .map_err(|e| anyhow::anyhow!("Failed to create health publisher: {}", e))?;

        let interval = std::time::Duration::from_secs_f64(1.0 / self.config.rate_hz);
        let mut sequence: u32 = 0;
        let mut tick = tokio::time::interval(interval);

        // Wait briefly for initial CPU measurement baseline
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;

        log::info!(
            "Publishing metrics to '{}' at {:.1} Hz",
            full_topic,
            self.config.rate_hz
        );

        loop {
            tokio::select! {
                biased;

                _ = shutdown_rx.changed() => {
                    log::info!("Received shutdown signal");
                    break;
                }

                _ = tick.tick() => {
                    let metrics = self.collect_metrics(sequence);

                    if sequence.is_multiple_of(10) {
                        log::debug!(
                            "Published metrics seq={} cpu={:.1}% mem={:.1}%",
                            sequence,
                            metrics.cpu.as_ref().map(|c| c.usage_percent).unwrap_or(0.0),
                            metrics.memory.as_ref().map(|m| m.usage_percent).unwrap_or(0.0),
                        );
                    }

                    if let Err(e) = metrics_pub.async_publish(&metrics).await {
                        log::warn!("Failed to publish metrics: {}", e);
                    }

                    // Publish health heartbeat
                    if let Err(e) = health_publisher.put("ok").await {
                        log::warn!("Failed to publish health: {}", e);
                    }

                    sequence = sequence.wrapping_add(1);
                }
            }
        }

        log::info!("Shutdown complete");
        Ok(())
    }

    /// Collect system metrics
    fn collect_metrics(&mut self, sequence: u32) -> SystemMetrics {
        let now_ns = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0) as u64;

        let cpu = if self.config.collect.cpu {
            self.system.refresh_cpu_all();
            let cpus = self.system.cpus();
            let per_core: Vec<f32> = cpus.iter().map(|c| c.cpu_usage()).collect();
            let total_usage = if per_core.is_empty() {
                0.0
            } else {
                per_core.iter().sum::<f32>() / per_core.len() as f32
            };

            Some(CpuMetrics {
                usage_percent: total_usage,
                count: cpus.len() as u32,
                per_core,
            })
        } else {
            None
        };

        let memory = if self.config.collect.memory {
            self.system.refresh_memory();
            let total = self.system.total_memory();
            let used = self.system.used_memory();
            let available = self.system.available_memory();
            let usage_percent = if total > 0 {
                (used as f64 / total as f64 * 100.0) as f32
            } else {
                0.0
            };

            Some(MemoryMetrics {
                total_bytes: total,
                used_bytes: used,
                available_bytes: available,
                usage_percent,
            })
        } else {
            None
        };

        let disk = if self.config.collect.disk {
            self.disks.refresh(true);
            let mut total: u64 = 0;
            let mut available: u64 = 0;
            for disk in self.disks.list() {
                total += disk.total_space();
                available += disk.available_space();
            }
            let used = total.saturating_sub(available);
            let usage_percent = if total > 0 {
                (used as f64 / total as f64 * 100.0) as f32
            } else {
                0.0
            };

            Some(DiskMetrics {
                total_bytes: total,
                used_bytes: used,
                available_bytes: available,
                usage_percent,
            })
        } else {
            None
        };

        let network = if self.config.collect.network {
            self.networks.refresh(true);
            let mut bytes_sent: u64 = 0;
            let mut bytes_recv: u64 = 0;
            for data in self.networks.list().values() {
                bytes_sent += data.total_transmitted();
                bytes_recv += data.total_received();
            }

            Some(NetworkMetrics {
                bytes_sent,
                bytes_recv,
            })
        } else {
            None
        };

        let load = if self.config.collect.load {
            let load_avg = System::load_average();
            Some(LoadMetrics {
                one_min: load_avg.one as f32,
                five_min: load_avg.five as f32,
                fifteen_min: load_avg.fifteen as f32,
            })
        } else {
            None
        };

        let pub_time = chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0) as u64;

        SystemMetrics {
            header: Some(bubbaloop_schemas::Header {
                acq_time: now_ns,
                pub_time,
                sequence,
                frame_id: "system-telemetry".to_string(),
                machine_id: self.machine_id.clone(),
                scope: self.scope.clone(),
            }),
            cpu,
            memory,
            disk,
            network,
            load,
            uptime_secs: System::uptime(),
        }
    }
}
