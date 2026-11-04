use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;

use ludius_proto::v1::Rect;

use super::*;
use crate::decisions::{Decision, Options};
use crate::options::Opt;

#[derive(Default)]
struct Fake {
    snapshot: RefCell<Option<Snapshot>>,
    size: Cell<(i32, i32)>,
    pos: Cell<(i32, i32)>,
    offset: Cell<i32>,
    chords: RefCell<Vec<Vec<u32>>>,
}

impl Agent for Fake {
    fn vm(&self) -> &str {
        "t"
    }
    fn move_to(&self, x: i32, y: i32, _: u32) -> Result<i64> {
        self.pos.set((x + self.offset.get(), y));
        Ok(0)
    }
    fn click(&self, _: u32) -> Result<i64> {
        Ok(0)
    }
    fn chord(&self, vks: Vec<u32>) -> Result<i64> {
        self.chords.borrow_mut().push(vks);
        Ok(0)
    }
    fn type_text(&self, _: &str, _: u32, _: u32) -> Result<i64> {
        Ok(0)
    }
    fn scroll(&self, _: i32) -> Result<i64> {
        Ok(0)
    }
    fn ctrl_alt_del(&self) -> Result<i64> {
        Ok(0)
    }
    fn observe(&self, _: bool, _: u32, _: u32) -> Result<Snapshot> {
        if let Some(s) = &*self.snapshot.borrow() {
            return Ok(s.clone());
        }
        let ((w, h), (x, y)) = (self.size.get(), self.pos.get());
        Ok(Snapshot { screen_width: w, screen_height: h, dpi: 96, cursor_x: x, cursor_y: y, ..Default::default() })
    }
    fn screenshot(&self) -> Result<Vec<u8>> {
        Ok(vec![])
    }
}

struct NoDecider;

impl Decider for NoDecider {
    fn decide(&self, _: &Value, _: &Options, _: bool, _: Option<&[u8]>) -> Result<Decision> {
        bail!("no decisions in this test")
    }
}

fn runner<'a>(agent: &'a Fake, log: &'a GroundTruth) -> Runner<'a> {
    let persona: Persona = serde_yaml::from_str("{name: t, user: t}").unwrap();
    Runner::new(agent, &NoDecider, persona, log, Clock::Compressed, Policy::default(), Some(1), None, None)
}

fn log() -> GroundTruth {
    GroundTruth::open(&std::env::temp_dir().join(format!("ludius-runner-{}-{:?}.jsonl", std::process::id(), std::thread::current().id()))).unwrap()
}

#[test]
fn background_shell_clock_is_not_task_progress() {
    use ludius_proto::v1::{Window, window::Kind};
    let mut snap = Snapshot {
        windows: vec![Window { title: "Inbox - Outlook".into(), kind: Kind::Foreground as i32, ..Default::default() }, Window { kind: Kind::Shell as i32, ..Default::default() }],
        elements: vec![Element { key: "mail".into(), window: 0, ..Default::default() }, Element { key: "clock-14:30".into(), window: 1, ..Default::default() }],
        ..Default::default()
    };
    let before = fingerprint(&snap);
    snap.elements[1].key = "clock-14:31".into();
    assert_eq!(before, fingerprint(&snap));
    snap.elements[0].selected = true;
    assert_ne!(before, fingerprint(&snap));
    // The shell is relevant when it is the foreground, including Start search.
    snap.windows[0].kind = Kind::Shell as i32;
    snap.windows[1].kind = Kind::Foreground as i32;
    let before = fingerprint(&snap);
    snap.elements[1].value = "outlook".into();
    assert_ne!(before, fingerprint(&snap));
}

