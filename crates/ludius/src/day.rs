//! A persona's working day (port of day.py): plan items at their times, interrupts between subgoals, breaks
//! that lock and unlock the desktop, chat reactions before each slot. Irreversible items never run twice.
use std::collections::BTreeSet;

use anyhow::Result;
use chrono::{Duration, NaiveDate, NaiveDateTime, NaiveTime};
use rand::rngs::StdRng;
use rand::{RngExt as _, SeedableRng};
use serde_json::json;

use crate::calls;
use crate::planner::{BREAK, DayPlan, PlanItem, started_slots};
use crate::runner::{Clock, GroundTruth, Outcome, Runner, now_ms};
use crate::session::{EscalationDenied, Session, run_task};
use crate::tasks::{Task, Template, write_paths};

/// Runs `between` after each subgoal; it gets the desk back so an interrupt can run a task inside the task.
pub type Between<'b> = &'b mut dyn FnMut(&mut dyn Desk) -> Result<()>;

/// What a day needs from the desktop: run a task with hygiene, normalize, escalate, unlock, lock.
pub trait Desk {
    fn run(&mut self, task: &mut Task, between: Option<Between>, hygiene: bool, keep: &BTreeSet<String>) -> Result<Outcome>;
    fn normalize(&mut self) -> Result<bool>;
    fn escalate(&mut self) -> Result<u32>;
    fn unlock(&mut self) -> Result<bool>;
    fn lock(&mut self) -> Result<()>;
}

/// The real desk: a runner and the persona's session.
pub struct Live<'r, 'a> {
    pub runner: &'r mut Runner<'a>,
    pub session: &'r Session<'r>,
}

impl<'a> Desk for Live<'_, 'a> {
    fn run(&mut self, task: &mut Task, between: Option<Between>, hygiene: bool, keep: &BTreeSet<String>) -> Result<Outcome> {
        let session = self.session;
        match between {
            None => run_task(self.runner, session, task, None, hygiene, keep),
            Some(b) => run_task(self.runner, session, task, Some(&mut |r: &mut Runner<'a>| b(&mut Live { runner: r, session })), hygiene, keep),
        }
    }
    fn normalize(&mut self) -> Result<bool> {
        self.session.normalize(&BTreeSet::new(), 12)
    }
    fn escalate(&mut self) -> Result<u32> {
        self.session.escalate()
    }
    fn unlock(&mut self) -> Result<bool> {
        self.session.unlock()
    }
    fn lock(&mut self) -> Result<()> {
        self.session.agent.chord(crate::keys::chord("Win+L")?).map(drop)
    }
}

#[derive(Debug)]
pub struct BreakUnlockFailed(pub String);

impl std::fmt::Display for BreakUnlockFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for BreakUnlockFailed {}

#[derive(Debug, Clone, PartialEq)]
pub struct ItemResult {
    pub item: PlanItem,
    pub status: String,
}

/// Chat reactions due before a slot (react.Reactor); `run` runs one reaction item and returns its status.
pub trait React {
    fn react(&mut self, day: &str, at: &str, run: &mut dyn FnMut(&PlanItem) -> Result<String>) -> Result<()>;
}

/// Regular items in time order, each with the interrupts due before the next regular item.
pub fn slots(plan: &DayPlan) -> Vec<(PlanItem, Vec<PlanItem>)> {
    let mut items = plan.items.clone();
    items.sort_by(|a, b| a.at.cmp(&b.at));
    let (regular, mut pending): (Vec<_>, Vec<_>) = items.into_iter().partition(|i| !i.interrupt);
    let mut out = Vec::new();
    for (n, item) in regular.iter().enumerate() {
        let end = regular.get(n + 1).map_or("24:00", |r| r.at.as_str());
        let (mine, rest): (Vec<_>, Vec<_>) = pending.into_iter().partition(|i| item.at <= i.at && i.at.as_str() < end);
        pending = rest;
        out.push((item.clone(), mine));
    }
    out.extend(pending.into_iter().map(|o| (o, vec![])));
    out.sort_by(|a, b| a.0.at.cmp(&b.0.at));
    out
}

fn clock_name(c: Clock) -> &'static str {
    match c {
        Clock::Realtime => "realtime",
        Clock::Compressed => "compressed",
        Clock::TimeJump => "time-jump",
    }
}

fn at_time(day: NaiveDate, at: &str) -> NaiveDateTime {
    day.and_time(NaiveTime::parse_from_str(at, "%H:%M").unwrap_or_default())
}

/// When the persona is back from a break.
pub fn back_at(day: NaiveDate, brk: &PlanItem) -> NaiveDateTime {
    at_time(day, &brk.at) + Duration::minutes(brk.minutes as i64)
}

pub type SetClock<'r> = Box<dyn FnMut(Option<NaiveDateTime>) -> Result<()> + 'r>;

