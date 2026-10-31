//! Watchdog: a request hung for 60 s or 5 failed requests in a row end the process,
//! so the RangeAssist task (triggered by Application Error 1000) starts a fresh one.
use crate::settle::now;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU32, AtomicU64, Ordering::Relaxed};

const HUNG_MS: u64 = 60_000;
const MAX_FAILURES: u32 = 5;
const IDLE: u64 = u64::MAX;

static BUSY_SINCE: AtomicU64 = AtomicU64::new(IDLE);
static LAST_ACTIVITY: AtomicU64 = AtomicU64::new(0);
static FAILURES: AtomicU32 = AtomicU32::new(0);
static STEP: Mutex<String> = Mutex::new(String::new());

/// What the current request is doing, for the hang report.
pub fn step(s: &str) {
    if let Ok(mut g) = STEP.lock() {
        g.clear();
        g.push_str(s);
    }
}

pub fn begin() {
    BUSY_SINCE.store(now(), Relaxed);
}

pub fn end(ok: bool) {
    if ok {
        FAILURES.store(0, Relaxed);
    } else {
        FAILURES.fetch_add(1, Relaxed);
    }
    LAST_ACTIVITY.store(now(), Relaxed);
    BUSY_SINCE.store(IDLE, Relaxed);
}

pub fn fault() -> Option<String> {
    let since = BUSY_SINCE.load(Relaxed);
    if since != IDLE && now().saturating_sub(since) > HUNG_MS {
        let step = STEP.lock().map(|s| s.clone()).unwrap_or_default();
        return Some(format!("request hung for {} s during {step}", (now() - since) / 1000));
    }
    let n = FAILURES.load(Relaxed);
    (n >= MAX_FAILURES).then(|| format!("{n} requests failed in a row"))
}

pub fn idle(quiet_ms: u64) -> bool {
    BUSY_SINCE.load(Relaxed) == IDLE && now().saturating_sub(LAST_ACTIVITY.load(Relaxed)) >= quiet_ms
}

/// Checks every second; a fault aborts (__fastfail), which Windows logs as Application Error 1000.
pub fn spawn() {
    LAST_ACTIVITY.store(now(), Relaxed);
    std::thread::spawn(|| {
        loop {
            std::thread::sleep(std::time::Duration::from_secs(1));
            if let Some(f) = fault() {
                eprintln!("observer watchdog: {f}");
                std::process::abort();
            }
        }
    });
}
