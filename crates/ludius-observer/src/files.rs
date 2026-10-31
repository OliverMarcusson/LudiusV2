//! ReadFile: a file in one of the user's noise folders, never through a link, never another file.
use ludius_proto::v1::{FileContent, ReadFile};
use std::io::Read;
use std::os::windows::fs::{MetadataExt, OpenOptionsExt};
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::Storage::FileSystem::{FILE_NAME_NORMALIZED, GetFinalPathNameByHandleW};

pub const MAX: u32 = 8 << 20;
const NOISE_FOLDERS: &[&str] = &["Documents", "Pictures", "Downloads"];
const REPARSE_POINT: u32 = 0x400;

fn is_link(p: &Path) -> Result<bool, String> {
    match std::fs::symlink_metadata(p) {
        Ok(m) => Ok(m.file_type().is_symlink() || m.file_attributes() & REPARSE_POINT != 0),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(format!("IOException: {e}")),
    }
}

fn final_path(f: &std::fs::File) -> Result<String, String> {
    let mut buf = vec![0u16; 32768];
    let n = unsafe { GetFinalPathNameByHandleW(HANDLE(f.as_raw_handle()), &mut buf, FILE_NAME_NORMALIZED) } as usize;
    if n == 0 || n >= buf.len() {
        return Err("IOException: final path unavailable".into());
    }
    let p = String::from_utf16_lossy(&buf[..n]);
    Ok(if p.starts_with(r"\\?\UNC\") { p } else { p.strip_prefix(r"\\?\").map(str::to_string).unwrap_or(p) })
}

pub fn read(r: &ReadFile, profile: &Path) -> Result<FileContent, String> {
    let deny = |m: &str| Err(format!("UnauthorizedAccessException: {m}"));
    let full = |p: &Path| std::path::absolute(p).map_err(|e| format!("ArgumentException: {e}"));
    let root = full(profile)?.to_string_lossy().trim_end_matches('\\').to_string();
    let path = full(Path::new(&r.path))?.to_string_lossy().into_owned();
    let prefix = format!("{root}\\");
    if path.len() <= prefix.len() || !path.is_char_boundary(prefix.len()) || !path[..prefix.len()].eq_ignore_ascii_case(&prefix) {
        return deny("outside the user's profile");
    }
    let parts: Vec<&str> = path[prefix.len()..].split('\\').collect();
    if parts.len() < 2 || !NOISE_FOLDERS.iter().any(|f| f.eq_ignore_ascii_case(parts[0])) || parts.iter().any(|p| p.is_empty()) {
        return deny("not in a noise folder");
    }

    let mut dir = PathBuf::from(&root);
    for part in std::iter::once("").chain(parts[..parts.len() - 1].iter().copied()) {
        if !part.is_empty() {
            dir.push(part);
        }
        if is_link(&dir)? {
            return Err("IOException: a link on the path".into());
        }
        if !dir.is_dir() {
            return Err("DirectoryNotFoundException: no such folder".into());
        }
    }

    let file = Path::new(&path);
    if is_link(file)? {
        return Err("IOException: the file is a link".into());
    }
    if file.is_dir() {
        return Err("IOException: not a file".into());
    }
    let mut f = match std::fs::OpenOptions::new().read(true).share_mode(7).open(file) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(FileContent { exists: false, ..Default::default() }),
        Err(e) => return Err(format!("IOException: {e}")),
    };
    if !final_path(&f)?.eq_ignore_ascii_case(&path) {
        return Err("IOException: the opened file is not the requested one".into());
    }
    let max = if r.max_bytes > 0 && r.max_bytes <= MAX { r.max_bytes } else { MAX } as u64;
    let meta = f.metadata().map_err(|e| format!("IOException: {e}"))?;
    if meta.len() > max {
        return Err("IOException: too large".into());
    }
    let mut data = Vec::with_capacity(meta.len() as usize);
    f.by_ref().take(max + 1).read_to_end(&mut data).map_err(|e| format!("IOException: {e}"))?;
    if data.len() as u64 > max {
        return Err("IOException: too large".into());
    }
    let written_ms = meta.modified().ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0, |d| d.as_millis() as i64);
    Ok(FileContent { exists: true, written_ms, data })
}
