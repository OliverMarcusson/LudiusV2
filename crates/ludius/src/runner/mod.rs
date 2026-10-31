//! The step loop: one task on one persona's desktop, subgoal by subgoal (port of loop.py).
mod act;
mod steps;
mod wrapup;

use std::fs::File;
use std::io::Write;
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::{Result, bail};
use ludius_proto::v1::{Element, Snapshot};
use rand::rngs::StdRng;
use rand::{RngExt as _, SeedableRng};
use serde_json::{Map, Value, json};

use crate::assist::Assist;
use crate::calls::CapReached;
use crate::decisions::Decider;
use crate::hostagent::HostAgent;
use crate::policy::Policy;
use crate::reply::{Composer, prepare_answer, prepare_file, prepare_read};
use crate::requests;
use crate::tasks::{Done, Persona, Subgoal, Task, file_baseline, mail_baseline, pixels_window};

pub use wrapup::image_reason;

pub const STUCK: &str = "stuck";
pub const FRONT_MOVED: &str = "the foreground window changed before the action";
const MAX_DETOURS: u32 = 2;
const STUCK_WAITS: u32 = 6;
pub const DONE_THRESHOLD: f64 = 0.85;
const OFF_TRACK_THRESHOLD: f64 = 0.8;
const NEAR_TIE: f64 = 0.05;
const BLOCKED_WAIT: Duration = Duration::from_millis(1500);
const SLOW_KEY_WAIT: Duration = Duration::from_secs(4);
const WAIT_CAP_S: f64 = 90.0;
const HUNG_WAIT: Duration = Duration::from_secs(5);
pub const LAUNCH_REASON: &str = "the launch query goes into the Start search box only";
pub const REPLACE_REASON: &str = "replacing text needs a target field that holds the keyboard focus";

pub fn narrative() -> std::path::PathBuf {
    requests::root().join("world").join("narrative.yaml")
}

pub fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Clock {
    Realtime,
    Compressed,
    TimeJump,
}

impl std::str::FromStr for Clock {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self> {
        Ok(match s {
            "realtime" => Self::Realtime,
            "compressed" => Self::Compressed,
            "time-jump" => Self::TimeJump,
            _ => bail!("clock {s:?}: realtime, compressed or time-jump"),
        })
    }
}

/// The desktop a runner drives: the host agent, or a test double.
pub trait Agent {
    fn vm(&self) -> &str;
    fn move_to(&self, x: i32, y: i32, duration_ms: u32) -> Result<i64>;
    fn click(&self, count: u32) -> Result<i64>;
    fn chord(&self, vks: Vec<u32>) -> Result<i64>;
    fn type_text(&self, text: &str, mean_ms: u32, jitter_ms: u32) -> Result<i64>;
    fn scroll(&self, notches: i32) -> Result<i64>;
    fn ctrl_alt_del(&self) -> Result<i64>;
    fn observe(&self, wait_settle: bool, quiet_ms: u32, timeout_ms: u32) -> Result<Snapshot>;
    fn screenshot(&self) -> Result<Vec<u8>>;
}

impl Agent for HostAgent {
    fn vm(&self) -> &str {
        &self.vm
    }
    fn move_to(&self, x: i32, y: i32, duration_ms: u32) -> Result<i64> {
        HostAgent::move_to(self, x, y, duration_ms)
    }
    fn click(&self, count: u32) -> Result<i64> {
        HostAgent::click(self, ludius_proto::v1::click::Button::Left, count)
    }
    fn chord(&self, vks: Vec<u32>) -> Result<i64> {
        HostAgent::chord(self, vks)
    }
    fn type_text(&self, text: &str, mean_ms: u32, jitter_ms: u32) -> Result<i64> {
        HostAgent::type_text(self, text, mean_ms, jitter_ms)
    }
    fn scroll(&self, notches: i32) -> Result<i64> {
        HostAgent::scroll(self, notches)
    }
    fn ctrl_alt_del(&self) -> Result<i64> {
        HostAgent::ctrl_alt_del(self)
    }
    fn observe(&self, wait_settle: bool, quiet_ms: u32, timeout_ms: u32) -> Result<Snapshot> {
        Ok(HostAgent::observe(self, wait_settle, quiet_ms, timeout_ms)?.snapshot.unwrap_or_default())
    }
    fn screenshot(&self) -> Result<Vec<u8>> {
        HostAgent::screenshot(self)
    }
}