#[test]
fn unreadable_launch_does_not_pass_on_its_title_or_ask_for_blind_actions() {
    use ludius_proto::v1::{Window, window::Kind};
    let (agent, log) = (Fake::default(), log());
    *agent.snapshot.borrow_mut() = Some(Snapshot {
        windows: vec![Window { title: "Inbox - Outlook".into(), process_name: "OUTLOOK.EXE".into(), kind: Kind::Foreground as i32, no_tree: true, incomplete: true, read_error: "BuildUpdatedCache: E_FAIL".into(), ..Default::default() }],
        ..Default::default()
    });
    let data = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    let t = crate::tasks::load_template(&data.join("templates/outlook_read_mail.yaml")).unwrap();
    let inst = crate::tasks::TaskInstance { template: t.task.clone(), params: t.example.clone(), variant: Some(0), ..Default::default() };
    let mut task = Task::new(t, inst, &mut StdRng::seed_from_u64(0)).unwrap();
    let sg = task.subgoal("open_outlook").unwrap();
    let result = runner(&agent, &log).subgoal_steps(&mut task, "open_outlook", &sg, 8, Instant::now(), true, None).unwrap();
    assert!(!result.0);
    assert_eq!(result.1, 0);
    assert!(result.2.starts_with("foreground unreadable:"));
    assert!(agent.chords.borrow().is_empty());
}

#[test]
fn mapping_recheck_on_display_change() {
    let (agent, log) = (Fake::default(), log());
    agent.size.set((1920, 1080));
    let mut r = runner(&agent, &log);
    r.observe(5000).unwrap();
    assert!(r.mapping_ok);
    agent.size.set((1280, 720));
    r.observe(5000).unwrap();
    assert!(r.mapping_ok);
    agent.size.set((1600, 900));
    agent.offset.set(7);
    r.observe(5000).unwrap();
    assert!(!r.mapping_ok);
}

#[test]
fn document_click_keeps_the_caret_at_the_end() {
    let (agent, log) = (Fake::default(), log());
    let mut r = runner(&agent, &log);
    let data = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    unsafe { std::env::set_var("LUDIUS_DATA", &data) };
    let t = crate::tasks::load_template(&data.join("templates/notepad_write_note.yaml")).unwrap();
    let inst = crate::tasks::TaskInstance { template: t.task.clone(), params: t.example.clone(), variant: Some(0), ..Default::default() };
    let task = Task::new(t, inst, &mut StdRng::seed_from_u64(0)).unwrap();
    let b = Some(Rect { x: 0, y: 0, width: 800, height: 600 });
    let doc = Element { key: "d".into(), role: "document".into(), value: "heading\r".into(), bounds: b, ..Default::default() };
    let web = Element { key: "w".into(), role: "document".into(), value: "https://intranet/".into(), read_only: true, bounds: b, ..Default::default() };
    let button = Element { key: "b".into(), role: "button".into(), bounds: b, ..Default::default() };
    for (e, expected) in [(doc, vec![crate::keys::chord("Ctrl+End").unwrap()]), (web, vec![]), (button, vec![])] {
        agent.chords.borrow_mut().clear();
        let o = Opt { key: "o1".into(), kind: "click".into(), text: "click".into(), element: Some(e.clone()), ..Default::default() };
        r.act(&o, &task, None).unwrap();
        assert_eq!(*agent.chords.borrow(), expected, "{}", e.role);
    }
}

#[test]
fn near_tie_only_between_equivalent_options() {
    let (agent, log) = (Fake::default(), log());
    let mut r = runner(&agent, &log);
    let el = |i: u32, name: &str| Some(Element { id: i, key: format!("k{i}"), role: "button".into(), name: name.into(), ..Default::default() });
    let o = |k: &str, e| Opt { key: k.into(), kind: "click".into(), text: k.into(), element: e, ..Default::default() };
    let opts = vec![o("o1", el(1, "Save")), o("o2", el(2, "&Save")), o("o3", el(3, "Cancel"))];
    let d = Decision {
        choice: "o1".into(),
        confidence: 0.5,
        probabilities: BTreeMap::from([("o1".into(), 0.45), ("o2".into(), 0.42), ("o3".into(), 0.44)]),
        ..Default::default()
    };
    let picks: std::collections::BTreeSet<String> = (0..40).map(|_| r.choose(&d, &opts).unwrap().key).collect();
    assert_eq!(picks, ["o1".to_string(), "o2".to_string()].into());
}

