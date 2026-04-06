use serde::{Deserialize, Serialize};
use std::path::Path;

/// Hardware acceleration backend for decoding and RGBA conversion.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "lowercase")]
pub enum HwAccel {
    /// NVIDIA hardware decoder: `nvv4l2decoder ! nvvidconv` (Jetson VIC).
    /// Best performance on Jetson — zero extra GPU kernels.
    #[default]
    Nvidia,
    /// Software decoder: `avdec_h264 ! videoconvert ! videoscale`.
    /// Works on any x86/ARM host without NVDEC support.
    Cpu,
}

/// Configuration for a single RTSP camera instance
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// Unique name for this camera instance (must end with `_camera`, e.g. `tapo_entrance_camera`).
    /// The topic key is derived by stripping the `_camera` suffix:
    /// `tapo_entrance_camera` → topics `tapo_entrance/compressed` and `tapo_entrance/raw`.
    pub name: String,
    /// RTSP URL (e.g., rtsp://user:pass@192.168.1.10:554/stream)
    pub url: String,
    /// Latency in milliseconds for the RTSP stream
    #[serde(default = "default_latency")]
    pub latency: u32,
    /// Target publish frame rate (frames per second, 1–120)
    #[serde(default)]
    pub frame_rate: Option<u32>,
    /// Width of raw RGBA frames published over SHM (default: 560)
    #[serde(default = "default_raw_width")]
    pub raw_width: u32,
    /// Height of raw RGBA frames published over SHM (default: 560)
    #[serde(default = "default_raw_height")]
    pub raw_height: u32,
    /// Hardware acceleration backend for decoding and resize (default: nvidia)
    #[serde(default)]
    pub hw_accel: HwAccel,
}

fn default_latency() -> u32 {
    200
}

fn default_raw_width() -> u32 {
    560
}

fn default_raw_height() -> u32 {
    560
}

impl Config {
    /// Load configuration from a YAML file
    pub fn from_file(path: impl AsRef<Path>) -> Result<Self, ConfigError> {
        let contents = std::fs::read_to_string(path.as_ref())
            .map_err(|e| ConfigError::IoError(e.to_string()))?;
        Self::parse(&contents)
    }

    /// Parse configuration from a YAML string
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn parse(yaml: &str) -> Result<Self, ConfigError> {
        let config: Config =
            serde_yaml::from_str(yaml).map_err(|e| ConfigError::ParseError(e.to_string()))?;
        config.validate()?;
        Ok(config)
    }

    /// Derive the topic key by stripping the `_camera` suffix from `name`.
    ///
    /// `tapo_entrance_camera` → `tapo_entrance`
    /// Used as: `{key}/compressed`, `{key}/raw`
    pub fn topic_key(&self) -> &str {
        self.name
            .strip_suffix("_camera")
            .unwrap_or(self.name.as_str())
    }

    /// Validate configuration values
    pub fn validate(&self) -> Result<(), ConfigError> {
        // name: [a-zA-Z0-9_\-\.]+
        if self.name.is_empty()
            || !self
                .name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-' || b == b'.')
        {
            return Err(ConfigError::ValidationError(format!(
                "name '{}' contains invalid characters (must match [a-zA-Z0-9_\\-\\.]+)",
                self.name
            )));
        }

        // Validate URL is non-empty
        if self.url.is_empty() {
            return Err(ConfigError::ValidationError(
                "url must not be empty".to_string(),
            ));
        }

        // Validate numeric bounds
        if self.latency == 0 || self.latency > 10000 {
            return Err(ConfigError::ValidationError(format!(
                "latency {} out of range (1-10000 ms)",
                self.latency
            )));
        }

        if let Some(fps) = self.frame_rate {
            if fps == 0 || fps > 120 {
                return Err(ConfigError::ValidationError(format!(
                    "frame_rate {} out of range (1-120)",
                    fps
                )));
            }
        }

        if self.raw_width == 0 || self.raw_width > 4096 {
            return Err(ConfigError::ValidationError(format!(
                "raw_width {} out of range (1-4096)",
                self.raw_width
            )));
        }

        if self.raw_height == 0 || self.raw_height > 4096 {
            return Err(ConfigError::ValidationError(format!(
                "raw_height {} out of range (1-4096)",
                self.raw_height
            )));
        }

        Ok(())
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
    fn test_parse_config() -> Result<(), ConfigError> {
        let yaml = r#"
name: tapo_entrance_camera
url: "rtsp://192.168.1.10:554/stream"
latency: 200
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.name, "tapo_entrance_camera");
        assert_eq!(config.topic_key(), "tapo_entrance");
        assert_eq!(config.latency, 200);
        Ok(())
    }

