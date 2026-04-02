//! rtsp-camera node — single RTSP camera capture, publishes compressed H264.
//!
//! Uses the bubbaloop Node SDK for session, health, schema, and shutdown handling.
//! Each process handles one camera. For multiple cameras, register
//! multiple instances with different names and configs via the daemon.

use rtsp_camera::rtsp_camera_node::RtspCameraNode;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    bubbaloop_node::run_node::<RtspCameraNode>().await
}