/// The ground-truth JSONL file: one record per line, flushed as written.
pub struct GroundTruth {
    f: Mutex<File>,
}

impl GroundTruth {
    pub fn open(path: &Path) -> Result<Self> {
        if let Some(p) = path.parent() {
            std::fs::create_dir_all(p)?;
        }
        Ok(Self { f: Mutex::new(std::fs::OpenOptions::new().create(true).append(true).open(path)?) })
    }

    pub fn write(&self, rec: Value) -> Result<()> {
        let mut f = self.f.lock().unwrap();
        writeln!(f, "{rec}")?;
        f.flush()?;
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Outcome {
    pub status: String,
    pub steps: i64,
    pub reason: String,
    pub subgoals_done: Vec<String>,
}

impl Outcome {
    fn new(status: &str, steps: i64, reason: impl Into<String>, done: &[String]) -> Self {
        Self { status: status.into(), steps, reason: reason.into(), subgoals_done: done.to_vec() }
    }
}

/// What a screen looks like to the stuck detector.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Fingerprint(Vec<(String, i32)>, String, Vec<(String, String, bool, bool)>);

pub fn fingerprint(s: &Snapshot) -> Fingerprint {
    Fingerprint(
        s.windows.iter().map(|w| (w.title.clone(), w.kind)).collect(),
        s.elements.iter().find(|e| e.focused).map(|e| e.key.clone()).unwrap_or_default(),
        s.elements.iter().map(|e| (e.key.clone(), e.value.clone(), e.selected, e.expanded)).collect(),
    )
}

pub(crate) use crate::options::fg as foreground;

pub(crate) fn short(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

pub(crate) fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}

/// A detour subgoal the LLM proposed; checked only by asking the Decider.
fn helper(goal: &str, launch: bool) -> Result<Subgoal> {
    let mut sg: Subgoal = serde_json::from_value(json!({"goal": goal}))?;
    sg.launch = launch;
    sg.steps = 6;
    sg.done = Some(Done { ask: Some(format!("Is this done on the screen: {goal}")), ..Default::default() });
    Ok(sg)
}

/// Typed/pressed/clicked state and the added-item baseline, carried across a subgoal's detours.
type Carry = (bool, bool, bool, Option<Element>, Option<usize>);

pub struct Runner<'a> {
    pub agent: &'a dyn Agent,
    pub decider: &'a dyn Decider,
    pub persona: Persona,
    pub log: &'a GroundTruth,
    pub clock: Clock,
    pub policy: Policy,
    pub assist: Option<Assist>,
    pub composer: Option<Composer>,
    rng: StdRng,
    snap: Snapshot,
    display: Option<(i32, i32, i32)>,
    mapping_ok: bool,
    steps: i64,
    live: i64,
    image: bool,
    focus_shared: bool,
}

impl<'a> Runner<'a> {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        agent: &'a dyn Agent,
        decider: &'a dyn Decider,
        persona: Persona,
        log: &'a GroundTruth,
        clock: Clock,
        policy: Policy,
        seed: Option<u64>,
        assist: Option<Assist>,
        composer: Option<Composer>,
    ) -> Self {
        Self {
            agent,
            decider,
            persona,
            log,
            clock,
            policy,
            assist,
            composer,
            rng: seed.map(StdRng::seed_from_u64).unwrap_or_else(|| StdRng::from_rng(&mut rand::rng())),
            snap: Snapshot::default(),
            display: None,
            mapping_ok: true,
            steps: 0,
            live: 0,
            image: std::env::var("LUDIUS_DECISION_IMAGE").as_deref() == Ok("1"),
            focus_shared: false,
        }
    }

