fn main() -> Result<(), Box<dyn std::error::Error>> {
    bubbaloop_node_build::compile_protos(&["protos/camera.proto"])
}
