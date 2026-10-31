fn main() -> Result<(), Box<dyn std::error::Error>> {
    let files = ["../../proto/ludius/v1/agent.proto", "../../proto/ludius/v1/observation.proto"];
    let fds = protox::compile(files, ["../../proto"])?;
    tonic_prost_build::configure().compile_fds(fds)?;
    for f in files {
        println!("cargo:rerun-if-changed={f}");
    }
    Ok(())
}
