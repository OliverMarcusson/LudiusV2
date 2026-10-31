use std::cell::RefCell;
use std::rc::Rc;

use super::*;
use crate::day::DayRunner;
use crate::day::tests::{Calls, Fake, d, note};
use crate::planner::{Person, templates};
use crate::runner::Clock;

#[derive(Default)]
struct Llm {
    items: Value,
    prompts: Vec<String>,
    systems: Vec<String>,
}

type Shared = Rc<RefCell<Llm>>;

fn tmp(name: &str) -> PathBuf {
    let log = crate::day::tests::log(name); // sets LUDIUS_DATA
    drop(log);
    let dir = std::env::temp_dir().join(format!("ludius-react-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn world(dir: &std::path::Path, people: &[(&str, &str)], channels: &[(&str, &[&str])]) -> PathBuf {
    let person = |u: &str, n: &str| Person { name: n.into(), role: "Clerk".into(), department: "Ops".into(), user: u.into(), manager: String::new() };
    let n = Narrative {
        company: "Acme".into(),
        city: "Delft".into(),
        summary: "s".into(),
        people: people.iter().map(|(u, n)| person(u, n)).collect(),
        channels: channels.iter().map(|(c, m)| (c.to_string(), m.iter().map(|u| u.to_string()).collect())).collect(),
        shares: Default::default(),
        projects: vec![],
        events: vec![],
        revised: String::new(),
        accounts: vec![],
        history: vec![],
    };
    let path = dir.join("narrative.yaml");
    std::fs::write(&path, serde_yaml::to_string(&n).unwrap()).unwrap();
    path
}

fn post(id: &str, sender: &str, message: &str, at: i64, channel: &str) -> Post {
    Post { id: id.into(), sender: sender.into(), message: message.into(), at, channel: channel.into() }
}

fn persona(name: &str, user: &str) -> Persona {
    serde_yaml::from_str(&format!("{{name: {name}, user: {user}}}")).unwrap()
}

fn reactor<'r>(who: (&str, &str), narrative: PathBuf, log: &'r GroundTruth, llm: &Shared) -> Reactor<'r> {
    let llm = llm.clone();
    Reactor {
        persona: persona(who.0, who.1),
        templates: templates().unwrap(),
        policy: Policy::default(),
        llm: Box::new(move |system, user| {
            let mut l = llm.borrow_mut();
            l.prompts.push(user.into());
            l.systems.push(system.into());
            Ok((serde_json::from_value(json!({"items": l.items}))?, 0.0))
        }),
        narrative,
        log,
        existing: Box::new(Vec::new),
        poll: Box::new(|_| Some(vec![])),
        talk: Box::new(|_, _| None),
        decider: None,
        taken: vec![],
    }
}

fn queue<T: Clone + 'static>(items: Vec<T>, then: T) -> impl Fn() -> T {
    let q = RefCell::new(std::collections::VecDeque::from(items));
    move || q.borrow_mut().pop_front().unwrap_or_else(|| then.clone())
}

fn runs(calls: &Calls) -> Vec<String> {
    calls.borrow().iter().filter(|c| c.starts_with("run")).cloned().collect()
}

fn records(log: &GroundTruth) -> Vec<Value> {
    std::fs::read_to_string(&log.path).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
}

fn slot(reactor: &mut Reactor, log: &GroundTruth, desk: &mut Fake) -> Vec<crate::day::ItemResult> {
    let mut day = DayRunner::new(templates().unwrap(), log, Clock::Compressed, 0);
    day.reactor = Some(reactor);
    day.slot(desk, d(), &note("10:00", false), &mut vec![], None).unwrap()
}

