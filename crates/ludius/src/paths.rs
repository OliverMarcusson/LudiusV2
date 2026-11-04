use std::path::PathBuf;

/// Repo root: LUDIUS_ROOT, else the working directory (the box runs from the checkout).
pub fn root() -> PathBuf {
    std::env::var_os("LUDIUS_ROOT").map(PathBuf::from).unwrap_or_else(|| std::env::current_dir().unwrap_or_default())
}

pub fn runs() -> PathBuf {
    std::env::var_os("LUDIUS_RUNS").map(PathBuf::from).unwrap_or_else(|| root().join("runs"))
}

/// Templates, apps, personas: LUDIUS_DATA, else data/ in the checkout.
pub fn data() -> PathBuf {
    std::env::var_os("LUDIUS_DATA").map(PathBuf::from).unwrap_or_else(|| root().join("data"))
}

/// Persona files: LUDIUS_PERSONAS (a range set up by `ludius setup`), else data/personas.
pub fn personas() -> PathBuf {
    std::env::var_os("LUDIUS_PERSONAS").map(PathBuf::from).unwrap_or_else(|| data().join("personas"))
}

/// The *.yaml files in a directory, sorted by name.
pub fn yaml_files(dir: &std::path::Path) -> std::io::Result<Vec<PathBuf>> {
    let mut paths: Vec<PathBuf> = std::fs::read_dir(dir)?.map(|e| e.map(|e| e.path())).collect::<std::io::Result<_>>()?;
    paths.retain(|p| p.extension().is_some_and(|x| x == "yaml"));
    paths.sort();
    Ok(paths)
}

pub fn env(key: &str) -> String {
    std::env::var(key).unwrap_or_default()
}
