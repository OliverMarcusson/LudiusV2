use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;

use ludius_proto::v1::Rect;

use super::*;
use crate::decisions::{Decision, Options};
use crate::options::Opt;

#[derive(Default)]
struct Fake {
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
    let data = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../controller/data");
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
