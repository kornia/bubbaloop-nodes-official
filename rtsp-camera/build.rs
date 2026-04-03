fn main() -> Result<(), Box<dyn std::error::Error>> {
    // DEP_BUBBALOOP_NODE_PROTOS_DIR is set by the bubbaloop-node build script
    // (via `links = "bubbaloop-node"` in its Cargo.toml). This lets us import
    // header.proto from the SDK without keeping a local copy.
    let sdk_protos = std::env::var("DEP_BUBBALOOP_NODE_PROTOS_DIR")
        .expect("DEP_BUBBALOOP_NODE_PROTOS_DIR not set — check bubbaloop-node links field");

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
