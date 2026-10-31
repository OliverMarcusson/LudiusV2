use std::cell::RefCell;

use super::*;
use crate::requests::*;
use crate::decisions::{Decision, Options};
use crate::planner::PlanItem;
use chrono::Duration;
use serde_json::{Value, json};

const BODY: &str = "Hi Anna,\ncould you put the lund accruals in a note \"lund accruals q4\" with the totals?\nThanks, Marcus";

fn story() -> Narrative {
    let p = |name: &str, role: &str, dep: &str, user: &str, manager: &str| json!({"name": name, "role": role, "department": dep, "user": user, "manager": manager});
    serde_json::from_value(json!({
        "company": "Harbourline", "city": "Rotterdam", "summary": "s", "projects": ["q4 close"], "events": [],
        "people": [p("Marcus Vellinga", "Managing director", "Executive", "mvellinga", ""), p("Anna Berg", "Finance controller", "Finance", "aberg", "mvellinga"),
                   p("Priya Nanda", "IT administrator", "IT", "pnanda", "mvellinga"), p("Lars Nord", "IT administrator", "IT", "lnord", "aberg")]
    }))
    .unwrap()
}

fn persona(name: &str, user: &str, role: &str, dep: &str) -> Persona {
    serde_json::from_value(json!({"name": name, "user": user, "role": role, "department": dep})).unwrap()
}

fn personas() -> Vec<Persona> {
    vec![persona("Anna Berg", "aberg", "Finance controller", "Finance"), persona("Lars Nord", "lnord", "IT administrator", "IT")]
}

fn ask() -> Value {
    json!({"to": "aberg", "task": "notepad_write_note", "subject": "lund accruals note", "body": BODY, "file_name": "lund accruals q4", "include": ["totals"]})
}

fn asks(xs: Vec<Value>) -> Asks {
    serde_json::from_value(json!({ "requests": xs })).unwrap()
}

fn thu() -> NaiveDate {
    NaiveDate::from_ymd_opt(2026, 10, 29).unwrap()
}

