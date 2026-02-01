use serde::{Deserialize, Serialize};
use std::path::Path;

/// Decoder backend selection for raw frame decoding
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum DecoderBackend {
    /// Software decoding using avdec_h264 (CPU, always available)
    #[default]
    Cpu,
    /// Hardware decoding using nvh264dec (NVIDIA desktop GPU)
    Nvidia,
    /// Hardware decoding using nvv4l2decoder (NVIDIA Jetson)
    Jetson,
}

impl From<DecoderBackend> for crate::h264_decode::DecoderBackend {
    fn from(config: DecoderBackend) -> Self {
        match config {
            DecoderBackend::Cpu => crate::h264_decode::DecoderBackend::Software,
            DecoderBackend::Nvidia => crate::h264_decode::DecoderBackend::Nvidia,
            DecoderBackend::Jetson => crate::h264_decode::DecoderBackend::Jetson,
        }
    }
}

/// Configuration for a single RTSP camera instance
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    /// Unique name for this camera instance (used in topic names and health)
    pub name: String,
    /// Zenoh topic suffix for publishing compressed frames
    /// Full topic: bubbaloop/{scope}/{machine}/{publish_topic}
    pub publish_topic: String,
    /// RTSP URL (e.g., rtsp://user:pass@192.168.1.10:554/stream)
    /// Can also be set via RTSP_URL environment variable
    pub url: String,
    /// Latency in milliseconds for the RTSP stream
    #[serde(default = "default_latency")]
    pub latency: u32,
    /// Decoder backend to use (cpu, nvidia, jetson)
    #[serde(default)]
    pub decoder: DecoderBackend,
    /// Output width for decoded frames
    pub width: u32,
    /// Output height for decoded frames
    pub height: u32,
}

fn default_latency() -> u32 {
    200
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
        config.validate()?;
        Ok(config)
    }

    /// Validate configuration values
    pub fn validate(&self) -> Result<(), ConfigError> {
        // Validate name
        let name_re = regex_lite::Regex::new(r"^[a-zA-Z0-9_\-\.]+$").unwrap();
        if !name_re.is_match(&self.name) {
            return Err(ConfigError::ValidationError(format!(
                "name '{}' contains invalid characters (must match [a-zA-Z0-9_\\-\\.]+)",
                self.name
            )));
        }

        // Validate publish_topic
        let topic_re = regex_lite::Regex::new(r"^[a-zA-Z0-9/_\-\.]+$").unwrap();
        if !topic_re.is_match(&self.publish_topic) {
            return Err(ConfigError::ValidationError(format!(
                "publish_topic '{}' contains invalid characters (must match [a-zA-Z0-9/_\\-\\.]+)",
                self.publish_topic
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

        if self.width == 0 || self.width > 7680 {
            return Err(ConfigError::ValidationError(format!(
                "width {} out of range (1-7680)",
                self.width
            )));
        }

        if self.height == 0 || self.height > 4320 {
            return Err(ConfigError::ValidationError(format!(
                "height {} out of range (1-4320)",
                self.height
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
name: entrance
publish_topic: camera/entrance/compressed
url: "rtsp://192.168.1.10:554/stream"
latency: 200
decoder: cpu
width: 640
height: 480
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.name, "entrance");
        assert_eq!(config.publish_topic, "camera/entrance/compressed");
        assert_eq!(config.latency, 200);
        assert_eq!(config.width, 640);
        assert_eq!(config.height, 480);
        assert_eq!(config.decoder, DecoderBackend::Cpu);
        Ok(())
    }

    #[test]
    fn test_parse_config_with_nvidia() -> Result<(), ConfigError> {
        let yaml = r#"
name: front
publish_topic: camera/front/compressed
url: "rtsp://192.168.1.10:554/stream"
latency: 200
decoder: nvidia
width: 640
height: 480
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.decoder, DecoderBackend::Nvidia);
        Ok(())
    }

    #[test]
    fn test_parse_config_with_jetson() -> Result<(), ConfigError> {
        let yaml = r#"
name: front
publish_topic: camera/front/compressed
url: "rtsp://192.168.1.10:554/stream"
latency: 200
decoder: jetson
width: 320
height: 240
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.decoder, DecoderBackend::Jetson);
        assert_eq!(config.width, 320);
        assert_eq!(config.height, 240);
        Ok(())
    }

    #[test]
    fn test_validate_invalid_name() {
        let yaml = r#"
name: "bad name with spaces"
publish_topic: camera/test/compressed
url: "rtsp://192.168.1.10:554/stream"
width: 640
height: 480
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_invalid_topic() {
        let yaml = r#"
name: test
publish_topic: "camera/test/bad topic!"
url: "rtsp://192.168.1.10:554/stream"
width: 640
height: 480
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_empty_url() {
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: ""
width: 640
height: 480
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_zero_width() {
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: "rtsp://192.168.1.10:554/stream"
width: 0
height: 480
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_default_latency() -> Result<(), ConfigError> {
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: "rtsp://192.168.1.10:554/stream"
decoder: cpu
width: 640
height: 480
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.latency, 200);
        Ok(())
    }

    #[test]
    fn test_url_env_override() -> Result<(), ConfigError> {
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: "rtsp://placeholder:554/stream"
width: 640
height: 480
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.url, "rtsp://placeholder:554/stream");
        Ok(())
    }
}
