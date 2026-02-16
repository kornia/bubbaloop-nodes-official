fn main() -> Result<(), Box<dyn std::error::Error>> {
    let protos_dir = std::path::Path::new("protos");
    if !protos_dir.exists() {
        return Ok(());
    }

    let proto_files: Vec<_> = std::fs::read_dir(protos_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|ext| ext == "proto"))
        .collect();

    if proto_files.is_empty() {
        return Ok(());
    }

    let out_dir = std::path::PathBuf::from(std::env::var("OUT_DIR")?);

    let proto_strs: Vec<_> = proto_files.iter().filter_map(|p| p.to_str()).collect();
    prost_build::Config::new()
        .extern_path(".bubbaloop.header.v1", "::bubbaloop_schemas::header::v1")
        .type_attribute(".", "#[derive(serde::Serialize, serde::Deserialize)]")
        .file_descriptor_set_path(out_dir.join("descriptor.bin"))
        .compile_protos(&proto_strs, &["protos/"])?;

    for f in &proto_files {
        println!("cargo:rerun-if-changed={}", f.display());
    }
    println!("cargo:rerun-if-changed=protos");
    Ok(())
}