    fn gauss(&mut self, mu: f64, sigma: f64) -> f64 {
        // Box-Muller: rand_distr only for this is not worth a dependency.
        let (u1, u2): (f64, f64) = (1.0 - self.rng.random::<f64>(), self.rng.random::<f64>());
        mu + sigma * (-2.0 * u1.ln()).sqrt() * (std::f64::consts::TAU * u2).cos()
    }

    pub fn observe(&mut self, timeout_ms: u32) -> Result<Snapshot> {
        self.snap = self.agent.observe(true, 300, timeout_ms)?;
        let display = (self.snap.screen_width, self.snap.screen_height, self.snap.dpi);
        if !self.snap.locked && display.0 > 0 && Some(display) != self.display {
            if self.display.is_some() {
                self.mapping_ok = self.recheck_mapping(display)?;
            }
            self.display = Some(display);
        }
        Ok(self.snap.clone())
    }

    fn recheck_mapping(&mut self, (w, h, dpi): (i32, i32, i32)) -> Result<bool> {
        let mut misses = Vec::new();
        for (x, y) in [(w / 2, h / 2), (w / 5, h / 4)] {
            self.agent.move_to(x, y, 0)?;
            let s = self.agent.observe(false, 300, 5000)?;
            if (s.cursor_x - x).abs() > 1 || (s.cursor_y - y).abs() > 1 {
                misses.push(json!([x, y, s.cursor_x, s.cursor_y]));
            }
        }
        self.log.write(json!({"event": "mapping_recheck", "host_ms": now_ms(), "display": [w, h, dpi], "ok": misses.is_empty(), "misses": misses}))?;
        Ok(misses.is_empty())
    }

    fn think(&mut self) {
        if self.clock == Clock::Realtime {
            let s = (self.gauss(0.0, 0.6).exp() * self.persona.think_mean_s).min(20.0);
            std::thread::sleep(Duration::from_secs_f64(s.max(0.0)));
        }
    }

    fn point(&mut self, e: &Element) -> (i32, i32) {
        let b = e.bounds.unwrap_or_default();
        let fx = self.gauss(0.5, 0.12).clamp(0.15, 0.85);
        let fy = self.gauss(0.5, 0.1).clamp(0.2, 0.8);
        let x = b.x + (f64::from(b.width) * fx) as i32;
        let y = b.y + (f64::from(b.height) * fy) as i32;
        (x.max(1), y.max(1))
    }

    fn move_to(&mut self, e: &Element) -> Result<()> {
        let (x, y) = self.point(e);
        let (lo, hi) = self.persona.pointer_ms;
        let ms = if self.clock == Clock::Realtime { self.rng.random_range(lo..=hi.max(lo)) } else { self.rng.random_range(60..=140) };
        self.agent.move_to(x, y, ms)?;
        Ok(())
    }

    fn type_text(&mut self, text: &str) -> Result<()> {
        let (mean, jitter) = if self.clock == Clock::Compressed { (25, 10) } else { (self.persona.typing_mean_ms, self.persona.typing_jitter_ms) };
        self.agent.type_text(text, mean, jitter)?;
        Ok(())
    }

    fn caret_to_end(&mut self, e: &Element, sg: Option<&Subgoal>) -> Result<()> {
        let editable_doc = e.role == "document" && !e.read_only;
        if sg.is_some_and(|s| s.caret == "start") {
            if editable_doc {
                self.agent.chord(crate::keys::chord("Ctrl+Home")?)?;
            }
        } else if editable_doc && !e.value.is_empty() {
            self.agent.chord(crate::keys::chord("Ctrl+End")?)?;
        }
        Ok(())
    }

