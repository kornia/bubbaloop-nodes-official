use serde::{Deserialize, Serialize};
use std::path::Path;

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
    /// Frame rate hint (informational, actual rate depends on RTSP source)
    #[serde(default)]
    pub frame_rate: Option<u32>,
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
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn parse(yaml: &str) -> Result<Self, ConfigError> {
        let config: Config =
            serde_yaml::from_str(yaml).map_err(|e| ConfigError::ParseError(e.to_string()))?;
        config.validate()?;
        Ok(config)
    }

    /// Validate configuration values
    pub fn validate(&self) -> Result<(), ConfigError> {
        // Validate name: [a-zA-Z0-9_\-\.]+
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

        // Validate publish_topic: [a-zA-Z0-9/_\-\.]+
        if self.publish_topic.is_empty()
            || !self.publish_topic.bytes().all(|b| {
                b.is_ascii_alphanumeric() || b == b'/' || b == b'_' || b == b'-' || b == b'.'
            })
        {
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
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.name, "entrance");
        assert_eq!(config.publish_topic, "camera/entrance/compressed");
        assert_eq!(config.latency, 200);
        Ok(())
    }

    #[test]
    fn test_parse_config_with_frame_rate() -> Result<(), ConfigError> {
        let yaml = r#"
name: front
publish_topic: camera/front/compressed
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
publish_topic: camera/test/compressed
url: "rtsp://192.168.1.10:554/stream"
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_invalid_topic() {
        let yaml = r#"
name: test
publish_topic: "camera/test/bad topic!"
url: "rtsp://192.168.1.10:554/stream"
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_validate_empty_url() {
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: ""
"#;
        assert!(Config::parse(yaml).is_err());
    }

    #[test]
    fn test_default_latency() -> Result<(), ConfigError> {
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: "rtsp://192.168.1.10:554/stream"
"#;
        let config = Config::parse(yaml)?;
        assert_eq!(config.latency, 200);
        Ok(())
    }

    #[test]
    fn test_backward_compat_ignores_unknown_fields() -> Result<(), ConfigError> {
        // Old configs with decoder/width/height should still parse (serde ignores unknown by default)
        let yaml = r#"
name: test
publish_topic: camera/test/compressed
url: "rtsp://192.168.1.10:554/stream"
decoder: cpu
width: 640
height: 480
"#;
        // serde_yaml ignores unknown fields by default, so this should work
        let config = Config::parse(yaml)?;
        assert_eq!(config.name, "test");
        Ok(())
    }
}
