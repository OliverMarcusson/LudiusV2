use std::sync::Barrier;

use chrono::TimeZone;
use serde_json::json;

use super::*;
use crate::planner::Narrative;

const DAY: &str = "2026-10-28";

fn date() -> NaiveDate {
    DAY.parse().unwrap()
}

fn persona(name: &str, user: &str) -> Persona {
    serde_json::from_value(json!({"name": name, "user": user, "role": "Finance controller", "department": "Finance"})).unwrap()
}

fn item(at: &str, query: &str) -> PlanItem {
    serde_json::from_value(json!({"at": at, "template": "edge_web_lookup", "params": {"search_query": query}, "context": "Checks the lease rules before the close."})).unwrap()
}

fn no_inbox(_: &str) -> Option<Vec<Message>> {
    Some(vec![])
}

fn setup(name: &str) -> (World, Sources<'static>) {
    crate::day::tests::log(name);
    let root = std::env::temp_dir().join(format!("ludius-nightly-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let world = World::new(&root);
    let n: Narrative = serde_json::from_value(json!({"company": "Harbourline", "city": "Rotterdam", "summary": "Packaging.", "projects": [], "events": [],
        "people": [{"name": "Anna Berg", "role": "Finance controller", "department": "Finance", "user": "aberg"}]}))
    .unwrap();
    world.save_narrative(&n).unwrap();
    let src = Sources { personas: vec![persona("Anna Berg", "aberg"), persona("Lars Nord", "lnord")], templates: planner::templates().unwrap(), policy: Policy::default(), inbox: &no_inbox, decider: None };
    (world, src)
}

fn fixed(items: Vec<PlanItem>) -> impl Fn(&Persona, NaiveDate, &Shared) -> Result<Generated> + Sync {
    move |p, d, _| Ok(Generated { plan: DayPlan { persona: p.user.clone(), date: d.to_string(), items: items.clone() }, rejects: vec![], note: String::new() })
}

fn never(_: &Persona, _: NaiveDate, _: &Shared) -> Result<Generated> {
    panic!("an existing plan must never reach the model")
}

#[test]
fn planning_date_is_the_next_local_day_of_the_occurrence() {
    for ((y, mo, d, h, mi), want) in [((2026, 10, 27, 21, 0), "2026-10-28"), ((2026, 10, 27, 23, 30), "2026-10-29"), ((2026, 10, 25, 0, 30), "2026-10-26"), ((2026, 10, 25, 1, 30), "2026-10-26"), ((2026, 3, 29, 1, 30), "2026-03-30"), ((2026, 10, 30, 21, 0), "2026-10-31")] {
        let at = Utc.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap();
        assert_eq!(planning_date(at, "Europe/Stockholm").unwrap().to_string(), want);
    }
    assert!(planning_date(Utc::now(), "Mars/Base").is_err());
}

#[test]
fn generated_plan_is_kept_on_retry_and_never_overwritten() {
    let (world, src) = setup("kept");
    let shared = Shared::new(&world);
    let anna = &src.personas[0];
    let first = ensure_day(&fixed(vec![item("09:00", "ifrs 16 lease accounting")]), &world, anna, &src, date(), &shared).unwrap();
    assert!(first.generated && first.items == 1 && first.date == DAY, "{first:?}");
    let before = std::fs::read(&first.path).unwrap();
    let again = ensure_day(&never, &world, anna, &src, date(), &shared).unwrap();
    assert!(!again.generated && again.items == 1);
    let mut plan = check_existing(&world, anna, date(), &src).unwrap();
    plan.items.clear();
    assert!(world.save_plan(&plan, &src.templates, true).is_err());
    assert_eq!(std::fs::read(&first.path).unwrap(), before);
}

#[test]
fn conflicting_existing_plan_fails_closed_untouched() {
    let (world, src) = setup("conflict");
    let path = world.plan_path("aberg", DAY);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    for (text, want) in [
        ("persona: lnord\ndate: '2026-10-28'\nitems: []\n", "not aberg"),
        ("persona: aberg\ndate: '2026-10-28'\nitems: [\n", "not a readable plan"),
        ("persona: aberg\ndate: '2026-10-28'\nitems:\n- {at: '09:00', template: nope}\n", "fails validation"),
    ] {
        std::fs::write(&path, text).unwrap();
        let e = ensure_day(&never, &world, &src.personas[0], &src, date(), &Shared::new(&world)).unwrap_err();
        assert!(e.is::<PlanConflict>() && e.to_string().contains(want), "{e:#}");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), text);
    }
}

#[test]
fn personas_plan_in_parallel_and_failures_come_back() {
    let (world, src) = setup("parallel");
    let both = Barrier::new(2);
    let ok = fixed(vec![item("09:00", "ifrs 16 lease accounting"), item("10:30", "supplier payment terms netherlands")]);
    let run = |p: &Persona, d: NaiveDate, s: &Shared| {
        both.wait(); // both personas generate at once
        s.hold("", || { anyhow::ensure!(p.user != "lnord", "no plan"); Ok(()) })?;
        ok(p, d, s)
    };
    let got = ensure_days(&run, &world, &src.personas, &src, date());
    assert!(got["aberg"].as_ref().is_ok_and(|p| p.generated && p.items == 2));
    assert!(got["lnord"].as_ref().is_err_and(|e| e.to_string() == "no plan"));
}

#[test]
fn activity_rejects_bad_personas_and_retries_a_busy_planner() {
    use super::worker::{PlanFire, is_final, plan};
    let (world, src) = setup("activity");
    let shared = Shared::new(&world);
    let fire = |persona: &str| PlanFire { persona: persona.into(), tz: "Europe/Stockholm".into(), occurrence: Utc.with_ymd_and_hms(2026, 10, 27, 21, 0, 0).unwrap() };
    for (persona, tz) in [("ghost", "Europe/Stockholm"), ("../aberg", "Europe/Stockholm"), ("aberg", "Mars/Base")] {
        let e = plan(&world, &shared, &src, &never, &PlanFire { tz: tz.into(), ..fire(persona) }).unwrap_err();
        assert!(is_final(&e), "{e:#}");
    }
    // another process holds the planner lock: retry later, not final
    std::fs::create_dir_all(world.root.join("locks")).unwrap();
    let held = File::create(world.root.join("locks").join("nightly.lock")).unwrap();
    held.try_lock().unwrap();
    let busy = |_: &Persona, _: NaiveDate, s: &Shared| s.hold("", bail_never);
    let e = plan(&world, &shared, &src, &busy, &fire("aberg")).unwrap_err();
    assert!(!is_final(&e) && e.to_string().contains("retrying"), "{e:#}");
    drop(held);
    let ok = plan(&world, &shared, &src, &fixed(vec![item("09:00", "ifrs 16 lease accounting")]), &fire("aberg")).unwrap();
    assert!(ok.generated && ok.date == DAY);
    // a plan file that cannot be kept is final
    std::fs::write(&ok.path, "persona: lnord\ndate: '2026-10-28'\nitems: []\n").unwrap();
    assert!(is_final(&plan(&world, &shared, &src, &never, &fire("aberg")).unwrap_err()));
}

fn bail_never() -> Result<Generated> {
    panic!("the lock is held elsewhere")
}