fn setup(name: &str) -> (World, BTreeMap<String, Template>) {
    crate::day::tests::log(name);
    let root = std::env::temp_dir().join(format!("ludius-requests-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    (World::new(&root), crate::planner::templates().unwrap())
}

type Sent = RefCell<Vec<Vec<String>>>;

fn run(world: &World, templates: &BTreeMap<String, Template>, answer: Option<Asks>, date: NaiveDate, inbox: Vec<Message>, policy: &Policy, sent: &Sent) -> Day {
    let mut answer = answer;
    let send = |a: &str, b: &str, c: &str, d: &str, e: &str, f: &str, g: &str| {
        sent.borrow_mut().push([a, b, c, d, e, f, g].map(String::from).to_vec());
        true
    };
    let n = story();
    let boss = n.people[0].clone();
    let briefs = BTreeMap::from([("aberg".to_string(), "- nothing".to_string())]);
    let inbox = BTreeMap::from([("aberg".to_string(), Some(inbox))]);
    let existing = BTreeMap::from([("aberg".to_string(), vec![])]);
    ensure(&mut |_| answer.take().ok_or_else(|| anyhow::anyhow!("no answer left")), &send, world, &n, &briefs, &personas(), templates, policy, date, &boss, &inbox, &existing).unwrap()
}

#[test]
fn org_chart_managers() {
    let n = story();
    assert_eq!(manager_of(&n, "aberg").unwrap(), n.people[0]);
    assert_eq!(manager_of(&n, "lnord").unwrap().user, "aberg");
    assert!(manager_of(&n, "mvellinga").unwrap_err().contains("no manager"));
}

#[test]
fn requests_are_validated_sent_once_and_capped() {
    let (w, ts) = setup("capped");
    let (sent, policy) = (Sent::default(), Policy::default());
    let with = |k: &str, v: Value| {
        let mut a = ask();
        a[k] = v;
        a
    };
    let bad = vec![with("to", "lnord".into()), with("body", BODY.replace('"', "").into()), with("include", json!(["budget"])), with("task", "edge_web_lookup".into()), with("subject", "RE: lund".into())];
    let day = run(&w, &ts, Some(asks([bad, vec![ask(), with("subject", "second today".into())]].concat())), thu(), vec![], &policy, &sent);
    assert_eq!(day.requests.iter().map(|r| (r.subject.as_str(), r.status.as_deref())).collect::<Vec<_>>(), [("lund accruals note", Some("sent"))]);
    assert_eq!(day.rejects.len(), 6, "{:?}", day.rejects);
    assert!(day.rejects[5].contains("not a report with requests left"));
    assert_eq!(sent.borrow()[0][..5], [addr("mvellinga"), "Marcus Vellinga".into(), addr("aberg"), "Anna Berg".into(), "lund accruals note".into()]);
    assert_eq!(run(&w, &ts, None, thu(), vec![], &policy, &sent).requests, day.requests);
    assert_eq!(sent.borrow().len(), 1);
    assert_eq!(run(&w, &ts, None, NaiveDate::from_ymd_opt(2026, 10, 31).unwrap(), vec![], &policy, &sent).rejects, ["no request: weekend"]);
    let dir = w.root.join("world").join("requests");
    let text = std::fs::read_to_string(dir.join("2026-10-29-mvellinga.yaml")).unwrap();
    for d in [27, 28] {
        std::fs::write(dir.join(format!("2026-10-{d}-mvellinga.yaml")), text.replace("10-29", &format!("10-{d}"))).unwrap();
    }
    assert_eq!(run(&w, &ts, None, NaiveDate::from_ymd_opt(2026, 10, 30).unwrap(), vec![], &policy, &sent).rejects, ["no request: weekly budget used"]);
    let dup = Message { sender: "x".into(), subject: "Lund accruals note".into(), ..Default::default() };
    let next = run(&w, &ts, Some(asks(vec![ask()])), thu() + Duration::days(7), vec![dup], &policy, &sent);
    assert!(next.rejects[0].contains("already in aberg's Inbox"), "{:?}", next.rejects);
    let a: Ask = serde_json::from_value(with("heading", "x".into())).unwrap();
    let reserved = Policy { reserved_paths: vec!["aberg@".into()], ..Default::default() };
    let all = BTreeMap::from([("aberg".to_string(), ts.clone())]);
    let why = problem(&a, &BTreeMap::from([("aberg".into(), 1)]), &all, &BTreeMap::new(), &story().people[0], &reserved, &BTreeMap::from([("aberg".into(), Some(BTreeSet::new()))]), &BTreeMap::from([("aberg".into(), vec![])]));
    assert_eq!(why.unwrap(), format!("{} is scenario-reserved", addr("aberg")));
}

#[test]
fn a_recorded_request_is_checked_against_todays_policy() {
    let (w, ts) = setup("policy");
    let n = story();
    let planned = plan(&mut |_| Ok(asks(vec![ask()])), &w, &n, &BTreeMap::new(), &personas(), &ts, &Policy::default(), thu(), &n.people[0], &BTreeMap::from([("aberg".into(), Some(vec![]))]), &BTreeMap::from([("aberg".into(), vec![])])).unwrap();
    dump(&dir(&w.root).join("2026-10-29-mvellinga.yaml"), &planned, true).unwrap();
    let sent = Sent::default();
    let reserved = Policy { reserved_paths: vec!["aberg@".into()], ..Default::default() };
    let day = run(&w, &ts, None, thu(), vec![], &reserved, &sent);
    assert!(sent.borrow().is_empty() && day.requests[0].status.as_deref() == Some("rejected"));
    assert!(day.rejects.last().unwrap().contains("scenario-reserved"));
}

#[test]
fn persona_maps_a_request_fail_closed() {
    let (w, ts) = setup("map");
    let n = story();
    let boss = n.people[0].clone();
    let msgid = run(&w, &ts, Some(asks(vec![ask()])), thu(), vec![], &Policy::default(), &Sent::default()).requests[0].msgid.clone();
    let at = NaiveDate::from_ymd_opt(2026, 10, 28).unwrap().and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp_millis();
    let m = Message { sender: addr("mvellinga"), delivery_ms: at, body: BODY.into(), cls: "IPM.Note".into(), subject: "lund accruals note".into(), msgid: msgid.clone(), ..Default::default() };
    let seed = Message { subject: "seeded".into(), msgid: "<s1@x>".into(), ..m.clone() };
    let anna = &personas()[0];
    assert_eq!(pending(&w, "aberg", &boss, Some(&[m.clone(), seed]), &Policy::default(), thu()).unwrap(), std::slice::from_ref(&m));
    let quiet = Policy { reserved_subjects: vec!["lund accruals note".into()], ..Default::default() };
    assert!(pending(&w, "aberg", &boss, Some(std::slice::from_ref(&m)), &quiet, thu()).unwrap().is_empty());
    let book = BTreeMap::from([("aberg".to_string(), "Anna Berg".to_string())]);
    let calls = RefCell::new(0);
    let mut llm = |_: &str| -> Result<Item> {
        *calls.borrow_mut() += 1;
        Ok(serde_json::from_value(json!({"at": "09:30", "params": {"file_name": "lund accruals q4", "body": "lund totals\n- 12 400"}, "context": "c"}))?)
    };
    let decider = |choice: &'static str, p: f64| {
        move |_: &Value, o: &Options| -> Result<Decision> {
            assert!(o.iter().any(|(k, _)| k == "none"));
            Ok(Decision { choice: choice.into(), probabilities: [(choice.to_string(), p)].into(), ..Default::default() })
        }
    };
    let (mut items, notes) = candidates(&mut llm, &mut decider("notepad_write_note", 0.9), &w, anna, &boss, std::slice::from_ref(&m), &ts, thu(), &book).unwrap();
    assert_eq!(items.iter().map(|i| (i.template.as_str(), i.request.as_str())).collect::<Vec<_>>(), [("notepad_write_note", msgid.as_str())]);
    assert!(notes.is_empty());
    assert_eq!(planned(&items[0], &ts, &w.root), None);
    let own = |at: &str, f: &str, b: &str| PlanItem { template: "notepad_write_note".into(), at: at.into(), params: [("file_name".to_string(), f.to_string()), ("body".to_string(), b.to_string())].into_iter().collect(), ..Default::default() };
    let mine = [own("09:00", "a", "Totals"), own("10:00", "b", "sums")];
    assert_eq!(duplicates(&mine, &items[0], &w.root).unwrap(), mine[..1]);
    items[0].params.insert("body".into(), "lund sums".into());
    assert!(planned(&items[0], &ts, &w.root).unwrap().starts_with("content lacks") && planned(&items[0], &ts, &w.root).unwrap().contains("totals"));
    items[0].params.insert("file_name".into(), "other".into());
    assert!(planned(&items[0], &ts, &w.root).unwrap().contains("lund accruals q4"));
    for (choice, p) in [("none", 0.9), ("notepad_write_note", 0.4)] {
        assert!(candidates(&mut llm, &mut decider(choice, p), &w, anna, &boss, std::slice::from_ref(&m), &ts, thu(), &book).unwrap().0.is_empty());
        assert!(pending(&w, "aberg", &boss, Some(std::slice::from_ref(&m)), &Policy::default(), thu()).unwrap().is_empty());
        std::fs::remove_file(dir(&w.root).join("seen-aberg.yaml")).unwrap();
    }
    assert_eq!(*calls.borrow(), 1);
    // the write's file goes back to the manager after its budget, read first before it
    let back = deliver(&items[0], &boss, Some(std::slice::from_ref(&m)), &ts, &[]).unwrap();
    assert_eq!((back.params["to"].as_str(), back.params["file_name"].as_str(), back.request.as_str()), (addr("mvellinga").as_str(), "other.txt", msgid.as_str()));
    let read = read_first(&items[0], Some(&[m]), &ts, &[own("09:25", "x", "y")]).unwrap();
    assert_eq!((read.at.as_str(), read.params["subject"].as_str()), ("09:24", "lund accruals note"));
}
