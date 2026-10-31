use std::path::PathBuf;

/// Repo root: LUDIUS_ROOT, else the working directory (the box runs from the checkout).
pub fn root() -> PathBuf {
    std::env::var_os("LUDIUS_ROOT").map(PathBuf::from).unwrap_or_else(|| std::env::current_dir().unwrap_or_default())
}

pub fn runs() -> PathBuf {
    std::env::var_os("LUDIUS_RUNS").map(PathBuf::from).unwrap_or_else(|| root().join("runs"))
}

/// Templates, apps, personas: LUDIUS_DATA, else controller/data in the checkout.
pub fn data() -> PathBuf {
    std::env::var_os("LUDIUS_DATA").map(PathBuf::from).unwrap_or_else(|| root().join("controller").join("data"))
}

pub fn env(key: &str) -> String {
    std::env::var(key).unwrap_or_default()
}