#[test]
fn chat_notification_reads_then_reacts_outside_the_plan() {
    let dom = crate::mail::domain();
    for fail in ["", "erp roles", "read", "read ui"] {
        let dir = tmp(&format!("notify{}", fail.replace(' ', "")));
        let narrative = world(&dir, &[("aberg", "Anna Berg"), ("lnord", "Lars Nord")], &[]);
        let p = post("p1", "lnord", "could you put the erp roles in a note and ping me", 100, "");
        let talk = vec![post("p0", "aberg", "morning lars", 50, ""), p.clone(), post("p2", "lnord", "the dock list too", 150, "")];
        let polls = queue(vec![vec![p.clone()], if fail == "read" { vec![p.clone()] } else { vec![] }], vec![]);
        let llm = Shared::default();
        llm.borrow_mut().items = json!([
            {"template": "notepad_write_note", "params": {"file_name": "erp roles", "body": "finance needs ap and ar"}},
            {"template": "chat_direct_message", "params": {"to": format!("lnord@{dom}"), "message": "done, see erp roles"}},
        ]);
        let log = GroundTruth::open(&dir.join("day.jsonl")).unwrap();
        let calls = Calls::default();
        let failing = match fail {
            "read" => vec![],
            "read ui" => vec!["chat_read_messages".to_string()],
            f => vec![f.to_string()],
        };
        let mut desk = Fake { calls: calls.clone(), fail: failing.into_iter().collect(), ..Default::default() };
        let mut r = reactor(("Anna Berg", "aberg"), narrative, &log, &llm);
        r.poll = Box::new(move |_| Some(polls()));
        r.talk = Box::new(move |_, other| (other == "lnord").then(|| (talk.clone(), 200)));
        let results = slot(&mut r, &log, &mut desk);
        let read = "run chat_read_messages";
        let want: Vec<&str> = match fail {
            "" | "read ui" => vec![read, "run erp roles", "run chat_direct_message", read, "run note 1000"],
            "erp roles" => vec![read, "run erp roles", read, "run note 1000"],
            _ => vec![read, read, "run note 1000"],
        };
        assert_eq!(runs(&calls), want, "{fail}");
        let l = llm.borrow();
        assert_eq!(l.prompts.len(), match fail { "erp roles" => 2, "read" => 0, _ => 1 }, "{fail}");
        let c = calls.borrow();
        let note_at = c.iter().position(|x| x == "run note 1000").unwrap();
        assert!(c.contains(&"normalize keep {\"msedge.exe\"}".to_string()) && !c[..note_at - 1].contains(&"normalize".to_string()), "{c:?}");
        if fail == "erp roles" {
            assert!(l.prompts[1].contains("FAILED: notepad_write_note") && !l.prompts[0].contains("You already tried"));
        }
        assert_eq!(fail != "read", l.prompts.iter().any(|p| p.contains("could you put the erp roles in a note")));
        if fail != "read" {
            assert!(l.prompts[0].contains("You: morning lars\n(new) Lars Nord: could you"));
            assert!(l.prompts[0].contains("(new) Lars Nord: the dock list too"));
        }
        drop(l);
        assert_eq!(results.iter().map(|r| r.item.at.as_str()).collect::<Vec<_>>(), ["10:00"]);
        let ends: Vec<Value> = records(&log).into_iter().filter(|e| e["event"] == "item_end").map(|e| e["template"].clone()).collect();
        assert_eq!(ends, [json!("notepad_write_note")]);

        llm.borrow_mut().items = json!([{"template": "chat_direct_message", "params": {"message": "hi"}, "content": {"to": format!("mvellinga@{dom}"), "to_name": "Marcus Vellinga"}}]);
        let people: BTreeMap<String, String> = [("aberg", "Anna Berg"), ("lnord", "Lars Nord"), ("mvellinga", "Marcus Vellinga")].map(|(u, n)| (u.to_string(), n.to_string())).into();
        assert!(r.plan("lnord", &people, &[p], "10:05", "2026-11-02", &[], "", &[]).unwrap().is_empty());
        let last = records(&log).pop().unwrap();
        assert_eq!(last["event"], "reaction_plan");
        assert_eq!(last["rejects"], json!([format!("chat_direct_message: to must be lnord@{dom}")]));
    }
}

#[test]
fn messages_sent_while_reading_or_answering_are_answered_too() {
    let dir = tmp("more");
    let narrative = world(&dir, &[("dkoster", "Daan Koster"), ("aberg", "Anna Berg")], &[]);
    let (hi, ask) = (post("p1", "aberg", "morning daan", 100, ""), post("p2", "aberg", "could you note the deliveries", 150, ""));
    let more = post("p3", "aberg", "and the carriers", 300, "");
    let polls = queue(vec![vec![hi.clone()], vec![ask.clone()]], vec![]);
    let talks = queue(vec![Some((vec![hi.clone(), ask.clone()], 200)), Some((vec![hi, ask, post("p9", "dkoster", "done", 250, ""), more], 400))], None);
    let llm = Shared::default();
    llm.borrow_mut().items = json!([{"template": "chat_direct_message", "params": {"to": format!("aberg@{}", crate::mail::domain()), "message": "ok"}}]);
    let log = GroundTruth::open(&dir.join("day.jsonl")).unwrap();
    let calls = Calls::default();
    let mut desk = Fake { calls: calls.clone(), ..Default::default() };
    let mut r = reactor(("Daan Koster", "dkoster"), narrative, &log, &llm);
    r.poll = Box::new(move |_| Some(polls()));
    r.talk = Box::new(move |_, _| talks());
    slot(&mut r, &log, &mut desk);
    let (read, dm) = ("run chat_read_messages", "run chat_direct_message");
    assert_eq!(runs(&calls), [read, read, dm, dm, read, "run note 1000"]);
    let l = llm.borrow();
    assert!(l.prompts[0].contains("(new) Anna Berg: morning daan\n(new) Anna Berg: could you"));
    assert!(l.prompts[1].contains("(new) Anna Berg: and the carriers") && !l.prompts[1].contains("(new) Anna Berg: could"));
}

