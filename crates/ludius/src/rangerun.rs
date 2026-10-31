//! The range supervisor (port of rangerun.py): one plan worker and one day worker per persona, each
//! restarted with backoff when it dies, plus the status table. Schedules are not created here yet.
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Child as Os, Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use chrono::NaiveDate;
use serde_json::{Value, json};

use crate::planner::World;

pub const CAPS: [&str; 4] = ["LUDIUS_CAP_DECISION", "LUDIUS_CAP_LLM", "LUDIUS_CAP_PERSONA_DECISION", "LUDIUS_CAP_PERSONA_LLM"];
const BACKOFF: (f64, f64) = (5.0, 300.0);
const STEADY_S: f64 = 600.0;
/// What a worker prints when an activity fails (durable::worker::failure); a live pid is not health.
pub const FAILED: &str = "activity failed:";

pub fn vms() -> PathBuf {
    std::env::var_os("LUDIUS_RANGE_VMS").map(PathBuf::from).unwrap_or_else(|| crate::paths::root().join("infra/ansible/group_vars/all/range-vms.yml"))
}

pub fn personas(path: &Path) -> Result<Vec<String>> {
    let v: Value = serde_yaml::from_str(&std::fs::read_to_string(path).with_context(|| path.display().to_string())?)?;
    v["range_personas"].as_array().context("range_personas")?.iter().map(|p| p["user"].as_str().map(String::from).context("range_personas[].user")).collect()
}

pub fn vm_of(user: &str) -> String {
    format!("ludius-{user}")
}

pub fn password(user: &str, env: &dyn Fn(&str) -> String) -> String {
    Some(env(&format!("LUDIUS_PERSONA_PASSWORD_{}", user.to_uppercase()))).filter(|p| !p.is_empty()).unwrap_or_else(|| env("LUDIUS_PERSONA_PASSWORD"))
}

pub fn refusals(users: &[String], env: &dyn Fn(&str) -> String, persona_files: &Path) -> Vec<String> {
    let mut why: Vec<String> = CAPS
        .iter()
        .filter(|k| !(!env(k).is_empty() && env(k).bytes().all(|b| b.is_ascii_digit())))
        .map(|k| format!("{k} is not set (unattended runs are budgeted, in total and per persona and day)"))
        .collect();
    for u in users {
        let file = persona_files.join(format!("{u}.yaml"));
        if !file.exists() {
            why.push(format!("{u}: no persona file {}", file.display()));
        }
        if password(u, env).is_empty() {
            why.push(format!("{u}: no unlock password (LUDIUS_PERSONA_PASSWORD_{} or LUDIUS_PERSONA_PASSWORD)", u.to_uppercase()));
        }
    }
    why
}

pub struct Spec {
    pub name: String,
    pub args: Vec<String>,
    /// Set over the inherited environment; LUDIUS_PERSONA and LUDIUS_VM are always cleared first.
    pub env: Vec<(String, String)>,
}

pub fn specs(users: &[String], agent: &str, temporal: &str, model: &str, env: &dyn Fn(&str) -> String) -> Vec<Spec> {
    let s = |v: &[&str]| v.iter().map(|a| a.to_string()).collect::<Vec<_>>();
    let mut out = vec![Spec { name: "plan".into(), args: s(&["--agent", agent, "plan-worker", "--temporal", temporal]), env: vec![] }];
    for u in users {
        let vm = vm_of(u);
        let own = vec![("LUDIUS_PERSONA".into(), u.clone()), ("LUDIUS_VM".into(), vm.clone()), ("LUDIUS_PERSONA_PASSWORD".into(), password(u, env))];
        out.push(Spec { name: u.clone(), args: s(&["--agent", agent, "--vm", &vm, "worker", "--temporal", temporal, "--persona", u, "--model", model]), env: own });
    }
    out
}

pub trait Proc {
    fn pid(&self) -> u32;
    fn poll(&mut self) -> Option<i32>;
    fn kill(&mut self);
}

