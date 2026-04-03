fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Resolve the SDK protos directory from the DEP_ metadata set by bubbaloop-node's build.rs,
    // falling back to the CARGO_MANIFEST_DIR-relative local copy for reproducibility.
    let sdk_protos = std::env::var("DEP_BUBBALOOP_NODE_PROTOS_DIR")
        .unwrap_or_else(|_| "protos".to_string());

    let out_dir = std::path::PathBuf::from(std::env::var("OUT_DIR")?);

    prost_build::Config::new()
        // Header type comes from the SDK crate — do not generate Rust code for it.
        .extern_path(".bubbaloop.header.v1", "::bubbaloop_node::schemas::header::v1")
        .type_attribute(".", "#[derive(serde::Serialize, serde::Deserialize)]")
        .file_descriptor_set_path(out_dir.join("descriptor.bin"))
        .compile_protos(&["protos/camera.proto"], &["protos/", &sdk_protos])?;

    println!("cargo:rerun-if-changed=protos/camera.proto");
    Ok(())
}
