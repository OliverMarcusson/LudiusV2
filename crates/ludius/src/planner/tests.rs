use super::*;
use std::path::Path;

fn setup() -> std::path::PathBuf {
    // SAFETY: tests only read LUDIUS_DATA and LUDIUS_APPS (the apps as in controller/tests/conftest.py), always set to the same values.
    unsafe {
        std::env::set_var("LUDIUS_DATA", Path::new(env!("CARGO_MANIFEST_DIR")).join("../../controller/data"));
        std::env::set_var("LUDIUS_APPS", "edge,excel,explorer,notepad,outlook,paint,powerpoint,powershell,wordpad");
    }
    let dir = std::env::temp_dir().join(format!("ludius-planner-{}-{:?}", std::process::id(), std::thread::current().id()));
    let _ = std::fs::remove_dir_all(&dir);
    dir
}

fn item(template: &str, at: &str, params: &[(&str, &str)]) -> PlanItem {
    PlanItem { template: template.into(), at: at.into(), params: params.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(), ..Default::default() }
}

fn plan(items: Vec<PlanItem>) -> DayPlan {
    DayPlan { persona: "aberg".into(), date: "2026-10-28".into(), items }
}

#[test]
fn new_plan_is_published_once_and_never_replaced() {
    let root = setup();
    let world = World::new(&root);
    let p = plan(vec![item("notepad_write_note", "09:00", &[("file_name", "standup notes"), ("body", "one\r\ntwo")])]);
    let path = world.save_plan(&p, &templates().unwrap(), true).unwrap();
    let mut back = load_plan(&path, false).unwrap();
    assert_eq!(back.items[0].params["body"], "one\ntwo");
    back.items[0].params["body"] = "one\r\ntwo".into();
    assert_eq!(back, p);
    let before = std::fs::read(&path).unwrap();
    assert!(world.save_plan(&p, &templates().unwrap(), true).unwrap_err().to_string().contains("already exists"));
    assert_eq!(std::fs::read(&path).unwrap(), before);
    assert!(std::fs::read_dir(path.parent().unwrap()).unwrap().all(|e| !e.unwrap().file_name().to_string_lossy().ends_with(".tmp")));
    // the journal holds the content once; a repair adds nothing
    assert_eq!(world.history("aberg").unwrap(), vec!["standup notes".to_string()]);
    assert_eq!(world.repair_history(&p, &templates().unwrap()).unwrap(), 0);
    std::fs::write(&world.history_path, "{\"date\": 1}\n").unwrap();
    assert!(world.history("aberg").is_err(), "a malformed history row is an error, never skipped");
}

#[test]
fn duplicate_slots_are_refused_or_dropped_for_history() {
    let root = setup();
    let p = plan(vec![item("break", "10:00", &[]), item("break", "10:00", &[])]);
    let path = root.join("plans").join("2026-10-28-aberg.yaml");
    dump(&path, &p, false).unwrap();
    assert!(load_plan(&path, false).unwrap_err().to_string().contains("share a time and template"));
    assert!(load_plan(&path, true).unwrap().items.is_empty());
}

#[test]
fn owned_files_follow_done_items_only() {
    let root = setup();
    let world = World::new(&root);
    let day1 = DayPlan { date: "2026-10-20".into(), ..plan(vec![item("notepad_write_note", "09:00", &[("file_name", "Kept"), ("body", "x")]), item("notepad_write_note", "10:00", &[("file_name", "never saved"), ("body", "x")])]) };
    dump(&world.plan_path("aberg", &day1.date), &day1, false).unwrap();
    let log = world.day_log("aberg", &day1.date);
    std::fs::create_dir_all(log.parent().unwrap()).unwrap();
    std::fs::write(&log, "{\"event\":\"item_end\",\"at\":\"09:00\",\"template\":\"notepad_write_note\",\"status\":\"done\"}\nnot json\n{\"event\":\"item_end\",\"at\":\"10:00\",\"template\":\"notepad_write_note\",\"status\":\"abandoned\"}\n").unwrap();
    let owned = owned_files(&root, "aberg", "2026-10-28").unwrap();
    assert_eq!(owned.into_iter().collect::<Vec<_>>(), vec![(r"documents\kept.txt".to_string(), "2026-10-20".to_string())]);
    assert!(owned_files(&root, "aberg", "2026-10-20").unwrap().is_empty(), "only days before `before`");

    // recycling: own file, untouched for RECYCLE_AGE_DAYS
    let ts = templates().unwrap();
    let t = &ts["explorer_recycle_file"];
    let owned = owned_files(&root, "aberg", "2026-10-28").unwrap();
    let rec = item("explorer_recycle_file", "11:00", &[("file_name", "kept"), ("file_ext", "txt")]);
    assert_eq!(owned_problem(&rec, t, &owned, "2026-10-28", &[]), None);
    assert!(owned_problem(&rec, t, &owned, "2026-10-22", &[]).unwrap().contains("untouched"));
    let other = item("explorer_recycle_file", "11:00", &[("file_name", "someone else"), ("file_ext", "txt")]);
    assert!(owned_problem(&other, t, &owned, "2026-10-28", &[]).unwrap().contains("own files"));
}