impl Proc for Os {
    fn pid(&self) -> u32 {
        self.id()
    }
    fn poll(&mut self) -> Option<i32> {
        // an unreadable status counts as exited, so the worker is restarted rather than trusted
        self.try_wait().map_or(Some(-1), |s| s.map(|s| s.code().unwrap_or(-1)))
    }
    fn kill(&mut self) {
        let _ = Os::kill(self);
        let _ = self.wait();
    }
}

/// Start `spec` as this binary with its output appended to `log`.
pub fn spawn(spec: &Spec, log: &Path) -> std::io::Result<Box<dyn Proc>> {
    std::fs::create_dir_all(log.parent().unwrap_or(Path::new(".")))?;
    let out = std::fs::OpenOptions::new().create(true).append(true).open(log)?;
    let mut cmd = Command::new(std::env::current_exe()?);
    cmd.args(&spec.args).env_remove("LUDIUS_PERSONA").env_remove("LUDIUS_VM").envs(spec.env.iter().cloned());
    Ok(Box::new(cmd.stdin(Stdio::null()).stdout(out.try_clone()?).stderr(out).spawn()?))
}

pub struct Child {
    pub spec: Spec,
    proc: Option<Box<dyn Proc>>,
    started: f64,
    due: f64,
    wait: f64,
    pub starts: u32,
    pub last_exit: Option<i32>,
}

impl Child {
    pub fn new(spec: Spec) -> Self {
        Self { spec, proc: None, started: 0.0, due: 0.0, wait: BACKOFF.0, starts: 0, last_exit: None }
    }
}

pub type Spawn = Box<dyn FnMut(&Spec, &Path) -> std::io::Result<Box<dyn Proc>>>;

pub struct Supervisor {
    pub children: Vec<Child>,
    pub dir: PathBuf,
    spawn: Spawn,
    pub now: Box<dyn Fn() -> f64>,
}

impl Supervisor {
    pub fn new(children: Vec<Child>, dir: &Path, spawn: Spawn) -> Self {
        let t0 = Instant::now();
        Self { children, dir: dir.into(), spawn, now: Box::new(move || t0.elapsed().as_secs_f64()) }
    }

    pub fn step(&mut self) -> Result<()> {
        let t = (self.now)();
        for i in 0..self.children.len() {
            let c = &mut self.children[i];
            if let Some(code) = c.proc.as_mut().and_then(|p| p.poll()) {
                let ran = t - c.started;
                let wait = if ran >= STEADY_S { BACKOFF.0 } else { c.wait };
                (c.proc, c.last_exit, c.due, c.wait) = (None, Some(code), t + wait, (wait * 2.0).min(BACKOFF.1));
                let rec = json!({"code": code, "ran_s": ran.round(), "restart_in_s": wait});
                self.event(i, "exit", rec)?;
            }
            let c = &mut self.children[i];
            if c.proc.is_none() && t >= c.due {
                match (self.spawn)(&c.spec, &self.dir.join(format!("{}.log", c.spec.name))) {
                    Ok(p) => {
                        let rec = json!({"pid": p.pid(), "start": c.starts + 1});
                        (c.proc, c.started, c.starts) = (Some(p), t, c.starts + 1);
                        self.event(i, "start", rec)?;
                    }
                    Err(e) => {
                        (c.due, c.wait) = (t + c.wait, (c.wait * 2.0).min(BACKOFF.1));
                        self.event(i, "spawn_failed", json!({"error": e.to_string().chars().take(300).collect::<String>()}))?;
                    }
                }
            }
        }
        self.save()
    }

    /// Kill every worker. ponytail: no graceful SIGINT (that needs a signal crate); a killed day worker's
    /// activity times out on its heartbeat and is retried, and the reservation keeps the desktop meanwhile.
    pub fn stop(&mut self) {
        for i in 0..self.children.len() {
            if let Some(mut p) = self.children[i].proc.take() {
                p.kill();
                let _ = self.event(i, "stopped", json!({}));
            }
        }
        let _ = self.save();
    }

