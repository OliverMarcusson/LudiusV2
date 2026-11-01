//! Share outcome checks through the optional `share` add-on (crates/ludius-share); without it, unknown.
use crate::guestfile::GuestFile;

#[cfg(feature = "share")]
pub fn unc(share: &str, name: &str) -> String {
    ludius_share::unc(share, name)
}

#[cfg(feature = "share")]
pub fn read(share: &str, name: &str) -> Option<GuestFile> {
    Some(match ludius_share::read(share, name, crate::guestfile::MAX_BYTES as usize)? {
        None => GuestFile::missing(),
        Some(data) => GuestFile { exists: true, written_ms: 0, data },
    })
}

/// Without the share add-on there is no share path, so a share check refuses to start.
#[cfg(not(feature = "share"))]
pub fn unc(_share: &str, _name: &str) -> String {
    String::new()
}

#[cfg(not(feature = "share"))]
pub fn read(_share: &str, _name: &str) -> Option<GuestFile> {
    None
}