pub struct DayRunner<'r> {
    pub templates: std::collections::BTreeMap<String, Template>,
    pub log: &'r GroundTruth,
    pub clock: Clock,
    rng: StdRng,
    pub sleep: Box<dyn FnMut(f64) + 'r>,
    pub now: Box<dyn Fn() -> NaiveDateTime + 'r>,
    /// Sets the guest clock (time-jump); None hands it back to real time.
    pub set_clock: Option<SetClock<'r>>,
    pub reactor: Option<&'r mut dyn React>,
    pub results: Vec<ItemResult>,
}

impl<'r> DayRunner<'r> {
    pub fn new(templates: std::collections::BTreeMap<String, Template>, log: &'r GroundTruth, clock: Clock, seed: u64) -> Self {
        Self {
            templates,
            log,
            clock,
            rng: StdRng::seed_from_u64(seed),
            sleep: Box::new(|s| std::thread::sleep(std::time::Duration::from_secs_f64(s))),
            now: Box::new(|| chrono::Local::now().naive_local()),
            set_clock: None,
            reactor: None,
            results: vec![],
        }
    }

    pub fn run(&mut self, desk: &mut dyn Desk, plan: &DayPlan) -> Result<Vec<ItemResult>> {
        let day = NaiveDate::parse_from_str(&plan.date, "%Y-%m-%d")?;
        self.start(plan)?;
        for (item, mut interrupts) in slots(plan) {
            self.wait_for(day, &item.at);
            if let Err(e) = self.slot(desk, day, &item, &mut interrupts, None) {
                if e.is::<BreakUnlockFailed>() {
                    self.release_clock()?;
                }
                return Err(e);
            }
        }
        self.finish(plan)?;
        Ok(self.results.clone())
    }

    pub fn start(&self, plan: &DayPlan) -> Result<()> {
        self.log.write(json!({"event": "day_start", "host_ms": now_ms(), "persona": plan.persona, "date": plan.date, "clock": clock_name(self.clock), "items": plan.items.len()}))
    }

    pub fn finish(&mut self, plan: &DayPlan) -> Result<()> {
        self.release_clock()?;
        let done = self.results.iter().filter(|r| r.status == "done").count();
        self.log.write(json!({"event": "day_end", "host_ms": now_ms(), "persona": plan.persona, "date": plan.date, "done": done, "items": self.results.len()}))
    }

    fn release_clock(&mut self) -> Result<()> {
        match (&mut self.set_clock, self.clock) {
            (Some(set), Clock::TimeJump) => set(None),
            _ => Ok(()),
        }
    }

    /// One slot: reactions due, the item, then any interrupts it did not take. Returns this slot's results.
    pub fn slot(&mut self, desk: &mut dyn Desk, day: NaiveDate, item: &PlanItem, interrupts: &mut Vec<PlanItem>, jump_at: Option<&str>) -> Result<Vec<ItemResult>> {
        calls::on_day(day, || {
            let first = self.results.len();
            if item.template != BREAK {
                self.jump(day, jump_at.unwrap_or(&item.at))?;
                if let Some(reactor) = self.reactor.take() {
                    let r = reactor.react(&day.format("%Y-%m-%d").to_string(), &item.at, &mut |it| self.reaction(desk, day, it));
                    self.reactor = Some(reactor);
                    r?;
                }
            }
            self.item(desk, item, day, interrupts, false)?;
            for late in std::mem::take(interrupts) {
                self.item(desk, &late, day, &mut vec![], false)?;
            }
            Ok(self.results[first..].to_vec())
        })
    }

    fn logical(day: NaiveDate, at: &str) -> String {
        format!("{}T{at}", day.format("%Y-%m-%d"))
    }

    fn item(&mut self, desk: &mut dyn Desk, item: &PlanItem, day: NaiveDate, interrupts: &mut Vec<PlanItem>, nested: bool) -> Result<()> {
        if item.template == BREAK {
            self.begin_break(desk, day, item)?;
            let away = self.away_s(item);
            (self.sleep)(away);
            if self.end_break(desk, day, item)?.status != "done" {
                return Err(BreakUnlockFailed(format!("break at {}: unlock failed, day stopped", item.at)).into());
            }
            return Ok(());
        }
        let Some(t) = self.templates.get(&item.template).cloned() else { anyhow::bail!("unknown template {:?}", item.template) };
        if t.irreversible() && started_slots(&self.log.path)?.contains(&(item.at.clone(), item.template.clone())) {
            self.log.write(json!({"event": "item_skipped", "host_ms": now_ms(), "logical": Self::logical(day, &item.at), "template": item.template, "reason": "already attempted, effects never repeated"}))?;
            self.ended(day, item, "skipped")?;
            return Ok(());
        }
        self.started(day, item)?;
        let mut task = Task::new(t, item.instance(), &mut self.rng)?;
        let mine: BTreeSet<String> = write_paths(&task)?.iter().map(|w| w.to_lowercase()).collect();
        let realtime = self.clock == Clock::Realtime;
        let mut between = |d: &mut dyn Desk| -> Result<()> {
            let mut due = Vec::new();
            for i in interrupts.iter() {
                let writes: BTreeSet<String> = if i.template == BREAK {
                    BTreeSet::new()
                } else {
                    let ti = self.templates.get(&i.template).cloned().ok_or_else(|| anyhow::anyhow!("unknown template {:?}", i.template))?;
                    write_paths(&Task::new(ti, i.instance(), &mut StdRng::seed_from_u64(0))?)?.iter().map(|w| w.to_lowercase()).collect()
                };
                if (!realtime || (self.now)() >= at_time(day, &i.at)) && writes.is_disjoint(&mine) {
                    due.push(i.clone());
                }
            }
            interrupts.retain(|i| !due.contains(i));
            for i in due {
                self.log.write(json!({"event": "interrupt", "host_ms": now_ms(), "task": item.template, "by": i.template, "at": i.at}))?;
                self.item(d, &i, day, &mut vec![], true)?;
                self.log.write(json!({"event": "resume", "host_ms": now_ms(), "task": item.template}))?;
            }
            Ok(())
        };
        let status = desk.run(&mut task, Some(&mut between), !nested, &BTreeSet::new())?.status;
        self.ended(day, item, &status)?;
        Ok(())
    }

