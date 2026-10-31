//! The persona's Windows session around a task: unlock, normalize the desktop, the escalation ladder (port of session.py).
use std::collections::{BTreeSet, HashMap};
use std::time::{Duration, Instant};

use anyhow::{Result, bail};
use ludius_proto::v1::lifecycle_request::Op;
use ludius_proto::v1::{Element, Rect, Snapshot, Window, window::Kind};
use serde_json::json;

use crate::keys::chord;
use crate::policy::{SHELL_PROCESSES, shell_dialog};
use crate::runner::{Agent, GroundTruth, foreground as fg_of, now_ms};
use crate::tasks::pixels_window;
use crate::template::fullmatch;

const DISCARD: &str = r"(don'?t save|no|discard)";
const DISMISS: &str = r"(cancel|close|ok|not now|no thanks|dismiss|got it)";

macro_rules! error {
    ($name:ident) => {
        #[derive(Debug)]
        pub struct $name(pub String);
        impl std::fmt::Display for $name {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str(&self.0)
            }
        }
        impl std::error::Error for $name {}
    };
}
error!(VmUnhealthy);
error!(EscalationDenied);

pub type Lifecycle<'a> = &'a dyn Fn(Op) -> Result<String>;

pub struct Session<'a> {
    pub agent: &'a dyn Agent,
    pub log: &'a GroundTruth,
    password: String,
    pub lifecycle: Option<Lifecycle<'a>>,
}

fn sleep(s: f64) {
    std::thread::sleep(Duration::from_secs_f64(s));
}

fn is(w: &Window, k: Kind) -> bool {
    w.kind == k as i32
}

fn shell_surface(w: &Window) -> bool {
    ["Progman", "WorkerW", "Shell_TrayWnd", "OperationStatusWindow"].contains(&w.class_name.as_str())
        || (SHELL_PROCESSES.contains(&w.process_name.to_lowercase().as_str()) && w.process_name.to_lowercase() != "explorer.exe")
}

fn inert_shell_popup(snap: &Snapshot, i: usize) -> bool {
    let w = &snap.windows[i];
    w.process_name.to_lowercase() == "explorer.exe"
        && ["Worker Window", "SysShadow"].contains(&w.class_name.as_str())
        && w.no_tree
        && !snap.elements.iter().any(|e| e.window as usize == i)
}

fn contains(b: &Rect, x: i32, y: i32) -> bool {
    b.x <= x && x < b.x + b.width && b.y <= y && y < b.y + b.height
}

fn explorer_window(w: &Window) -> bool {
    w.process_name.to_lowercase() == "explorer.exe" && w.class_name == "CabinetWClass"
}

fn button<'s>(snap: &'s Snapshot, pattern: &str) -> Option<&'s Element> {
    snap.elements.iter().find(|e| e.role == "button" && fullmatch(pattern, e.name.replace('&', "").trim(), true))
}

impl<'a> Session<'a> {
    pub fn new(agent: &'a dyn Agent, log: &'a GroundTruth, password: String, lifecycle: Option<Lifecycle<'a>>) -> Self {
        Self { agent, log, password, lifecycle }
    }

    pub fn observe(&self, timeout_ms: u32) -> Result<Snapshot> {
        self.agent.observe(true, 300, timeout_ms)
    }

    pub fn wait_ready(&self, seconds: f64) -> Result<Snapshot> {
        let deadline = Instant::now() + Duration::from_secs_f64(seconds);
        loop {
            match self.observe(2000) {
                Ok(s) if !s.locked => return Ok(s),
                Err(e) if Instant::now() > deadline => return Err(e),
                _ => {}
            }
            if Instant::now() > deadline {
                bail!("desktop not ready");
            }
            sleep(2.0);
        }
    }

    pub fn unlock(&self) -> Result<bool> {
        if !self.observe(5000)?.locked {
            return Ok(true);
        }
        self.log.write(json!({"event": "unlock", "host_ms": now_ms()}))?;
        self.sign_in()
    }

    pub fn sign_in(&self) -> Result<bool> {
        for _ in 0..2 {
            self.agent.ctrl_alt_del()?;
            sleep(2.0);
            self.agent.type_text(&format!("{}\n", self.password), 90, 40)?;
            if self.wait_ready(30.0).is_ok() {
                self.log.write(json!({"event": "unlocked", "host_ms": now_ms()}))?;
                return Ok(true);
            }
            self.agent.chord(chord("Esc")?)?;
            sleep(1.0);
        }
        Ok(false)
    }