#[test]
fn team_channel_posts_are_read_and_answered_in_the_channel() {
    let dir = tmp("channel");
    let people = [("aberg", "Anna Berg"), ("lnord", "Lars Nord"), ("nokafor", "Nia Okafor")];
    let narrative = world(&dir, &people, &[("Ops Team", &["aberg", "lnord", "nokafor"]), ("Board", &["lnord", "nokafor"])]);
    let p = post("p1", "lnord", "anna, could you send the dock list here", 100, "Ops Team");
    let other = post("p2", "nokafor", "board only", 120, "Board");
    let polls = queue(vec![vec![p.clone(), other.clone()], vec![other.clone()]], vec![other]);
    let llm = Shared::default();
    llm.borrow_mut().items = json!([{"template": "chat_post_message", "params": {"channel": "Ops Team", "message": "on it"}}]);
    let log = GroundTruth::open(&dir.join("day.jsonl")).unwrap();
    let calls = Calls::default();
    let mut desk = Fake { calls: calls.clone(), ..Default::default() };
    let seen = Rc::new(RefCell::new(BTreeSet::new()));
    let mut r = reactor(("Anna Berg", "aberg"), narrative, &log, &llm);
    r.poll = Box::new(move |_| Some(polls()));
    let (s, shown) = (seen.clone(), p.clone());
    r.talk = Box::new(move |_, key| {
        s.borrow_mut().insert(key.to_string());
        Some((vec![shown.clone()], 200))
    });
    slot(&mut r, &log, &mut desk);
    let read = "run chat_read_channel";
    assert_eq!(runs(&calls), [read, "run chat_post_message", read, "run note 1000"]);
    assert_eq!(*seen.borrow(), BTreeSet::from(["~Ops Team".to_string()]));
    assert_eq!(llm.borrow().prompts.len(), 1);
    assert!(llm.borrow().prompts[0].contains("Your team channel Ops Team (the others in it: Lars Nord (lnord@"));
    assert!(!llm.borrow().prompts[0].contains("board only"));
    let world: BTreeMap<String, String> = [("aberg", "Anna Berg"), ("lnord", "Lars Nord"), ("nokafor", "Nia Okafor"), ("mvellinga", "Marcus Vellinga")].map(|(u, n)| (u.to_string(), n.to_string())).into();
    let members = ["lnord".to_string(), "nokafor".to_string()];
    for bad in ["Town Square", "Board"] {
        llm.borrow_mut().items = json!([{"template": "chat_post_message", "params": {"channel": bad, "message": "hi"}}]);
        assert!(r.plan("~Ops Team", &world, &[p.clone()], "10:05", "2026-11-02", &[], "", &members).unwrap().is_empty());
        let rejects = records(&log).pop().unwrap()["rejects"].to_string();
        assert!(rejects.contains("channel must be Ops Team"), "{rejects}");
    }
    llm.borrow_mut().items = json!([{"template": "chat_direct_message", "params": {"to": format!("mvellinga@{}", crate::mail::domain()), "message": "hi"}}]);
    assert!(r.plan("~Ops Team", &world, &[p], "10:05", "2026-11-02", &[], "", &members).unwrap().is_empty());
    // a capped channel counts by ~channel
    log.write(json!({"event": "chat_notification", "channel": "Ops Team", "posts": ["p1"]})).unwrap();
    assert_eq!(r.counts().unwrap().0["~Ops Team"], 2);
}

fn decider<'r>(yes: Option<f64>, asked: &Rc<RefCell<Vec<(String, Vec<String>)>>>) -> Ask<'r> {
    let asked = asked.clone();
    Box::new(move |state, questions| {
        let keys: Vec<String> = questions.as_object().unwrap().keys().cloned().collect();
        asked.borrow_mut().push((state["conversation"].as_str().unwrap().into(), keys.clone()));
        let Some(yes) = yes else { anyhow::bail!("down") };
        let answers: serde_json::Map<String, Value> = keys
            .into_iter()
            .map(|k| {
                let a = match k.as_str() {
                    "work" => json!({"type": "choice", "choice": "chat", "probabilities": {"chat": 1.0}}),
                    "gossip" => json!({"type": "noul", "noul": yes}),
                    _ => json!({"type": "noul", "noul": 0.1}),
                };
                (k, a)
            })
            .collect();
        Ok(Answers { answers: Value::Object(answers), cost: None })
    })
}

