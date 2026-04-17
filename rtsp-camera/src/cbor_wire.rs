use serde::{Deserialize, Serialize};

#[derive(Debug, Serialize, Deserialize)]
pub struct HeaderCbor {
    pub acq_time: u64,
    pub pub_time: u64,
    pub sequence: u32,
    pub frame_id: String,
    pub machine_id: String,
}

#[derive(Debug, Serialize)]
pub struct CompressedImageCborRef<'a> {
    pub header: &'a HeaderCbor,
    pub format: &'a str,
    #[serde(with = "serde_bytes")]
    pub data: &'a [u8],
}

#[derive(Debug, Deserialize)]
pub struct CompressedImageCborOwned {
    pub header: HeaderCbor,
    pub format: String,
    #[serde(with = "serde_bytes")]
    pub data: Vec<u8>,
}

#[derive(Debug, Serialize)]
pub struct RawImageCborRef<'a> {
    pub header: &'a HeaderCbor,
    pub width: u32,
    pub height: u32,
    pub encoding: &'a str,
    pub step: u32,
    #[serde(with = "serde_bytes")]
    pub data: &'a [u8],
}

impl RawImageCborRef<'_> {
    /// Upper bound for CBOR-encoded header/metadata bytes on top of pixel data.
    /// Covers the inner `HeaderCbor` plus the SDK `Envelope` wrapper (schema_uri,
    /// source_instance, ts_ns, monotonic_seq) that wraps the published body.
    pub const HEADER_OVERHEAD_BYTES: usize = 1024;
}