#[test]
fn reply_titles_and_a_closed_app() {
    use ludius_proto::v1::Window;
    let data = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    unsafe { std::env::set_var("LUDIUS_DATA", &data) };
    let t = crate::tasks::load_template(&data.join("templates/outlook_read_mail.yaml")).unwrap();
    let mut inst = crate::tasks::TaskInstance { template: t.task.clone(), params: t.example.clone(), variant: Some(0), ..Default::default() };
    inst.params.insert("subject".into(), "packaging receipts".into());
    let task = Task::new(t, inst, &mut StdRng::seed_from_u64(0)).unwrap();
    // The server's subject is normalized; Outlook's title keeps the reply prefix.
    let check = task.template.subgoals["open_message_click"].done.as_ref().unwrap().window.clone().unwrap();
    let w = |title: &str, p: &str| Window { title: title.into(), process_name: p.into(), ..Default::default() };
    for (title, ok) in [("RE: packaging receipts - Message (HTML) ", true), ("packaging receipts - Message", true), ("RE: other packaging receipts - Message", false)] {
        assert_eq!(crate::tasks::window_matches(&w(title, "OUTLOOK.EXE"), &check, &task).unwrap(), ok, "{title}");
    }
    let snap = |ws: Vec<Window>| Snapshot { windows: ws, ..Default::default() };
    assert!(steps::app_open(&snap(vec![w("Inbox - Outlook", "OUTLOOK.EXE")]), &task));
    assert!(!steps::app_open(&snap(vec![w("Program Manager", "explorer.exe")]), &task));
    assert!(steps::app_open(&snap(vec![]), &task), "an unreadable screen is not a closed app");
}

#[test]
fn an_original_titled_re_is_not_the_reply() {
    use ludius_proto::v1::Window;
    let data = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    unsafe { std::env::set_var("LUDIUS_DATA", &data) };
    let t = crate::tasks::load_template(&data.join("templates/outlook_reply_mail.yaml")).unwrap();
    let mut inst = crate::tasks::TaskInstance { template: t.task.clone(), params: t.example.clone(), variant: Some(0), ..Default::default() };
    inst.params.insert("subject".into(), "packaging receipts".into());
    let mut task = Task::new(t, inst, &mut StdRng::seed_from_u64(0)).unwrap();
    let done = task.template.subgoals["reply_shortcut"].done.clone();
    let win = Window { title: "RE: packaging receipts - Message (HTML) ".into(), process_name: "OUTLOOK.EXE".into(), ..Default::default() };
    let mut snap = Snapshot { windows: vec![win], ..Default::default() };
    let check = |snap: &Snapshot, task: &mut Task| crate::tasks::check_done(done.as_ref(), snap, task, false, false, false, false, None).unwrap();
    assert_eq!(check(&snap, &mut task), Some(false), "the read window of a RE: mail");
    snap.elements.push(Element { role: "button".into(), name: "Send".into(), ..Default::default() });
    assert_eq!(check(&snap, &mut task), Some(true), "the compose window");
}

#[test]
fn a_commit_is_an_allowlisted_click_or_the_subgoals_key() {
    let data = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../data");
    unsafe { std::env::set_var("LUDIUS_DATA", &data) };
    let file = crate::tasks::load_template(&data.join("templates/outlook_file_mail.yaml")).unwrap();
    let invite = crate::tasks::load_template(&data.join("templates/outlook_send_invite.yaml")).unwrap();
    let click = |name: &str| Opt { kind: "click".into(), element: Some(Element { name: name.into(), ..Default::default() }), ..Default::default() };
    let key = |k: &str| Opt { kind: "key".into(), keys: k.into(), ..Default::default() };
    let (mv, send) = (&file.subgoals["move"], &invite.subgoals["send_shortcut"]);
    assert!(steps::commits(&click("OK"), mv));
    assert!(!steps::commits(&click("Cancel"), mv));
    assert!(steps::commits(&key(&send.keys[0]), send));
    assert!(!steps::commits(&key("Esc"), send));
    assert!(!steps::commits(&key(&send.keys[0]), &invite.subgoals["write_body"]), "no allowlist, no commit");
    let win = |h: i64| ludius_proto::v1::Window { handle: h, ..Default::default() };
    let snap = |hs: &[i64]| Snapshot { windows: hs.iter().map(|h| win(*h)).collect(), ..Default::default() };
    assert!(steps::closed(Some(7), &snap(&[3])), "the dialog in front closed");
    assert!(!steps::closed(Some(7), &snap(&[7, 9])), "Send to opened a submenu, Explorer still there");
    assert!(!steps::closed(Some(7), &snap(&[])), "unreadable is not closed");
}
