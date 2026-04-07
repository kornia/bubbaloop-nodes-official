include!(concat!(env!("OUT_DIR"), "/bubbaloop.camera.v1.rs"));

impl bubbaloop_node::MessageTypeName for CompressedImage {
    fn type_name() -> &'static str {
        "bubbaloop.camera.v1.CompressedImage"
    }
}

impl bubbaloop_node::MessageTypeName for RawImage {
    fn type_name() -> &'static str {
        "bubbaloop.camera.v1.RawImage"
    }
}