    pub fn normalize(&self, keep: &BTreeSet<String>, rounds: u32) -> Result<bool> {
        let keep: BTreeSet<String> = keep.iter().map(|k| k.to_lowercase()).collect();
        let mut closed: Vec<String> = Vec::new();
        let mut tried: HashMap<(String, String), u32> = HashMap::new();
        for _ in 0..rounds {
            if !self.unlock()? {
                return Ok(false);
            }
            let snap = self.observe(5000)?;
            if snap.secure_desktop {
                self.agent.chord(chord("Esc")?)?;
                closed.push("UAC prompt".into());
                sleep(1.0);
                continue;
            }
            let fg = fg_of(&snap);
            if fg.is_some_and(pixels_window) {
                if snap.elements.iter().any(|e| e.framework_id == "visual" && e.name == "Shut Down Windows") {
                    self.agent.chord(chord("Esc")?)?;
                    sleep(0.6);
                    continue;
                }
                self.log.write(json!({"event": "normalized", "host_ms": now_ms(), "closed": closed, "trust": "pixels", "verified": false}))?;
                return Ok(true);
            }
            let popup = snap.windows.iter().enumerate().find(|(i, w)| {
                (is(w, Kind::Popup) || is(w, Kind::Owned))
                    && !inert_shell_popup(&snap, *i)
                    && tried.get(&(w.title.clone(), w.class_name.clone())).copied().unwrap_or(0) < 2
            });
            let strays: Vec<&Window> = snap
                .apps
                .iter()
                .filter(|a| {
                    let p = a.process_name.to_lowercase();
                    !keep.contains(&p) && (!SHELL_PROCESSES.contains(&p.as_str()) || explorer_window(a))
                })
                .collect();
            let dialog = fg.is_some_and(|w| w.class_name == "#32770");
            if fg.is_some_and(|w| shell_surface(w) || (keep.contains(&w.process_name.to_lowercase()) && !dialog)) && popup.is_none() && strays.is_empty() {
                self.log.write(json!({"event": "normalized", "host_ms": now_ms(), "closed": closed}))?;
                return Ok(true);
            }
            if popup.is_some() || dialog {
                if let Some((_, p)) = popup {
                    *tried.entry((p.title.clone(), p.class_name.clone())).or_default() += 1;
                }
                let target = if shell_dialog(fg) { None } else { button(&snap, DISCARD).or_else(|| button(&snap, DISMISS)) };
                match target {
                    Some(t) => self.click(t)?,
                    None => _ = self.agent.chord(chord("Esc")?)?,
                }
            } else if let Some(w) = fg.filter(|w| !shell_surface(w) && !keep.contains(&w.process_name.to_lowercase())) {
                closed.push(format!("{} ({})", w.title, w.process_name));
                self.agent.chord(chord("Alt+F4")?)?;
            } else if let Some(stray) = strays.first() {
                let app = stray.title.rsplit(" - ").next().unwrap_or("").replace('\u{200b}', "").to_lowercase();
                let btn = snap.elements.iter().find(|e| e.role == "button" && !app.is_empty() && e.name.replace('\u{200b}', "").to_lowercase().contains(&app));
                let (x, y) = (snap.screen_width * 3 / 4, snap.screen_height / 3);
                let clear = || {
                    snap.windows.iter().chain(&snap.apps).all(|w| {
                        let b = w.bounds.unwrap_or_default();
                        b.width > 0 && b.height > 0 && (["Progman", "WorkerW"].contains(&w.class_name.as_str()) || !contains(&b, x, y))
                    })
                };
                if let Some(b) = btn {
                    self.click(b)?;
                } else if fg.is_some_and(|w| w.class_name == "Shell_TrayWnd") && clear() {
                    self.agent.move_to(x, y, 120)?;
                    self.agent.click(1)?;
                } else {
                    self.agent.chord(chord("Alt+Tab")?)?;
                }
            } else {
                self.agent.chord(chord("Esc")?)?;
            }
            sleep(0.6);
        }
        let mut rec = json!({"event": "normalize_failed", "host_ms": now_ms(), "closed": closed});
        // Diagnostics only; unreadable leaves them out.
        if let Ok(snap) = self.observe(5000) {
            let line = |w: &Window| format!("{} ({}, {})", w.title, w.process_name, w.class_name);
            rec["foreground"] = json!(fg_of(&snap).map(line));
            rec["windows"] = json!(snap.windows.iter().filter(|w| is(w, Kind::Popup) || is(w, Kind::Owned)).map(line).take(6).collect::<Vec<_>>());
            rec["apps"] = json!(snap.apps.iter().map(|a| format!("{} ({})", a.title, a.process_name)).take(8).collect::<Vec<_>>());
        }
        self.log.write(rec)?;
        Ok(false)
    }

