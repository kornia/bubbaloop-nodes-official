use crate::config::HwAccel;
use gstreamer::prelude::*;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum H264CaptureError {
    #[error("GStreamer error: {0}")]
    GStreamer(#[from] gstreamer::glib::Error),

    #[error("GStreamer state change error: {0}")]
    StateChange(#[from] gstreamer::StateChangeError),

    #[error("Element not found: {0}")]
    ElementNotFound(&'static str),

    #[error("Failed to downcast")]
    DowncastError,

    #[error("Buffer error")]
    BufferError,
}

/// H264 frame (zero-copy from GStreamer buffer)
pub struct H264Frame {
    buffer: gstreamer::MappedBuffer<gstreamer::buffer::Readable>,
    pub pts: u64,
    pub keyframe: bool,
    pub sequence: u32,
}

impl H264Frame {
    pub fn as_slice(&self) -> &[u8] {
        self.buffer.as_slice()
    }

    pub fn len(&self) -> usize {
        self.buffer.len()
    }

    pub fn is_empty(&self) -> bool {
        self.buffer.is_empty()
    }
}

/// Raw RGBA frame, already resized to the target inference dimensions by the
/// GStreamer pipeline (nvvidconv hardware scaler on Jetson VIC).
/// Data length is always `raw_width * raw_height * 4` bytes.
pub struct RgbaFrame {
    pub pts: u64,
    pub sequence: u32,
    /// Row-major RGBA bytes, length == raw_width * raw_height * 4
    pub data: Vec<u8>,
}

/// Captures H264 from RTSP with a two-branch GStreamer tee:
///   branch 1 → H264 byte-stream (Annex-B, fast, compressed)
///   branch 2 → RGBA, resized to `raw_width × raw_height` by nvvidconv (Jetson VIC)
pub struct H264StreamCapture {
    pipeline: gstreamer::Pipeline,
    h264_rx: flume::Receiver<H264Frame>,
    rgba_rx: flume::Receiver<RgbaFrame>,
}

impl H264StreamCapture {
    pub fn new(
        url: &str,
        latency: u32,
        raw_width: u32,
        raw_height: u32,
        hw_accel: HwAccel,
    ) -> Result<Self, H264CaptureError> {
        if !gstreamer::INITIALIZED.load(std::sync::atomic::Ordering::Relaxed) {
            gstreamer::init()?;
        }

        // RGBA decode branch differs by hw_accel:
        //   nvidia — nvv4l2decoder ! nvvidconv (Jetson VIC, hardware decode + scale)
        //   cpu    — avdec_h264 ! videoconvert ! videoscale (software, portable)
        let rgba_branch = match hw_accel {
            HwAccel::Nvidia => format!(
                "nvv4l2decoder ! nvvidconv ! \
                 video/x-raw,format=RGBA,width={raw_width},height={raw_height}"
            ),
            HwAccel::Cpu => format!(
                "avdec_h264 ! videoconvert ! videoscale ! \
                 video/x-raw,format=RGBA,width={raw_width},height={raw_height}"
            ),
        };

        // Two-branch tee:
        //   h264sink — raw Annex-B bytes for Zenoh compressed topic
        //   rgbasink — decoded + resized RGBA for SHM raw topic
        let pipeline_desc = format!(
            "rtspsrc location={url} latency={latency} ! \
             rtph264depay ! h264parse config-interval=-1 ! \
             video/x-h264,stream-format=byte-stream,alignment=au ! \
             tee name=t \
             t. ! queue max-size-buffers=2 leaky=downstream ! \
               appsink name=h264sink emit-signals=true sync=false max-buffers=30 drop=true \
             t. ! queue max-size-buffers=2 leaky=downstream ! \
               {rgba_branch} ! \
               appsink name=rgbasink emit-signals=true sync=false max-buffers=2 drop=true"
        );

        let pipeline = gstreamer::parse::launch(&pipeline_desc)?
            .dynamic_cast::<gstreamer::Pipeline>()
            .map_err(|_| H264CaptureError::DowncastError)?;

        // Bounded to match appsink max-buffers (30). Prevents unbounded RAM growth
        // if the Zenoh publisher stalls or the event loop blocks on SHM allocation.
        let (h264_tx, h264_rx) = flume::bounded::<H264Frame>(30);
        let (rgba_tx, rgba_rx) = flume::bounded::<RgbaFrame>(2);

        // Wire H264 appsink
        let h264sink = pipeline
            .by_name("h264sink")
            .ok_or(H264CaptureError::ElementNotFound("h264sink"))?
            .dynamic_cast::<gstreamer_app::AppSink>()
            .map_err(|_| H264CaptureError::DowncastError)?;

        h264sink.set_callbacks(
            gstreamer_app::AppSinkCallbacks::builder()
                .new_sample({
                    let mut sequence: u32 = 0;
                    move |sink| {
                        if let Ok(frame) = Self::pull_h264(sink, sequence) {
                            sequence = sequence.wrapping_add(1);
                            let _ = h264_tx.try_send(frame);
                        }
                        Ok(gstreamer::FlowSuccess::Ok)
                    }
                })
                .build(),
        );

        // Wire RGBA appsink
        let rgbasink = pipeline
            .by_name("rgbasink")
            .ok_or(H264CaptureError::ElementNotFound("rgbasink"))?
            .dynamic_cast::<gstreamer_app::AppSink>()
            .map_err(|_| H264CaptureError::DowncastError)?;

        rgbasink.set_callbacks(
            gstreamer_app::AppSinkCallbacks::builder()
                .new_sample({
                    let mut sequence: u32 = 0;
                    move |sink| {
                        if let Ok(frame) = Self::pull_rgba(sink, sequence) {
                            sequence = sequence.wrapping_add(1);
                            let _ = rgba_tx.try_send(frame);
                        }
                        Ok(gstreamer::FlowSuccess::Ok)
                    }
                })
                .build(),
        );

        Ok(Self {
            pipeline,
            h264_rx,
            rgba_rx,
        })
    }

    fn pull_h264(
        sink: &gstreamer_app::AppSink,
        sequence: u32,
    ) -> Result<H264Frame, H264CaptureError> {
        let sample = sink
            .pull_sample()
            .map_err(|_| H264CaptureError::BufferError)?;
        let buffer = sample.buffer_owned().ok_or(H264CaptureError::BufferError)?;

        let pts = buffer
            .pts()
            .or_else(|| buffer.dts())
            .map(|t| t.nseconds())
            .unwrap_or(0);
        let keyframe = !buffer.flags().contains(gstreamer::BufferFlags::DELTA_UNIT);

        let mapped = buffer
            .into_mapped_buffer_readable()
            .map_err(|_| H264CaptureError::BufferError)?;

        Ok(H264Frame {
            buffer: mapped,
            pts,
            keyframe,
            sequence,
        })
    }

    fn pull_rgba(
        sink: &gstreamer_app::AppSink,
        sequence: u32,
    ) -> Result<RgbaFrame, H264CaptureError> {
        let sample = sink
            .pull_sample()
            .map_err(|_| H264CaptureError::BufferError)?;
        let buffer = sample.buffer_owned().ok_or(H264CaptureError::BufferError)?;

        let pts = buffer
            .pts()
            .or_else(|| buffer.dts())
            .map(|t| t.nseconds())
            .unwrap_or(0);

        let mapped = buffer
            .into_mapped_buffer_readable()
            .map_err(|_| H264CaptureError::BufferError)?;

        // Copy into owned Vec so it can be moved to the Zenoh SHM writer.
        // Width/height are known from the pipeline caps (fixed at construction).
        let data = mapped.as_slice().to_vec();

        Ok(RgbaFrame {
            pts,
            sequence,
            data,
        })
    }

    pub fn start(&self) -> Result<(), H264CaptureError> {
        self.pipeline.set_state(gstreamer::State::Playing)?;
        Ok(())
    }

    pub fn h264_receiver(&self) -> &flume::Receiver<H264Frame> {
        &self.h264_rx
    }

    pub fn rgba_receiver(&self) -> &flume::Receiver<RgbaFrame> {
        &self.rgba_rx
    }

    pub fn close(&self) -> Result<(), H264CaptureError> {
        let _ = self.pipeline.send_event(gstreamer::event::Eos::new());
        self.pipeline.set_state(gstreamer::State::Null)?;
        Ok(())
    }
}

impl Drop for H264StreamCapture {
    fn drop(&mut self) {
        let _ = self.close();
    }
}
