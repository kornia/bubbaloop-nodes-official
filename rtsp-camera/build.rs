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

    prost_build::Config::new()
        .file_descriptor_set_path(out_dir.join("descriptor.bin"))
        .compile_protos(
            &proto_files.iter().map(|p| p.as_path()).collect::<Vec<_>>(),
            &[protos_dir],
        )?;

    for f in &proto_files {
        println!("cargo:rerun-if-changed={}", f.display());
    }
    println!("cargo:rerun-if-changed=protos");
    Ok(())
}