#[test]
fn item_checks() {
    setup();
    let ts = templates().unwrap();
    let policy = crate::policy::Policy::default();
    let problem = |it: &PlanItem| item_problem(it, &ts, &policy, "aberg", &[], &[]).unwrap();
    assert_eq!(problem(&item("notepad_write_note", "09:00", &[("file_name", "standup notes"), ("body", "first\nsecond")])), None);
    assert!(problem(&item("notepad_write_note", "09:00", &[("file_name", "a:b"), ("body", "x")])).unwrap().contains("valid file name"));
    assert!(problem(&item("notepad_write_note", "09:00", &[("file_name", "CON"), ("body", "x")])).unwrap().contains("valid file name"));
    assert!(problem(&item("notepad_write_note", "09:00", &[("file_name", "caf\u{e9}"), ("body", "x")])).unwrap().contains("non-ASCII"));
    assert!(problem(&item("nope", "09:00", &[])).unwrap().contains("unknown template"));
    assert!(problem(&item("break", "09:00", &[])).unwrap().contains("1-120"));
    assert!(problem(&item("notepad_write_note", "09:00", &[("file_name", "x")])).is_some(), "missing body");

    assert_eq!(check::grid("Item\tQty\nApples\t3"), None);
    assert!(check::grid("Apples\nApp").unwrap().contains("complete"));
    assert!(check::grid("1.5").is_some());
    assert_eq!(check::office_text("Budget review done", 10), None);
    assert!(check::office_text("1. first point", 10).is_some());
    assert!(check::office_text("Meet on Tues", 10).unwrap().contains("complete"));

    let m = |date: &str, start: &str, end: &str| {
        check::meeting(&[("location", "Room 2"), ("date", date), ("start", start), ("end", end)].iter().map(|(k, v)| (k.to_string(), v.to_string())).collect())
    };
    assert_eq!(m("2026-10-29", "10:00", "11:00"), None);
    assert!(m("2026-10-31", "10:00", "11:00").unwrap().contains("weekday"));
    assert!(m("2026-10-29", "16:30", "17:30").is_some());
    let mut inv = item("x", "09:00", &[("date", "2026-10-28")]);
    assert!(check::meeting_day(&inv, "2026-10-28").is_some());
    inv.params["date"] = "2026-11-11".into();
    assert_eq!(check::meeting_day(&inv, "2026-10-28"), None);
    inv.params["date"] = "2026-11-12".into();
    assert!(check::meeting_day(&inv, "2026-10-28").is_some());

    assert_eq!(taken_names([r"Documents\Report.final.docx "], true), ["report.final", "report.final.docx"].map(String::from).into());
}