    fn reaction(&mut self, desk: &mut dyn Desk, day: NaiveDate, item: &PlanItem) -> Result<String> {
        let logical = Self::logical(day, &item.at);
        self.log.write(json!({"event": "reaction_start", "host_ms": now_ms(), "logical": logical, "template": item.template, "context": item.context, "params": item.params}))?;
        let chat: BTreeSet<String> = self.templates.get("chat_read_messages").map(|t| t.processes.iter().cloned().collect()).unwrap_or_default();
        let t = self.templates.get(&item.template).cloned().ok_or_else(|| anyhow::anyhow!("unknown template {:?}", item.template))?;
        let mut task = Task::new(t, item.instance(), &mut self.rng)?;
        let status = desk.run(&mut task, None, true, &chat)?.status;
        self.log.write(json!({"event": "reaction_end", "host_ms": now_ms(), "logical": logical, "template": item.template, "params": item.params, "status": status}))?;
        Ok(status)
    }

    fn started(&self, day: NaiveDate, item: &PlanItem) -> Result<()> {
        self.log.write(json!({"event": "item_start", "host_ms": now_ms(), "logical": Self::logical(day, &item.at), "at": item.at, "template": item.template, "context": item.context, "interrupt": item.interrupt}))
    }

    fn ended(&mut self, day: NaiveDate, item: &PlanItem, status: &str) -> Result<ItemResult> {
        let r = ItemResult { item: item.clone(), status: status.into() };
        self.results.push(r.clone());
        self.log.write(json!({"event": "item_end", "host_ms": now_ms(), "logical": Self::logical(day, &item.at), "at": item.at, "template": item.template, "context": item.context, "params": item.params, "status": status}))?;
        Ok(r)
    }

    pub fn away_s(&self, item: &PlanItem) -> f64 {
        if self.clock == Clock::Realtime { item.minutes as f64 * 60.0 } else { 5.0 }
    }

    pub fn begin_break(&mut self, desk: &mut dyn Desk, day: NaiveDate, item: &PlanItem) -> Result<()> {
        self.jump(day, &item.at)?;
        self.started(day, item)?;
        if !desk.normalize()? {
            match desk.escalate() {
                Err(e) if e.is::<EscalationDenied>() => {}
                r => _ = r?,
            }
        }
        desk.lock()?;
        self.log.write(json!({"event": "break", "host_ms": now_ms(), "minutes": item.minutes, "away_s": self.away_s(item) as i64}))
    }

    pub fn end_break(&mut self, desk: &mut dyn Desk, day: NaiveDate, item: &PlanItem) -> Result<ItemResult> {
        self.jump(day, &back_at(day, item).format("%H:%M").to_string())?;
        let ok = desk.unlock()?;
        self.ended(day, item, if ok { "done" } else { "failed" })
    }

    fn jump(&mut self, day: NaiveDate, at: &str) -> Result<()> {
        let (Some(set), Clock::TimeJump) = (&mut self.set_clock, self.clock) else { return Ok(()) };
        let logical = at_time(day, at);
        set(Some(logical))?;
        self.log.write(json!({"event": "clock_set", "host_ms": now_ms(), "logical": logical.format("%Y-%m-%dT%H:%M:%S").to_string()}))
    }

    fn wait_for(&mut self, day: NaiveDate, at: &str) {
        if self.clock != Clock::Realtime {
            return;
        }
        let target = at_time(day, at) + Duration::milliseconds((self.rng.random::<f64>() * 120_000.0) as i64);
        loop {
            let left = (target - (self.now)()).num_milliseconds() as f64 / 1000.0;
            if left <= 0.0 {
                break;
            }
            (self.sleep)(left.min(60.0));
        }
    }
}

#[cfg(test)]
pub(crate) mod tests;
