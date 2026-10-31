//! Files on the range's SMB shares, read with smbclient (outcome checks only).
use std::process::Command;

use crate::guestfile::{GuestFile, MAX_BYTES};
use crate::paths::env;

fn name_ok(s: &str) -> bool {
    let b = s.as_bytes();
    (1..=121).contains(&b.len())
        && b[0].is_ascii_alphanumeric()
        && b.iter().all(|c| c.is_ascii_alphanumeric() || b" ._()-".contains(c))
}

pub fn unc(share: &str, name: &str) -> String {
    let host = env("LUDIUS_SHARE_HOST");
    if host.is_empty() {
        return String::new();
    }
    let tail = if name.is_empty() { String::new() } else { format!("\\{name}") };
    format!("\\\\{host}\\{share}{tail}")
}

pub fn read(share: &str, name: &str) -> Option<GuestFile> {
    let (host, addr, user, secret) = (env("LUDIUS_SHARE_HOST"), env("LUDIUS_SRV_HOST"), env("LUDIUS_EWS_USER"), env("LUDIUS_EWS_PASSWORD_FILE"));
    if host.is_empty() || addr.is_empty() || user.is_empty() || secret.is_empty() || !name_ok(share) || !name_ok(name) {
        return None;
    }
    let password = std::fs::read_to_string(&secret).ok()?.trim().to_string();
    let tmp = std::env::temp_dir().join(format!("ludius-share-{}-{}", std::process::id(), rand::random::<u64>()));
    let out = Command::new("timeout")
        .args(["-k", "5", "60", "smbclient", &format!("//{host}/{share}"), "-I", &addr, "-U", &user, "-m", "SMB3", "-c"])
        .arg(format!("get \"{name}\" {}", tmp.display()))
        .env("PASSWD", password)
        .output()
        .ok()?;
    let said = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    let data = std::fs::read(&tmp).ok();
    let _ = std::fs::remove_file(&tmp);
    if said.contains("NT_STATUS_OBJECT_NAME_NOT_FOUND") && !said.contains("NT_STATUS_LOGON") {
        return Some(GuestFile::missing());
    }
    let data = data?;
    if !out.status.success() || data.len() > MAX_BYTES as usize {
        return None;
    }
    Some(GuestFile { exists: true, written_ms: 0, data })
}

#[cfg(test)]
mod tests {
    #[test]
    fn names() {
        assert!(super::name_ok("Finance (Q3).xlsx"));
        assert!(!super::name_ok("../x"));
        assert!(!super::name_ok("a\"b"));
    }
}
