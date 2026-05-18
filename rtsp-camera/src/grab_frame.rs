//! `grab_frame` queryable — serves JPEG snapshots on demand to the agent.
//!
//! The main capture loop updates a shared frame cache on every RGBA frame.
//! This module spawns a background task that responds to Zenoh queries on
//! `bubbaloop/local/{machine_id}/{node_name}/grab_frame` with a JSON payload
//! containing base64-encoded JPEG + capture metadata.

use crate::cbor_wire::HeaderCbor;
use base64::Engine as _;
use jpeg_encoder::{ColorType, Encoder as JpegEncoder};
use kornia_image::{Image, ImageSize, allocator::CpuAllocator};
use kornia_imgproc::{interpolation::InterpolationMode, resize::resize_fast_rgb};
use std::sync::{Arc, Mutex};
use bubbaloop_node::zenoh;
use tokio::sync::watch;
use zenoh::Session;

/// Cached latest RGBA frame from the capture loop.
pub struct CachedFrame {
    pub width: u32,
    pub height: u32,
    /// Row-major RGBA bytes (width * height * 4)
    pub data: Vec<u8>,
    pub header: HeaderCbor,
}

pub type FrameCache = Arc<Mutex<Option<CachedFrame>>>;

/// Spawn the grab_frame queryable background task.
///
/// The queryable listens on `key_expr`, encodes the latest cached frame as JPEG
/// using kornia-rs, and replies with a JSON payload on each incoming query.
pub async fn spawn_grab_frame_queryable(
    session: Arc<Session>,
    key_expr: String,
    frame_cache: FrameCache,
    mut shutdown_rx: watch::Receiver<()>,
) -> anyhow::Result<tokio::task::JoinHandle<()>> {
    let queryable = session
        .declare_queryable(key_expr.as_str())
        .await
        .map_err(|e| anyhow::anyhow!("grab_frame queryable declare failed: {}", e))?;

    log::info!("grab_frame queryable: {}", key_expr);

    let handle = tokio::spawn(async move {
        loop {
            tokio::select! {
                biased;
                _ = shutdown_rx.changed() => {
                    log::debug!("grab_frame queryable stopping");
                    break;
                }
                result = queryable.recv_async() => {
                    match result {
                        Ok(query) => {
                            let payload = match encode_reply(&frame_cache) {
                                Ok(json) => json,
                                Err(e) => {
                                    log::warn!("grab_frame encode failed: {}", e);
                                    format!("{{\"error\":\"{}\"}}", e)
                                }
                            };
                            let key = query.key_expr().clone();
                            if let Err(e) = query.reply(key, payload).await {
                                log::warn!("grab_frame reply failed: {}", e);
                            }
                        }
                        Err(_) => break,
                    }
                }
            }
        }
    });

    Ok(handle)
}

/// Encode the latest cached frame as JPEG and return a JSON string.
fn encode_reply(cache: &FrameCache) -> anyhow::Result<String> {
    let guard = cache.lock().map_err(|_| anyhow::anyhow!("frame cache poisoned"))?;
    let frame = guard
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("no frame captured yet"))?;

    let w = frame.width as usize;
    let h = frame.height as usize;

    // RGBA8 → RGB (drop alpha — kornia-imgproc only handles RGB for resize)
    let mut rgb = Vec::with_capacity(w * h * 3);
    for chunk in frame.data.chunks_exact(4) {
        rgb.extend_from_slice(&chunk[..3]);
    }
    drop(guard); // release lock before heavy processing

    let src: Image<u8, 3, CpuAllocator> = Image::new(
        ImageSize { width: w, height: h },
        rgb,
        CpuAllocator,
    )
    .map_err(|e| anyhow::anyhow!("kornia image create: {}", e))?;

    // Resize to max 1024 long edge (SIMD bilinear via kornia-imgproc)
    let (new_w, new_h) = scale_to_long_edge(w, h, 1024);
    let mut dst: Image<u8, 3, CpuAllocator> =
        Image::from_size_val(ImageSize { width: new_w, height: new_h }, 0, CpuAllocator)
            .map_err(|e| anyhow::anyhow!("kornia dst alloc: {}", e))?;
    resize_fast_rgb(&src, &mut dst, InterpolationMode::Bilinear)
        .map_err(|e| anyhow::anyhow!("kornia resize: {}", e))?;

    // JPEG encode quality 75 (pure-Rust, no C deps)
    let mut jpeg_buf = Vec::with_capacity(256 * 1024);
    JpegEncoder::new(&mut jpeg_buf, 75)
        .encode(dst.as_slice(), new_w as u16, new_h as u16, ColorType::Rgb)
        .map_err(|e| anyhow::anyhow!("JPEG encode: {}", e))?;

    let jpeg_b64 = base64::engine::general_purpose::STANDARD.encode(&jpeg_buf);

    // Re-acquire lock briefly just to read metadata
    let guard = cache.lock().map_err(|_| anyhow::anyhow!("frame cache poisoned"))?;
    let frame = guard
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("frame disappeared"))?;
    let now_ns = now_nanos();
    let acq_iso = ns_to_iso8601(frame.header.acq_time);
    let age_ms = now_ns.saturating_sub(frame.header.acq_time) / 1_000_000;
    let pipeline_latency_us = frame.header.pub_time.saturating_sub(frame.header.acq_time) / 1_000;
    let orig_w = frame.width;
    let orig_h = frame.height;
    let frame_id = frame.header.frame_id.clone();
    let sequence = frame.header.sequence;
    let machine_id = frame.header.machine_id.clone();
    drop(guard);

    let label = format!(
        "Camera '{}' — captured {} ({}ms ago), original {}×{} → downsampled {}×{}:",
        frame_id, acq_iso, age_ms, orig_w, orig_h, new_w, new_h
    );

    let json = serde_json::json!({
        "jpeg_b64": jpeg_b64,
        "media_type": "image/jpeg",
        "label": label,
        "camera": frame_id,
        "machine_id": machine_id,
        "frame_id": frame_id,
        "sequence": sequence,
        "acq_time_iso": acq_iso,
        "age_ms": age_ms,
        "pipeline_latency_us": pipeline_latency_us,
        "original_width": orig_w,
        "original_height": orig_h,
        "downsampled_width": new_w,
        "downsampled_height": new_h,
        "jpeg_bytes": jpeg_buf.len(),
    });

    Ok(json.to_string())
}

fn scale_to_long_edge(w: usize, h: usize, max_edge: usize) -> (usize, usize) {
    if w <= max_edge && h <= max_edge {
        return (w, h);
    }
    if w >= h {
        (max_edge, (h * max_edge / w).max(1))
    } else {
        ((w * max_edge / h).max(1), max_edge)
    }
}

fn now_nanos() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos() as u64
}

fn ns_to_iso8601(ns: u64) -> String {
    let secs = ns / 1_000_000_000;
    let ms = ((ns % 1_000_000_000) / 1_000_000) as u32;
    let sec = secs % 60;
    let min = (secs / 60) % 60;
    let hr = (secs / 3600) % 24;
    let days = secs / 86400;
    let (year, month, day) = days_to_ymd(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        year, month, day, hr, min, sec, ms
    )
}

fn days_to_ymd(days: u64) -> (u64, u64, u64) {
    let z = days + 719468;
    let era = z / 146097;
    let doe = z % 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}