    fn content(&self, task: &Task, rec: Map<String, Value>, why: &Option<String>) -> Value {
        let mut m = Map::new();
        m.insert("event".into(), json!("task_content"));
        m.insert("host_ms".into(), json!(now_ms()));
        m.insert("task".into(), json!(task.template.task));
        m.insert("reason".into(), json!(why));
        m.extend(rec);
        Value::Object(m)
    }

    /// Run one task. A reached call cap abandons it as "budget spent"; any other error is the caller's.
    pub fn run(&mut self, task: &mut Task, between: Option<&mut dyn FnMut() -> Result<()>>) -> Result<Outcome> {
        self.steps = 0;
        let mut done = Vec::new();
        match self.run_task(task, between, &mut done) {
            Err(e) if e.is::<CapReached>() => {
                let t = task.template.task.clone();
                self.log.write(json!({"event": "budget_spent", "host_ms": now_ms(), "task": t, "error": short(&e.to_string(), 300)}))?;
                self.abandon(task, "budget spent")?;
                self.log.write(json!({"event": "task_end", "host_ms": now_ms(), "task": t, "status": "abandoned", "reason": "budget spent", "steps": self.steps}))?;
                Ok(Outcome::new("abandoned", self.steps, "budget spent", &done))
            }
            r => r,
        }
    }

    fn run_task(&mut self, task: &mut Task, mut between: Option<&mut dyn FnMut() -> Result<()>>, done: &mut Vec<String>) -> Result<Outcome> {
        let mut t0 = Instant::now();
        let mut steps: i64 = 0;
        task.owner = self.persona.user.clone();
        task.tz = self.persona.tz.clone();
        task.values.insert("persona_name".into(), self.persona.name.clone());
        let guest = self.observe(5000)?.guest_time_ms;
        task.started_ms = if guest != 0 { guest } else { now_ms() };
        let t = task.template.task.clone();
        self.log.write(json!({"event": "task_start", "host_ms": now_ms(), "persona": self.persona.user, "vm": self.agent.vm(), "task": t, "variant": task.variant, "subgoals": task.subgoal_names}))?;
        let narrative = self.composer.as_ref().map(|c| c.narrative.clone()).unwrap_or_else(narrative);
        let mut why: Option<String> = None;
        let prepared = match task.template.cap() {
            "mail.reply" => Some(match &self.composer {
                Some(c) => c.prepare(task, &self.persona, self.decider, &self.policy)?,
                None => (Some("no reply composer (LLM) configured".into()), Map::new()),
            }),
            "mail.read" => Some(prepare_read(task, &self.policy, &narrative)),
            "mail.file" => Some(prepare_file(task, &self.policy, &narrative)),
            "calendar.respond" => Some(prepare_answer(task, &self.policy, &narrative)),
            _ => None,
        };
        if let Some((w, rec)) = prepared {
            self.log.write(self.content(task, rec, &w))?;
            why = w;
        }
        if (task.template.sends_mail() || task.template.cap() == "calendar.respond") && why.is_none() {
            task.mail_before = mail_baseline(task)?;
        }
        if why.is_none() {
            why = requests::waiting(task, &requests::root());
        }
        if why.is_none() {
            why = file_baseline(task, &self.policy)?;
        }
        if let Some(why) = why {
            self.log.write(json!({"event": "blocked", "host_ms": now_ms(), "task": t, "reason": why}))?;
            let reason = format!("start: {why}");
            self.log.write(json!({"event": "task_end", "host_ms": now_ms(), "task": t, "status": "abandoned", "reason": reason, "steps": 0}))?;
            return Ok(Outcome::new("abandoned", 0, reason, done));
        }
        let names = task.subgoal_names.clone();
        let budget_steps = i64::from(task.template.budget_steps);
        for (i, name) in names.iter().enumerate() {
            let sg = task.subgoal(name)?;
            let mut carry: Option<Carry> = None;
            let may = self.assist.is_some();
            let mut result = self.subgoal(task, name, &sg, i64::from(sg.steps).min(budget_steps - steps), t0, may, Some(&mut carry))?;
            steps += result.1;
            self.steps = steps;
            let mut detours = 0;
            while !result.0
                && (result.2 == "step budget" || result.2.starts_with(STUCK))
                && self.assist.is_some()
                && detours < MAX_DETOURS
                && budget_steps - steps > 0
            {
                detours += 1;
                steps += self.assisted(task, name, &sg, t0, steps, &result.2)?;
                self.steps = steps;
                let left = budget_steps - steps;
                result = self.subgoal(task, name, &sg, (i64::from(sg.steps) / 2 + 2).min(left), t0, detours < MAX_DETOURS, Some(&mut carry))?;
                steps += result.1;
                self.steps = steps;
            }
            if !result.0 {
                self.abandon(task, &result.2)?;
                let out = Outcome::new("abandoned", steps, format!("{name}: {}", result.2), done);
                self.log.write(json!({"event": "task_end", "host_ms": now_ms(), "task": t, "status": out.status, "reason": out.reason, "steps": steps}))?;
                return Ok(out);
            }
            done.push(name.clone());
            if let Some(b) = between.as_deref_mut()
                && i + 1 < names.len()
            {
                let paused = Instant::now();
                b()?;
                t0 += paused.elapsed();
            }
        }
        let (mut reason, mut status) = (String::new(), "done");
        if !task.inst.request.is_empty() {
            let (ok, why) = requests::fulfilled(task, &requests::root());
            self.log.write(json!({"event": "request_check", "host_ms": now_ms(), "task": t, "request": task.inst.request, "fulfilled": ok, "reason": why, "capability": task.template.capability}))?;
            if ok && task.template.cap() == "file.write" {
                // Unrecorded only blocks the delivery.
                let _ = requests::remember(&requests::root(), &task.owner, &task.inst.request, requests::WRITTEN);
            }
            reason = format!("request: {why}");
            status = if ok { "done" } else { "failed" };
        }
        self.log.write(json!({"event": "task_end", "host_ms": now_ms(), "task": t, "status": status, "steps": steps, "reason": reason}))?;
        Ok(Outcome::new(status, steps, reason, done))
    }