    fn event(&self, i: usize, event: &str, rec: Value) -> Result<()> {
        let at = chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string();
        let name = &self.children[i].spec.name;
        let mut line = json!({"at": at, "worker": name, "event": event});
        line.as_object_mut().unwrap().extend(rec.as_object().cloned().unwrap_or_default());
        std::fs::create_dir_all(&self.dir)?;
        writeln!(std::fs::OpenOptions::new().create(true).append(true).open(self.dir.join("supervisor.jsonl"))?, "{line}")?;
        println!("{at} {name}: {event} {}", if rec.as_object().is_some_and(|o| o.is_empty()) { String::new() } else { rec.to_string() });
        Ok(())
    }

    fn save(&self) -> Result<()> {
        let workers: serde_json::Map<String, Value> =
            self.children.iter().map(|c| (c.spec.name.clone(), json!({"pid": c.proc.as_ref().map(|p| p.pid()), "starts": c.starts, "last_exit": c.last_exit}))).collect();
        std::fs::create_dir_all(&self.dir)?;
        let tmp = self.dir.join("state.json.tmp");
        std::fs::write(&tmp, json!({"updated": chrono::Local::now().format("%Y-%m-%dT%H:%M:%S").to_string(), "workers": workers}).to_string())?;
        std::fs::rename(&tmp, self.dir.join("state.json"))?;
        Ok(())
    }
}

/// Run the supervisor until Ctrl-C or SIGTERM, then stop every worker.
pub fn serve(mut sup: Supervisor) -> Result<()> {
    let rt = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
    let r = rt.block_on(async {
        #[cfg(unix)]
        let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        loop {
            sup.step()?;
            #[cfg(unix)]
            let term = term.recv();
            #[cfg(not(unix))]
            let term = std::future::pending::<Option<()>>();
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_secs(5)) => {}
                _ = tokio::signal::ctrl_c() => return anyhow::Ok(()),
                _ = term => return Ok(()),
            }
        }
    });
    sup.stop();
    r
}

fn failures(log: &Path) -> String {
    match std::fs::read(log) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(_) => ", log unreadable".into(),
        Ok(b) => match String::from_utf8_lossy(&b).matches(FAILED).count() {
            0 => String::new(),
            n => format!(", {n} failed activities"),
        },
    }
}

