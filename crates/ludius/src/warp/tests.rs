use super::*;
use crate::rangerun;

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("ludius-warp-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn down(msg: &'static str) -> RangeClock {
    RangeClock::new(110, vec![111], Box::new(move |_, _| bail!(msg)))
}

/// Guests that follow whatever the clock sets (always succeed).
fn fine() -> RangeClock {
    RangeClock::new(110, vec![111], Box::new(|_, _| Ok(Utc::now().timestamp_millis().to_string())))
}

struct NoDays;

impl Days for NoDays {
    fn plan(&self, _: NaiveDate) -> BTreeMap<String, Result<DayPlan>> {
        panic!("planned")
    }
    fn play(&self, _: &BTreeMap<String, DayPlan>, _: bool, _: &mut Supervisor, _: &AtomicBool) -> Result<Option<BTreeMap<String, Value>>> {
        panic!("played")
    }
    fn stop(&self, _: &[String]) -> Vec<String> {
        vec![]
    }
}

fn warp<'a>(runs: &Path, users: &[&str], clock: RangeClock, stop: &'a AtomicBool) -> Warp<'a> {
    Warp {
        runs: runs.into(),
        users: users.iter().map(|u| u.to_string()).collect(),
        tz: chrono_tz::Europe::Stockholm,
        plan_at: NaiveTime::from_hms_opt(22, 0, 0).unwrap(),
        clock,
        lease: Lease::new(&lease_path(runs), "range"),
        sup: Supervisor::new(vec![], &runs.join("range"), Box::new(rangerun::spawn)),
        days: &NoDays,
        stop,
    }
}

fn day() -> Vec<NaiveDate> {
    dates(Some("2099-01-05"), 1, NaiveDate::from_ymd_opt(2099, 1, 1).unwrap()).unwrap()
}

fn events(runs: &Path) -> Vec<String> {
    std::fs::read_to_string(runs.join("range/warp.jsonl")).unwrap().lines().map(|l| serde_json::from_str::<Value>(l).unwrap()["event"].as_str().unwrap().to_string()).collect()
}

#[test]
fn dates_are_future_weekdays() {
    let today = NaiveDate::from_ymd_opt(2026, 10, 31).unwrap();
    let d = dates(Some("2026-11-06"), 3, today).unwrap();
    assert_eq!(d.iter().map(|d| d.to_string()).collect::<Vec<_>>(), ["2026-11-06", "2026-11-09", "2026-11-10"]);
    assert!(dates(Some("2026-10-31"), 1, today).unwrap_err().contains("forward only"));
    assert!(dates(Some("2026-11-02"), 8, today).is_err() && dates(None, 1, today).is_err() && dates(Some("x"), 1, today).is_err());
}

#[test]
fn unwarp_exits_3_and_keeps_the_lease_when_reset_fails() {
    let runs = tmp("unwarp");
    let lease = lease_path(&runs);
    std::fs::create_dir_all(lease.parent().unwrap()).unwrap();
    std::fs::write(&lease, "{}").unwrap();
    assert_eq!(unwarp(|| Ok(down("guest agent not running")), &lease), 3);
    assert!(lease.exists());
    assert_eq!(unwarp(|| Ok(fine()), &lease), 0);
    assert!(!lease.exists());
}

fn stale_lease(runs: &Path, age_s: i64) -> (PathBuf, DateTime<Utc>) {
    let now = DateTime::parse_from_rfc3339("2026-10-31T12:00:00Z").unwrap().to_utc();
    let p = lease_path(runs);
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    let renewed = (now - chrono::Duration::seconds(age_s)).to_rfc3339();
    std::fs::write(&p, json!({"vm": "range", "pid": 999_999_999u64, "renewed": renewed}).to_string()).unwrap();
    (p, now)
}

#[test]
fn sweep_resets_only_an_orphaned_lease() {
    let runs = tmp("sweep");
    let (p, now) = stale_lease(&runs, 5);
    assert_eq!(sweep(|| panic!("fresh lease reset"), &p, Some(now)).unwrap(), 0);
    assert!(p.exists());
    let (p, now) = stale_lease(&runs, clocklease::STALE_S + 5);
    assert_eq!(sweep(|| Ok(down("guest agent not running")), &p, Some(now)).unwrap(), 3);
    assert!(p.exists(), "kept for the next sweep");
    assert_eq!(sweep(|| Ok(fine()), &p, Some(now)).unwrap(), 0);
    assert!(!p.exists());
}

#[test]
fn warp_exits_3_when_reset_fails_after_the_warp_failed() {
    let runs = tmp("reset");
    std::fs::create_dir_all(runs.join("world")).unwrap();
    std::fs::write(runs.join("world/narrative.yaml"), "{}").unwrap();
    let stop = AtomicBool::new(false);
    assert_eq!(warp(&runs, &[], down("guest agent hung mid-jump"), &stop).run(&day()).unwrap(), 3);
    assert!(lease_path(&runs).exists());
    assert_eq!(events(&runs), ["warp_failed", "clock_reset_failed"]);
}

#[test]
fn warp_without_narrative_fails_visibly_before_any_jump() {
    let runs = tmp("narrative");
    let stop = AtomicBool::new(false);
    let e = warp(&runs, &[], down("clock jumped"), &stop).run(&day()).unwrap_err();
    assert!(e.to_string().contains("plan narrative --date 2099-01-05"), "{e}");
    assert_eq!(events(&runs), ["warp_failed"]);
}

#[test]
fn warp_refuses_a_reserved_desktop_before_any_jump() {
    let runs = tmp("reserved");
    std::fs::create_dir_all(runs.join("world")).unwrap();
    std::fs::write(runs.join("world/narrative.yaml"), "{}").unwrap();
    std::fs::create_dir_all(runs.join("locks")).unwrap();
    let plan = json!({"persona": "lnord", "date": "2027-02-03", "items": []});
    let day_json = json!({"owner": "ludius/day-lnord-2027-02-03", "day": {"plan": plan, "clock": "time-jump", "utc_offset_s": 0}});
    std::fs::write(runs.join("locks/ludius-lnord.day.json"), day_json.to_string()).unwrap();
    let stop = AtomicBool::new(false);
    let e = warp(&runs, &["aberg", "lnord"], down("clock jumped"), &stop).run(&day()).unwrap_err();
    assert!(e.to_string().contains("lnord by ludius/day-lnord-2027-02-03"), "{e}");
}

#[test]
fn a_raised_stop_ends_the_warp_before_any_jump() {
    let runs = tmp("stop");
    std::fs::create_dir_all(runs.join("world")).unwrap();
    std::fs::write(runs.join("world/narrative.yaml"), "{}").unwrap();
    let stop = AtomicBool::new(true);
    assert_eq!(warp(&runs, &[], down("clock jumped"), &stop).run(&day()).unwrap(), 0);
    assert_eq!(events(&runs), ["interrupted"]);
}