    fn assisted(&mut self, task: &mut Task, name: &str, sg: &Subgoal, t0: Instant, steps: i64, why: &str) -> Result<i64> {
        let snap = self.observe(5000)?;
        let v = crate::options::view(&snap, task, sg, &self.policy);
        let why = if why.starts_with(STUCK) { why.to_string() } else { format!("{STUCK}: {why}") };
        let state = crate::options::build_state(&v, task, sg, &[], &why)?;
        let proposal = self.assist.as_ref().expect("assisted only with an assist").next_subgoal(&state)?;
        self.log.write(json!({"event": "llm_subgoal", "host_ms": now_ms(), "task": task.template.task, "subgoal": name, "goal": proposal.goal, "why": why}))?;
        let goal = proposal.goal.replace('{', "{{").replace('}', "}}");
        let h = helper(&goal, sg.launch)?;
        let budget = i64::from(h.steps).min(i64::from(task.template.budget_steps) - steps);
        Ok(self.subgoal(task, &format!("{name}/detour"), &h, budget, t0, false, None)?.1)
    }

    #[allow(clippy::too_many_arguments)]
    fn subgoal(&mut self, task: &mut Task, name: &str, sg: &Subgoal, budget: i64, t0: Instant, may_detour: bool, carry: Option<&mut Option<Carry>>) -> Result<(bool, i64, String)> {
        self.live = 0;
        let r = self.subgoal_steps(task, name, sg, budget, t0, may_detour, carry);
        if r.as_ref().is_err_and(|e| e.is::<CapReached>()) {
            self.steps += self.live; // the run's budget abandonment counts this subgoal's steps too
        }
        r
    }
}

#[cfg(test)]
mod tests;