pub fn summary(world: &World, users: &[String], day: NaiveDate, state_dir: &Path, env: &dyn Fn(&str) -> String) -> String {
    let state: Value = std::fs::read_to_string(state_dir.join("state.json")).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(json!({}));
    let workers = &state["workers"];
    let caps = CAPS[2..].iter().map(|k| Some(env(k)).filter(|v| !v.is_empty()).unwrap_or("-".into())).collect::<Vec<_>>().join("/");
    let pid = |w: &Value| w["pid"].as_u64().filter(|p| *p != 0);
    let plan = pid(&workers["plan"]).map_or("down".into(), |p| format!("pid {p}"));
    let updated = state["updated"].as_str().unwrap_or("none");
    let mut lines = vec![
        format!("range {day}  supervisor state {updated}  plan worker {plan}{}", failures(&state_dir.join("plan.log"))),
        format!("{:10} {:30} {:34} {:>4} {:>5} {:>12} (cap {caps})", "persona", "worker", "current task", "done", "aband", "decis/llm"),
    ];
    for u in users {
        let (mut current, mut done, mut abandoned) = ("-".to_string(), 0, 0);
        let text = std::fs::read_to_string(world.day_log(u, &day.to_string())).unwrap_or_default();
        for e in text.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok()) {
            match e["event"].as_str() {
                Some("item_start") => current = format!("{} {}", crate::timeline::text(e.get("at")), crate::timeline::text(e.get("template"))),
                Some("item_end") => {
                    current = "-".into();
                    if e["status"] == "done" { done += 1 } else { abandoned += 1 }
                }
                Some("day_end") => current = "(day ended)".into(),
                _ => {}
            }
        }
        let w = &workers[u.as_str()];
        let mut worker = match pid(w) {
            Some(p) => format!("pid {p}"),
            None if w.is_object() => "down".into(),
            None => "-".into(),
        };
        if let Some(n) = w["starts"].as_u64().filter(|n| *n > 1) {
            worker += &format!(", {n} starts");
        }
        worker += &failures(&state_dir.join(format!("{u}.log")));
        let (decision, llm) = crate::calls::persona_day(u, Some(day));
        let current: String = current.chars().take(34).collect();
        lines.push(format!("{u:10} {worker:30} {current:34} {done:>4} {abandoned:>5} {:>12}", format!("{decision}/{llm}")));
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::rc::Rc;

    struct Fake(Rc<RefCell<Option<i32>>>);
    impl Proc for Fake {
        fn pid(&self) -> u32 {
            7
        }
        fn poll(&mut self) -> Option<i32> {
            *self.0.borrow()
        }
        fn kill(&mut self) {
            *self.0.borrow_mut() = Some(-9);
        }
    }

    #[test]
    fn a_dying_worker_is_restarted_with_backoff_and_the_others_are_untouched() {
        let dir = std::env::temp_dir().join(format!("ludius-rangerun-{}", std::process::id()));
        let exits: Rc<RefCell<Vec<(String, Rc<RefCell<Option<i32>>>)>>> = Rc::default();
        let seen = exits.clone();
        let spawn: Spawn = Box::new(move |s, _| {
            let code = Rc::new(RefCell::new(None));
            seen.borrow_mut().push((s.name.clone(), code.clone()));
            Ok(Box::new(Fake(code)))
        });
        let env = |k: &str| if k == "LUDIUS_PERSONA_PASSWORD" { "pw".to_string() } else { String::new() };
        let specs = specs(&["aberg".into()], "a:1", "t:2", "m", &env);
        assert_eq!(specs[1].args, ["--agent", "a:1", "--vm", "ludius-aberg", "worker", "--temporal", "t:2", "--persona", "aberg", "--model", "m"]);
        assert!(specs[1].env.contains(&("LUDIUS_PERSONA_PASSWORD".into(), "pw".into())));
        let clock = Rc::new(RefCell::new(0.0));
        let mut sup = Supervisor::new(specs.into_iter().map(Child::new).collect(), &dir, spawn);
        let now = clock.clone();
        sup.now = Box::new(move || *now.borrow());
        sup.step().unwrap();
        assert_eq!(exits.borrow().len(), 2);
        *exits.borrow()[1].1.borrow_mut() = Some(1);
        *clock.borrow_mut() = 10.0;
        sup.step().unwrap(); // exit seen, restart due at 15
        assert_eq!(exits.borrow().len(), 2);
        *clock.borrow_mut() = 15.0;
        sup.step().unwrap();
        assert_eq!(exits.borrow().len(), 3);
        assert_eq!((sup.children[0].starts, sup.children[1].starts, sup.children[1].last_exit), (1, 2, Some(1)));
        *exits.borrow()[2].1.borrow_mut() = Some(1);
        *clock.borrow_mut() = 16.0;
        sup.step().unwrap(); // second quick death doubles the wait
        *clock.borrow_mut() = 25.0;
        sup.step().unwrap();
        assert_eq!(exits.borrow().len(), 3);
        *clock.borrow_mut() = 26.0;
        sup.step().unwrap();
        assert_eq!(exits.borrow().len(), 4);
        sup.stop();
        let state: Value = serde_json::from_str(&std::fs::read_to_string(dir.join("state.json")).unwrap()).unwrap();
        assert_eq!(state["workers"]["aberg"]["starts"], 3);
        assert!(refusals(&["aberg".into()], &env, &dir).len() == 5, "4 caps and the persona file");
    }
}
