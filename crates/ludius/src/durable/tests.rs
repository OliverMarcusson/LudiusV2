use super::*;
use crate::nightly::worker::is_final;

fn input(date: &str) -> DayInput {
    let plan: DayPlan = serde_json::from_value(serde_json::json!({"persona": "aberg", "date": date, "items": []})).unwrap();
    DayInput { plan, clock: Clock::Realtime, utc_offset_s: 3600, heartbeat_s: 300, retry_s: 30, canary: false }
}

#[test]
fn reservation_is_claimed_once_per_owner() {
    let dir = std::env::temp_dir().join(format!("ludius-durable-{}", std::process::id()));
    let owner = DayOwner::new(&dir.join("WS01.lock"));
    assert!(owner.check("ns/a").unwrap().is_none());
    owner.claim("ns/a", &input("2026-11-02")).unwrap();
    owner.claim("ns/a", &input("2026-11-02")).unwrap(); // a retried prepare re-claims
    assert!(owner.claim("ns/a", &input("2026-11-03")).is_err_and(|e| is_final(&e)), "changed input");
    assert!(owner.claim("ns/b", &input("2026-11-02")).is_err_and(|e| is_final(&e)), "another owner");
    assert!(owner.check("ns/b").is_err_and(|e| is_final(&e)));
    owner.release("ns/a").unwrap();
    assert!(owner.read().unwrap().is_none());
    std::fs::write(dir.join("WS01.day.json"), "{").unwrap();
    assert!(owner.read().is_err(), "unreadable is unknown, not free");
}