    fn click(&self, e: &Element) -> Result<()> {
        let b = e.bounds.unwrap_or_default();
        self.agent.move_to((b.x + b.width / 2).max(1), (b.y + b.height / 2).max(1), 120)?;
        self.agent.click(1)?;
        Ok(())
    }

    pub fn escalate(&self) -> Result<u32> {
        let none = BTreeSet::new();
        if std::env::var("LUDIUS_NO_ESCALATE").as_deref() == Ok("1") {
            self.log.write(json!({"event": "escalate_blocked", "host_ms": now_ms()}))?;
            return Err(EscalationDenied("LUDIUS_NO_ESCALATE=1: ladder steps 4-8 denied, no input sent".into()).into());
        }
        self.log.write(json!({"event": "ladder", "step": 4, "action": "restart app", "host_ms": now_ms()}))?;
        if self.normalize(&none, 12)? {
            return Ok(4);
        }
        self.log.write(json!({"event": "ladder", "step": 5, "action": "normalize session", "host_ms": now_ms()}))?;
        if self.normalize(&none, 24)? {
            return Ok(5);
        }
        self.log.write(json!({"event": "ladder", "step": 6, "action": "sign out", "host_ms": now_ms()}))?;
        self.agent.chord(chord("Win+X")?)?;
        sleep(1.0);
        self.agent.chord(chord("U")?)?;
        sleep(0.6);
        self.agent.chord(chord("I")?)?;
        sleep(15.0);
        if matches!(self.sign_in(), Ok(true)) && matches!(self.normalize(&none, 12), Ok(true)) {
            return Ok(6);
        }
        self.log.write(json!({"event": "ladder", "step": 7, "action": "reboot VM", "host_ms": now_ms()}))?;
        if let Some(lc) = self.lifecycle {
            let rebooted = (|| -> Result<bool> {
                lc(Op::Stop)?;
                lc(Op::Start)?;
                self.wait_ready(300.0)?;
                self.normalize(&none, 12)
            })();
            if matches!(rebooted, Ok(true)) {
                return Ok(7);
            }
        }
        self.log.write(json!({"event": "ladder", "step": 8, "action": "mark VM unhealthy", "host_ms": now_ms()}))?;
        Err(VmUnhealthy("session could not be normalized".into()).into())
    }
}

/// One task with desktop hygiene around it (port of day.run_task).
pub fn run_task(
    runner: &mut crate::runner::Runner,
    session: &Session,
    task: &mut crate::tasks::Task,
    between: Option<&mut dyn FnMut() -> Result<()>>,
    hygiene: bool,
    keep: &BTreeSet<String>,
) -> Result<crate::runner::Outcome> {
    let denied = |e: &anyhow::Error| e.is::<EscalationDenied>();
    if hygiene && !session.normalize(keep, 12)? {
        match session.escalate() {
            Err(e) if denied(&e) => {
                let reason = "desktop not clean, escalation denied";
                runner.log.write(json!({"event": "task_end", "host_ms": now_ms(), "task": task.template.task, "status": "abandoned", "reason": reason, "steps": 0}))?;
                return Ok(crate::runner::Outcome { status: "abandoned".into(), steps: 0, reason: reason.into(), subgoals_done: vec![] });
            }
            r => _ = r?,
        }
    }
    let apps = |s: Snapshot| s.apps.iter().map(|a| format!("{} ({})", a.title, a.process_name)).collect::<Vec<_>>();
    let before = apps(runner.observe(5000)?);
    let out = runner.run(task, between)?;
    let after = apps(runner.observe(5000)?);
    let opened: Vec<&String> = after.iter().filter(|a| !before.contains(a)).collect();
    runner.log.write(json!({"event": "side_effects", "host_ms": now_ms(), "task": task.template.task, "opened": opened, "left": after}))?;
    if hygiene && out.status != "done" && !session.normalize(keep, 12)? {
        match session.escalate() {
            Err(e) if denied(&e) => {}
            r => _ = r?,
        }
    }
    Ok(out)
}