    #[test]
    fn test_topic_key_strips_camera_suffix() {
        let config = Config {
            name: "tapo_terrace_camera".to_string(),
            url: "rtsp://x".to_string(),
            latency: 50,
            frame_rate: None,
            raw_width: 560,
            raw_height: 560,
            hw_accel: HwAccel::Nvidia,
        };
        assert_eq!(config.topic_key(), "tapo_terrace");
    }

    #[test]
    fn test_topic_key_no_suffix() {
        let config = Config {
            name: "my_node".to_string(),
            url: "rtsp://x".to_string(),
            latency: 50,
            frame_rate: None,
            raw_width: 560,
            raw_height: 560,
            hw_accel: HwAccel::Cpu,
        };
        assert_eq!(config.topic_key(), "my_node");
    }

    #[test]
    fn test_parse_config_with_frame_rate() -> Result<(), ConfigError> {
        let yaml = r#"
name: tapo_front_camera
url: "rtsp://192.168.1.10:554/stream"
latency: 200
frame_rate: 30
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.frame_rate, Some(30));
        Ok(())
    }

    #[test]
    fn test_validate_invalid_name() {
        let yaml = r#"
name: "bad name with spaces"
url: "rtsp://192.168.1.10:554/stream"
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_empty_url() {
        let yaml = r#"
name: test_camera
url: ""
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_frame_rate_bounds() {
        let yaml = r#"
name: test_camera
url: "rtsp://x"
frame_rate: 200
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_default_latency() -> Result<(), ConfigError> {
        let yaml = r#"
name: test_camera
url: "rtsp://192.168.1.10:554/stream"
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.latency, 200);
        Ok(())
    }

    #[test]
    fn test_hw_accel_default_is_nvidia() -> Result<(), ConfigError> {
        let yaml = r#"
name: test_camera
url: "rtsp://192.168.1.10:554/stream"
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.hw_accel, HwAccel::Nvidia);
        Ok(())
    }

    #[test]
    fn test_hw_accel_cpu() -> Result<(), ConfigError> {
        let yaml = r#"
name: test_camera
url: "rtsp://192.168.1.10:554/stream"
hw_accel: cpu
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.hw_accel, HwAccel::Cpu);
        Ok(())
    }

    #[test]
    fn test_hw_accel_nvidia_explicit() -> Result<(), ConfigError> {
        let yaml = r#"
name: test_camera
url: "rtsp://192.168.1.10:554/stream"
hw_accel: nvidia
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.hw_accel, HwAccel::Nvidia);
        Ok(())
    }

    #[test]
    fn test_raw_dimensions_custom() -> Result<(), ConfigError> {
        let yaml = r#"
name: test_camera
url: "rtsp://192.168.1.10:554/stream"
raw_width: 640
raw_height: 480
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.raw_width, 640);
        assert_eq!(config.raw_height, 480);
        Ok(())
    }

    #[test]
    fn test_raw_dimensions_zero_invalid() {
        let yaml = r#"
name: test_camera
url: "rtsp://x"
raw_width: 0
raw_height: 480
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_backward_compat_ignores_unknown_fields() -> Result<(), ConfigError> {
        // Old configs with publish_topic/decoder/width/height should still parse
        let yaml = r#"
name: test_camera
url: "rtsp://192.168.1.10:554/stream"
publish_topic: camera/test/compressed
decoder: cpu
width: 640
height: 480
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.name, "test_camera");
        Ok(())
    }
}
