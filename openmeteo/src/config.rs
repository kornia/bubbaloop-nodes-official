use serde::{Deserialize, Serialize};
use std::path::Path;

/// Location configuration for weather data
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LocationConfig {
    /// Latitude coordinate (optional if auto_discover is true)
    pub latitude: Option<f64>,
    /// Longitude coordinate (optional if auto_discover is true)
    pub longitude: Option<f64>,
    /// Timezone (e.g., "America/New_York", "Europe/Berlin")
    /// If not specified, auto-detected by API
    #[serde(default)]
    pub timezone: Option<String>,
    /// Auto-discover location from IP address (default: true)
    #[serde(default = "default_auto_discover")]
    pub auto_discover: bool,
}

fn default_auto_discover() -> bool {
    true
}

impl Default for LocationConfig {
    fn default() -> Self {
        Self {
            latitude: None,
            longitude: None,
            timezone: None,
            auto_discover: true,
        }
    }
}

/// Weather data fetch configuration
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FetchConfig {
    /// Polling interval for current weather in seconds (default: 60)
    #[serde(default = "default_current_interval")]
    pub current_interval_secs: u64,
    /// Polling interval for hourly forecast in seconds (default: 3600)
    #[serde(default = "default_hourly_interval")]
    pub hourly_interval_secs: u64,
    /// Polling interval for daily forecast in seconds (default: 21600)
    #[serde(default = "default_daily_interval")]
    pub daily_interval_secs: u64,
    /// Number of hourly forecast hours (default: 48, max: 384)
    #[serde(default = "default_hourly_hours")]
    pub hourly_forecast_hours: u32,
    /// Number of daily forecast days (default: 7, max: 16)
    #[serde(default = "default_daily_days")]
    pub daily_forecast_days: u32,
}

fn default_current_interval() -> u64 {
    30
}
fn default_hourly_interval() -> u64 {
    1800
}
fn default_daily_interval() -> u64 {
    10800
} // 3 hours
fn default_hourly_hours() -> u32 {
    48
}
fn default_daily_days() -> u32 {
    7
}

impl Default for FetchConfig {
    fn default() -> Self {
        Self {
            current_interval_secs: default_current_interval(),
            hourly_interval_secs: default_hourly_interval(),
            daily_interval_secs: default_daily_interval(),
            hourly_forecast_hours: default_hourly_hours(),
            daily_forecast_days: default_daily_days(),
        }
    }
}

/// Root configuration structure
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Config {
    /// Location configuration (optional, auto-discovers if not specified)
    #[serde(default)]
    pub location: LocationConfig,
    /// Fetch configuration (optional, uses defaults if not specified)
    #[serde(default)]
    pub fetch: FetchConfig,
}

impl FetchConfig {
    /// Validate fetch config bounds
    pub fn validate(&self) -> Result<(), ConfigError> {
        if self.current_interval_secs == 0 || self.current_interval_secs > 86400 {
            return Err(ConfigError::ValidationError(format!(
                "current_interval_secs {} out of range (1-86400)",
                self.current_interval_secs
            )));
        }
        if self.hourly_interval_secs == 0 || self.hourly_interval_secs > 86400 {
            return Err(ConfigError::ValidationError(format!(
                "hourly_interval_secs {} out of range (1-86400)",
                self.hourly_interval_secs
            )));
        }
        if self.daily_interval_secs == 0 || self.daily_interval_secs > 86400 {
            return Err(ConfigError::ValidationError(format!(
                "daily_interval_secs {} out of range (1-86400)",
                self.daily_interval_secs
            )));
        }
        if self.hourly_forecast_hours == 0 || self.hourly_forecast_hours > 384 {
            return Err(ConfigError::ValidationError(format!(
                "hourly_forecast_hours {} out of range (1-384)",
                self.hourly_forecast_hours
            )));
        }
        if self.daily_forecast_days == 0 || self.daily_forecast_days > 16 {
            return Err(ConfigError::ValidationError(format!(
                "daily_forecast_days {} out of range (1-16)",
                self.daily_forecast_days
            )));
        }
        Ok(())
    }
}

impl Config {
    /// Load configuration from a YAML file
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let contents = std::fs::read_to_string(path.as_ref())
            .map_err(|e| ConfigError::IoError(e.to_string()))?;
        Self::parse(&contents)
    }

    /// Parse configuration from a YAML string
    pub fn parse(yaml: &str) -> Result<Self, ConfigError> {
        let config: Config =
            serde_yaml::from_str(yaml).map_err(|e| ConfigError::ParseError(e.to_string()))?;
        config.fetch.validate()?;
        Ok(config)
    }
}

/// Configuration errors
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
    fn test_parse_defaults() {
        let yaml = "";
        let config = Config::parse(yaml).unwrap();
        assert_eq!(config.fetch.current_interval_secs, 30);
        assert_eq!(config.fetch.hourly_interval_secs, 1800);
        assert_eq!(config.fetch.daily_interval_secs, 10800);
        assert_eq!(config.fetch.hourly_forecast_hours, 48);
        assert_eq!(config.fetch.daily_forecast_days, 7);
        assert!(config.location.auto_discover);
    }

    #[test]
    fn test_parse_custom_values() {
        let yaml = r#"
fetch:
  current_interval_secs: 120
  hourly_forecast_hours: 72
  daily_forecast_days: 14
"#;
        let config = Config::parse(yaml).unwrap();
        assert_eq!(config.fetch.current_interval_secs, 120);
        assert_eq!(config.fetch.hourly_forecast_hours, 72);
        assert_eq!(config.fetch.daily_forecast_days, 14);
    }

    #[test]
    fn test_parse_location() {
        let yaml = r#"
location:
  latitude: 48.8566
  longitude: 2.3522
  timezone: "Europe/Paris"
  auto_discover: false
"#;
        let config = Config::parse(yaml).unwrap();
        assert!(!config.location.auto_discover);
        assert_eq!(config.location.latitude, Some(48.8566));
        assert_eq!(config.location.longitude, Some(2.3522));
    }

    #[test]
    fn test_validate_current_interval_zero() {
        let fetch = FetchConfig {
            current_interval_secs: 0,
            ..Default::default()
        };
        assert!(fetch.validate().is_err());
    }

    #[test]
    fn test_validate_current_interval_too_high() {
        let fetch = FetchConfig {
            current_interval_secs: 86401,
            ..Default::default()
        };
        assert!(fetch.validate().is_err());
    }

    #[test]
    fn test_validate_hourly_forecast_too_high() {
        let fetch = FetchConfig {
            hourly_forecast_hours: 385,
            ..Default::default()
        };
        assert!(fetch.validate().is_err());
    }

    #[test]
    fn test_validate_daily_forecast_too_high() {
        let fetch = FetchConfig {
            daily_forecast_days: 17,
            ..Default::default()
        };
        assert!(fetch.validate().is_err());
    }

    #[test]
    fn test_validate_all_bounds_ok() {
        let fetch = FetchConfig {
            current_interval_secs: 1,
            hourly_interval_secs: 86400,
            daily_interval_secs: 1,
            hourly_forecast_hours: 384,
            daily_forecast_days: 16,
        };
        assert!(fetch.validate().is_ok());
    }
}