#[test]
fn near_duplicate_matches_difflib() {
    let ratio = |a: &str, b: &str| {
        let norm = |s: &str| re("[a-z0-9]+").find_iter(&s.to_lowercase()).map(|m| m.as_str()).collect::<Vec<_>>().join(" ").chars().collect::<Vec<_>>();
        let (a, b) = (norm(a), norm(b));
        2.0 * validate::matched(&a, &b) as f64 / (a.len() + b.len()) as f64
    };
    // difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() on the normalized strings
    for (a, b, want) in [
        ("Quarterly budget review notes for March", "quarterly budget review note for march!", 0.987012987012987),
        ("Send the Q3 numbers to Anna", "Send Q3 figures to Anna please", 0.6666666666666666),
        ("abcabcabcabc xyz", "xyzabc abcabcab", 0.5161290322580645),
        ("meeting about the new office move", "office move: new meeting about it", 0.46153846153846156),
    ] {
        assert!((ratio(a, b) - want).abs() < 1e-12, "{a:?} {b:?}: {} != {want}", ratio(a, b));
    }
    let earlier = vec!["Send Q3 figures to Anna please".to_string(), "quarterly budget review note for march!".to_string()];
    assert_eq!(near_duplicate("Quarterly budget review notes for March", &earlier), Some(&earlier[1]));
    assert_eq!(near_duplicate("Send the Q3 numbers to Anna", &earlier), None);
}

#[test]
fn day_slots_are_seeded_and_spaced() {
    setup();
    let persona: crate::tasks::Persona = crate::tasks::load_yaml(&crate::paths::data().join("personas").join("aberg.yaml")).unwrap();
    let person = |name: &str, user: &str| Person { name: name.into(), role: "Clerk".into(), department: "Finance".into(), user: user.into(), manager: String::new() };
    let narrative = Narrative {
        company: "Acme".into(),
        city: "Town".into(),
        summary: "s".into(),
        people: vec![person(&persona.name, &persona.user), person("Bo Ek", "bek")],
        channels: Default::default(),
        shares: Default::default(),
        projects: vec![],
        events: vec![],
        revised: String::new(),
        accounts: vec![persona.user.clone(), "bek".into()],
        history: vec![],
    };
    let d = Day {
        narrative: &narrative,
        persona: &persona,
        date: chrono::NaiveDate::from_ymd_opt(2026, 10, 28).unwrap(),
        world: String::new(),
        history: vec![],
        policy: Default::default(),
        scenario: None,
        followups: Default::default(),
        existing: vec![],
        mail_threads: Default::default(),
        inbox: Default::default(),
        owned: Default::default(),
        invites: Default::default(),
        unread: Default::default(),
        taken: vec![],
        attached: Default::default(),
    };
    let ts = role_templates(&templates().unwrap(), &persona);
    let slots = || make::day_slots(&d, &ts, vec!["general".into()], &Default::default(), &Default::default(), &[]).unwrap();
    let items = slots();
    assert_eq!(items, slots(), "same persona and day, same slots");
    let lunch: Vec<_> = items.iter().filter(|it| it.template == BREAK).collect();
    assert_eq!(lunch.len(), 1);
    let work: Vec<i64> = items.iter().filter(|it| it.template != BREAK).map(|it| minutes(&it.at)).collect();
    assert!((5..=8).contains(&work.len()), "{items:?}");
    assert!(work.windows(2).all(|w| w[1] - w[0] >= 15), "{items:?}");
    assert!(work.iter().all(|m| *m >= minutes(&persona.hours.0) + 10 && *m < minutes(&persona.hours.1) - 30));
    for it in items.iter().filter(|it| it.params.contains_key("to")) {
        assert_eq!(it.params["to"], format!("bek@{}", crate::mail::domain()));
    }
}

#[test]
fn validate_rejects_collisions_and_repeats() {
    setup();
    let ctx = Context { templates: templates().unwrap(), existing: vec![r"Documents\Budget.xlsx".into()], history: vec!["old weekly summary of sales".into()], ..Default::default() };
    let note = |at: &str, name: &str, body: &str| item("notepad_write_note", at, &[("file_name", name), ("body", body)]);
    let items = [
        note("09:00", "Team lunch plan", "x"),
        note("10:00", "team lunch plan", "y"),
        note("11:00", "budget", "z"),
        note("12:00", "Old weekly summary of sales", "w"),
        note("09:00", "Another note", "v"),
    ];
    let got: Vec<usize> = validate(&items, &ctx).unwrap().into_iter().map(|(i, _)| i).collect();
    assert_eq!(got, vec![1, 2, 3, 4]);
}
