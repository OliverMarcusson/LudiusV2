fn main() {
    if std::env::var("CARGO_CFG_TARGET_ENV").as_deref() != Ok("msvc") {
        return;
    }
    let manifest = if std::env::var_os("CARGO_FEATURE_UIACCESS").is_some() { "app.uiaccess.manifest" } else { "app.manifest" };
    let path = std::path::Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("manifest").join(manifest);
    // The manifest carries the trustInfo (uiAccess); the linker must not add its own.
    println!("cargo:rustc-link-arg-bins=/MANIFEST:EMBED");
    println!("cargo:rustc-link-arg-bins=/MANIFESTUAC:NO");
    println!("cargo:rustc-link-arg-bins=/MANIFESTINPUT:{}", path.display());
    println!("cargo:rerun-if-changed={}", path.display());
}