#[test]
fn gossip_gate_lets_one_dm_pass_news_on_to_a_third_colleague() {
    let dom = crate::mail::domain();
    let people: BTreeMap<String, String> = [("dkoster", "Daan Koster"), ("aberg", "Anna Berg"), ("lnord", "Lars Nord")].map(|(u, n)| (u.to_string(), n.to_string())).into();
    for yes in [Some(0.95), Some(0.3), None] {
        let dir = tmp(&format!("gossip{}", yes.map_or(0, |y| (y * 100.0) as i64)));
        let news = post("p1", "aberg", "the coffee machine on 3 finally works", 100, "");
        let llm = Shared::default();
        llm.borrow_mut().items = json!([
            {"template": "chat_direct_message", "params": {"to": format!("aberg@{dom}"), "message": "nice"}},
            {"template": "chat_direct_message", "params": {"to": format!("lnord@{dom}"), "message": "coffee works on 3"}},
        ]);
        let asked = Rc::new(RefCell::new(vec![]));
        let log = GroundTruth::open(&dir.join("day.jsonl")).unwrap();
        let mut r = reactor(("Daan Koster", "dkoster"), dir.join("narrative.yaml"), &log, &llm);
        r.decider = Some(decider(yes, &asked));
        let items = r.plan("aberg", &people, std::slice::from_ref(&news), "10:00", "2026-11-02", &[], "", &[]).unwrap();
        let qs: Vec<String> = ["request", "question", "offer", "small_talk", "work", "gossip"].map(String::from).into();
        assert_eq!(*asked.borrow(), [("(new) Anna Berg: the coffee machine on 3 finally works".to_string(), qs)]);
        let l = llm.borrow();
        assert_eq!(l.systems[0].contains(RULES[0].1), yes.is_none());
        assert_eq!(l.prompts[0].contains("- notepad_write_note:"), yes.is_none(), "{}", l.prompts[0]);
        assert_eq!(items.len(), if yes == Some(0.95) { 2 } else { 0 }, "{yes:?}");
        assert_eq!(l.prompts[0].contains(&format!("Lars Nord (lnord@{dom})")), yes == Some(0.95));
        drop(l);
        let mut more = llm.borrow().items.as_array().unwrap().clone();
        more.push(json!({"template": "chat_direct_message", "params": {"to": format!("lnord@{dom}"), "message": "and again"}}));
        llm.borrow_mut().items = Value::Array(more);
        assert!(r.plan("aberg", &people, &[news], "10:01", "2026-11-02", &[], "", &[]).unwrap().is_empty());
        let last: Vec<String> = asked.borrow().iter().map(|(_, q)| q.last().unwrap().clone()).collect();
        assert_eq!(last, ["gossip", if yes == Some(0.95) { "work" } else { "gossip" }]);
    }
}

#[test]
fn received_gossip_skips_the_gate_so_news_never_chains() {
    let dom = crate::mail::domain();
    let dir = tmp("received");
    std::fs::write(dir.join("gossip.jsonl"), json!({"from": "aberg", "to": "dkoster", "message": "the coffee machine  on 3 finally works"}).to_string() + "\n").unwrap();
    let people: BTreeMap<String, String> = [("dkoster", "Daan Koster"), ("aberg", "Anna Berg"), ("lnord", "Lars Nord")].map(|(u, n)| (u.to_string(), n.to_string())).into();
    let news = post("p1", "aberg", "the coffee machine on 3 finally works", 100, "");
    let llm = Shared::default();
    llm.borrow_mut().items = json!([{"template": "chat_direct_message", "params": {"to": format!("lnord@{dom}"), "message": "coffee works on 3"}}]);
    let asked = Rc::new(RefCell::new(vec![]));
    let log = GroundTruth::open(&dir.join("day.jsonl")).unwrap();
    let mut r = reactor(("Daan Koster", "dkoster"), dir.join("narrative.yaml"), &log, &llm);
    r.decider = Some(decider(Some(0.99), &asked));
    assert!(r.plan("aberg", &people, std::slice::from_ref(&news), "10:00", "2026-11-02", &[], "", &[]).unwrap().is_empty());
    assert_eq!(asked.borrow()[0].1.last().unwrap(), "work");
    assert!(!llm.borrow().prompts[0].contains(&format!("lnord@{dom}")));
    assert!(records(&log).iter().any(|r| r["skipped"] == "received gossip"));
    std::fs::write(dir.join("gossip.jsonl"), json!({"from": null, "to": "dkoster", "message": "x"}).to_string() + "\n").unwrap();
    assert!(r.heard_gossip(&[news]));
}
