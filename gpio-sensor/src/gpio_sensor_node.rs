use crate::config::{Config, SensorType};
use std::f64::consts::PI;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::watch;
use zenoh::Session;

type BoxError = Box<dyn std::error::Error + Send + Sync>;

pub struct GpioSensorNode {
    session: Arc<Session>,
    config: Config,
    machine_id: String,
}

impl GpioSensorNode {
    pub fn new(session: Arc<Session>, config: Config, machine_id: String) -> Result<Self, BoxError> {
        Ok(Self { session, config, machine_id })
    }

    pub async fn run(
        self,
        shutdown_tx: watch::Sender<()>,
        scope: String,
    ) -> Result<(), BoxError> {
        let mut shutdown_rx = shutdown_tx.subscribe();
        let config = &self.config;

        let full_topic = format!(
            "bubbaloop/{}/{}/{}",
            scope, self.machine_id, config.publish_topic
        );
        let health_topic = format!("{}/health", full_topic);

        let publisher = self.session.declare_publisher(&full_topic).await?;
        let health_pub = self.session.declare_publisher(&health_topic).await?;

        log::info!("gpio-sensor publishing to {}", full_topic);

        if config.simulation {
            log::info!(
                "gpio-sensor SIMULATION MODE — pin={} type={:?}",
                config.pin,
                config.sensor_type
            );
            log::info!("  ┌─ To use real hardware, set simulation: false in config and");
            log::info!("  │  enable the 'rppal' or 'gpio-cdev' Cargo feature.");
            log::info!("  │  See Cargo.toml for commented feature definitions.");
            log::info!("  └─ rppal example (Raspberry Pi):");
            log::info!("       let gpio = rppal::gpio::Gpio::new()?;");
            log::info!("       let pin = gpio.get(config.pin)?.into_input();");
            log::info!("       let value = if pin.is_high() {{ 1.0 }} else {{ 0.0 }};");
        }

        let interval = Duration::from_secs_f64(config.interval_secs);
        let mut ticker = tokio::time::interval(interval);
        let mut health_ticker = tokio::time::interval(Duration::from_secs(5));
        let mut reading_count: u64 = 0;
        let start = std::time::Instant::now();

        loop {
            tokio::select! {
                _ = shutdown_rx.changed() => {
                    log::info!("gpio-sensor shutting down");
                    break;
                }
                _ = ticker.tick() => {
                    let t = start.elapsed().as_secs_f64();
                    let value = simulate_read(&self.config, t);
                    reading_count += 1;

                    let ts = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();

                    let payload = serde_json::json!({
                        "sensor": config.name,
                        "pin": config.pin,
                        "value": value,
                        "sensor_type": format!("{:?}", config.sensor_type).to_lowercase(),
                        "unit": config.sensor_type.unit(),
                        "simulation": config.simulation,
                        "reading_count": reading_count,
                        "timestamp": ts,
                    });

                    let bytes = serde_json::to_vec(&payload)?;
                    publisher.put(bytes).await?;
                }
                _ = health_ticker.tick() => {
                    let ts = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs_f64();

                    let health = serde_json::json!({
                        "status": "healthy",
                        "node": config.name,
                        "reading_count": reading_count,
                        "simulation": config.simulation,
                        "timestamp": ts,
                    });
                    let bytes = serde_json::to_vec(&health)?;
                    health_pub.put(bytes).await?;
                }
            }
        }

        Ok(())
    }
}

/// Simulate GPIO pin readings without real hardware.
///
/// ── Real hardware replacement guide ────────────────────────────────────────
///
/// **Raspberry Pi (rppal crate):**
/// ```rust,ignore
/// // Cargo.toml: rppal = { version = "0.19", optional = true }
/// use rppal::gpio::Gpio;
///
/// let gpio = Gpio::new()?;
/// let pin = gpio.get(config.pin)?.into_input();
///
/// let value = match config.sensor_type {
///     SensorType::Digital | SensorType::Motion => {
///         if pin.is_high() { 1.0 } else { 0.0 }
///     }
///     SensorType::Analog | SensorType::Temperature => {
///         // Analog requires ADC (MCP3008, ADS1115, etc.)
///         // See: https://github.com/golemparts/rppal/tree/master/examples
///         todo!("Read from ADC over SPI/I2C")
///     }
/// };
/// ```
///
/// **Generic Linux chardev (gpio-cdev crate):**
/// ```rust,ignore
/// // Cargo.toml: gpio-cdev = { version = "0.6", optional = true }
/// use gpio_cdev::{Chip, LineRequestFlags};
///
/// let mut chip = Chip::new("/dev/gpiochip0")?;
/// let handle = chip.get_line(config.pin as u32)?.request(
///     LineRequestFlags::INPUT, 0, "gpio-sensor"
/// )?;
/// let value = handle.get_value()? as f64;
/// ```
///
/// **NVIDIA Jetson (Jetson.GPIO Python bridge or tegra-gpio):**
/// For Jetson, use the Tegra GPIO sysfs interface or the Python `Jetson.GPIO`
/// library via a subprocess command node (exec driver). The Jetson GPIO
/// numbering differs from Raspberry Pi — use the 40-pin header labels.
/// ─────────────────────────────────────────────────────────────────────────
fn simulate_read(config: &Config, t: f64) -> f64 {
    match config.sensor_type {
        SensorType::Digital => {
            // Slow toggle: flips state roughly every 30 seconds
            if (t / 30.0).floor() as u64 % 2 == 0 { 0.0 } else { 1.0 }
        }
        SensorType::Analog => {
            // 0–3.3V sine wave, 60s period
            1.65 + 1.65 * (2.0 * PI * t / 60.0).sin()
        }
        SensorType::Temperature => {
            // Room temperature: 20°C ± 3°C, slow drift
            20.0 + 3.0 * (2.0 * PI * t / 120.0).sin()
        }
        SensorType::Motion => {
            // Sporadic motion: fires for ~2s every ~15s
            let phase = t % 15.0;
            if phase < 2.0 { 1.0 } else { 0.0 }
        }
    }
}
