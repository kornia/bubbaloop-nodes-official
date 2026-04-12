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
