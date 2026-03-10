use serde::{Deserialize, Serialize};
use std::path::Path;

/// GPIO sensor type — determines how pin values are interpreted and simulated
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SensorType {
    /// Digital input: 0.0 (low) or 1.0 (high) — door switches, buttons, reed relays
    #[default]
    Digital,
    /// Analog voltage 0–3.3V via ADC — potentiometers, light sensors (LDR), soil moisture
    Analog,
    /// Temperature in °C — DHT22, DS18B20, or thermistors with ADC
    Temperature,
    /// Motion detection: 0.0 (no motion) or 1.0 (motion detected) — PIR sensors (HC-SR501)
    Motion,
}

impl SensorType {
    pub fn unit(&self) -> &'static str {
        match self {
            SensorType::Digital => "bool",
            SensorType::Analog => "V",
            SensorType::Temperature => "°C",
            SensorType::Motion => "bool",
        }
    }
}

/// Configuration for a single GPIO sensor instance
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// Unique name for this instance (used in topic and health)
    pub name: String,
    /// Zenoh topic suffix for publishing readings
    /// Full topic: bubbaloop/{scope}/{machine}/{publish_topic}
    pub publish_topic: String,
    /// GPIO pin number (BCM numbering on Raspberry Pi, 0–40)
    pub pin: u8,
    /// Type of sensor connected to this pin
    #[serde(default)]
    pub sensor_type: SensorType,
    /// Polling interval in seconds (0.01–3600.0)
    #[serde(default = "default_interval")]
    pub interval_secs: f64,
    /// If true, simulate sensor readings (default: true, no hardware required)
    #[serde(default = "default_simulation")]
    pub simulation: bool,
}

fn default_interval() -> f64 {
    1.0
}

fn default_simulation() -> bool {
    true
}

impl Config {
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let contents = std::fs::read_to_string(path.as_ref())
            .map_err(|e| ConfigError::IoError(e.to_string()))?;
        Self::parse(&contents)
    }

    pub fn parse(yaml: &str) -> Result<Self, ConfigError> {
        let config: Config =
            serde_yaml::from_str(yaml).map_err(|e| ConfigError::ParseError(e.to_string()))?;
        config.validate()?;
        Ok(config)
    }

    pub fn validate(&self) -> Result<(), ConfigError> {
        let name_re = regex_lite::Regex::new(r"^[a-zA-Z0-9_\-\.]+$").unwrap();
        if !name_re.is_match(&self.name) {
            return Err(ConfigError::ValidationError(format!(
                "name '{}' contains invalid characters (must match [a-zA-Z0-9_\\-\\.]+)",
                self.name
            )));
        }

        let topic_re = regex_lite::Regex::new(r"^[a-zA-Z0-9/_\-\.]+$").unwrap();
        if !topic_re.is_match(&self.publish_topic) {
            return Err(ConfigError::ValidationError(format!(
                "publish_topic '{}' contains invalid characters",
                self.publish_topic
            )));
        }

        if self.pin > 40 {
            return Err(ConfigError::ValidationError(format!(
                "pin {} out of range (0–40)",
                self.pin
            )));
        }

        if self.interval_secs < 0.01 || self.interval_secs > 3600.0 {
            return Err(ConfigError::ValidationError(format!(
                "interval_secs {} out of range (0.01–3600.0)",
                self.interval_secs
            )));
        }

        Ok(())
    }
}

#[derive(Debug, thiserror::Error)]
pub enum ConfigError {
    #[error("IO error: {0}")]
    IoError(String),
    #[error("Parse error: {0}")]
    ParseError(String),
    #[error("Validation error: {0}")]
    ValidationError(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_temperature_config() -> Result<(), ConfigError> {
        let yaml = r#"
name: temp_sensor
publish_topic: gpio/temp_sensor/reading
pin: 4
sensor_type: temperature
interval_secs: 2.0
simulation: true
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.name, "temp_sensor");
        assert_eq!(config.pin, 4);
        assert_eq!(config.sensor_type, SensorType::Temperature);
        assert_eq!(config.interval_secs, 2.0);
        assert!(config.simulation);
        Ok(())
    }

    #[test]
    fn test_parse_motion_config() -> Result<(), ConfigError> {
        let yaml = r#"
name: motion_sensor
publish_topic: gpio/motion_sensor/reading
pin: 17
sensor_type: motion
interval_secs: 0.1
simulation: true
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.sensor_type, SensorType::Motion);
        assert_eq!(config.interval_secs, 0.1);
        Ok(())
    }

    #[test]
    fn test_default_simulation_true() -> Result<(), ConfigError> {
        let yaml = r#"
name: test
publish_topic: gpio/test/reading
pin: 5
"#;
        let config = Config::parse(yaml)?;
        assert!(config.simulation);
        assert_eq!(config.interval_secs, 1.0);
        assert_eq!(config.sensor_type, SensorType::Digital);
        Ok(())
    }

    #[test]
    fn test_validate_invalid_name() {
        let yaml = r#"
name: "bad name!"
publish_topic: gpio/test/reading
pin: 5
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_pin_out_of_range() {
        let yaml = r#"
name: test
publish_topic: gpio/test/reading
pin: 99
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_interval_too_small() {
        let yaml = r#"
name: test
publish_topic: gpio/test/reading
pin: 5
interval_secs: 0.001
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_sensor_type_units() {
        assert_eq!(SensorType::Temperature.unit(), "°C");
        assert_eq!(SensorType::Analog.unit(), "V");
        assert_eq!(SensorType::Digital.unit(), "bool");
        assert_eq!(SensorType::Motion.unit(), "bool");
    }
}
