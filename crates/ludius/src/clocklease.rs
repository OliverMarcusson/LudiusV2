//! The range clock lease (port of clocklease.py): a warp renews the lease file while the range clocks are
//! jumped; a sweeper resets the clocks once the holder stops renewing it.
use std::path::{Path, PathBuf};
use std::sync::mpsc::{RecvTimeoutError, Sender, TryRecvError, channel};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Duration;

use anyhow::Result;
use chrono::{DateTime, Utc};
use serde_json::{Value, json};

pub const RENEW: Duration = Duration::from_secs(30);
pub const STALE_S: i64 = 120;

pub struct Lease {
    pub path: PathBuf,
    pub vm: String,
    pub renew: Duration,
    lock: Arc<Mutex<()>>,
    renewer: Option<(Sender<()>, JoinHandle<()>)>,
}

impl Lease {
    pub fn new(path: &Path, vm: &str) -> Self {
        Self { path: path.into(), vm: vm.into(), renew: RENEW, lock: Arc::default(), renewer: None }
    }

    pub fn held(&self) -> bool {
        self.renewer.is_some()
    }

    pub fn hold(&mut self) -> Result<()> {
        {
            let _g = self.lock.lock().unwrap();
            write(&self.path, &self.vm)?;
        }
        if self.renewer.is_none() {
            let (stop, rx) = channel::<()>();
            let (path, vm, lock, every) = (self.path.clone(), self.vm.clone(), self.lock.clone(), self.renew);
            let t = std::thread::Builder::new().name("clock-lease".into()).spawn(move || {
                // release drops the sender, which ends the loop; a failed write is retried on the next tick
                while rx.recv_timeout(every) == Err(RecvTimeoutError::Timeout) {
                    let _g = lock.lock().unwrap();
                    // release may have run while this tick waited for the lock
                    if rx.try_recv() == Err(TryRecvError::Disconnected) {
                        break;
                    }
                    let _ = write(&path, &vm);
                }
            })?;
            self.renewer = Some((stop, t));
        }
        Ok(())
    }

    pub fn release(&mut self) {
        let renewer = {
            let _g = self.lock.lock().unwrap();
            let _ = std::fs::remove_file(&self.path);
            self.renewer.take().map(|(stop, t)| {
                drop(stop);
                t
            })
        };
        if let Some(t) = renewer {
            let _ = t.join();
        }
    }
}

fn write(path: &Path, vm: &str) -> Result<()> {
    std::fs::create_dir_all(path.parent().unwrap_or(Path::new(".")))?;
    let tmp = path.with_extension("tmp");
    let now = Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Secs, false);
    std::fs::write(&tmp, json!({"vm": vm, "pid": std::process::id(), "renewed": now}).to_string())?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Host lock next to the lease: the sweeper's check-reset-delete and a warp's lease-and-jump never interleave.
pub fn exclusive<T>(path: &Path, f: impl FnOnce() -> T) -> Result<T> {
    std::fs::create_dir_all(path.parent().unwrap_or(Path::new(".")))?;
    let lock = std::fs::OpenOptions::new().write(true).create(true).truncate(false).open(path.with_extension("lock"))?;
    lock.lock()?;
    Ok(f())
}

/// The lease if its holder stopped renewing it; None when absent, fresh, unreadable or its process still runs.
pub fn orphan(path: &Path, now: Option<DateTime<Utc>>) -> Option<Value> {
    let rec: Value = serde_json::from_str(&std::fs::read_to_string(path).ok()?).ok()?;
    let renewed = DateTime::parse_from_rfc3339(rec.get("renewed")?.as_str()?).ok()?;
    if (now.unwrap_or_else(Utc::now) - renewed.to_utc()).num_seconds() < STALE_S {
        return None;
    }
    let pid = rec["pid"].as_u64().unwrap_or(0);
    if std::fs::read(format!("/proc/{pid}/cmdline")).is_ok_and(|c| c.windows(6).any(|w| w == b"ludius")) {
        return None;
    }
    Some(rec)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lease_renews_until_released_and_goes_stale() {
        let path = std::env::temp_dir().join(format!("ludius-lease-{}", std::process::id())).join("clock.lease");
        let mut lease = Lease::new(&path, "range");
        lease.renew = Duration::from_millis(20);
        lease.hold().unwrap();
        lease.hold().unwrap(); // a second hold renews, one thread
        assert!(lease.held() && path.exists());
        std::thread::sleep(Duration::from_millis(60));
        let rec: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(rec["vm"], "range");
        assert!(orphan(&path, None).is_none(), "fresh");
        let later = Utc::now() + chrono::Duration::seconds(STALE_S + 5);
        // the holder is this test binary (ludius-<hash>): still running on Linux, unknowable (orphaned) elsewhere
        let stale = orphan(&path, Some(later));
        assert!(if cfg!(target_os = "linux") { stale.is_none() } else { stale.is_some_and(|r| r["vm"] == "range") });
        exclusive(&path, || lease.release()).unwrap();
        assert!(!lease.held() && !path.exists());
        assert!(orphan(&path, Some(later)).is_none(), "absent");
        std::fs::write(&path, "{").unwrap();
        assert!(orphan(&path, Some(later)).is_none(), "unreadable");
    }
}
