//! OpenRouter request counters and caps (shared with the Python controller: same files, same lock).
use std::cell::RefCell;
use std::path::PathBuf;

use anyhow::{Result, anyhow, bail};
use chrono::{NaiveDate, SecondsFormat, Utc};
use fs2::FileExt;
use serde_json::{Map, Value, json};

use crate::paths;

#[derive(Debug)]
pub struct CapReached(pub String);

impl std::fmt::Display for CapReached {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for CapReached {}

thread_local! {
    static PERSONA: RefCell<Option<String>> = const { RefCell::new(None) };
    static DAY: RefCell<Option<NaiveDate>> = const { RefCell::new(None) };
}

pub fn path() -> PathBuf {
    paths::runs().join("openrouter-calls.json")
}

/// Count calls made by `f` against `user`'s per-persona counter.
pub fn for_persona<T>(user: &str, f: impl FnOnce() -> T) -> T {
    PERSONA.with(|p| *p.borrow_mut() = Some(user.to_string()));
    let r = f();
    PERSONA.with(|p| *p.borrow_mut() = None);
    r
}

pub fn on_day<T>(day: NaiveDate, f: impl FnOnce() -> T) -> T {
    let old = DAY.with(|d| d.borrow_mut().replace(day));
    let r = f();
    DAY.with(|d| *d.borrow_mut() = old);
    r
}

pub fn today() -> NaiveDate {
    DAY.with(|d| *d.borrow()).unwrap_or_else(|| chrono::Local::now().date_naive())
}

fn who() -> Option<String> {
    PERSONA.with(|p| p.borrow().clone()).or_else(|| std::env::var("LUDIUS_PERSONA").ok().filter(|s| !s.is_empty()))
}

/// Count one request of `kind` ("decision" or "llm"). With a cap set, an unusable counter fails closed.
pub fn count(kind: &str) -> Result<()> {
    let up = kind.to_uppercase();
    let cap = std::env::var(format!("LUDIUS_CAP_{up}")).ok();
    let who = who();
    let mine = who.as_ref().and_then(|_| std::env::var(format!("LUDIUS_CAP_PERSONA_{up}")).ok());
    match count_inner(kind, cap.as_deref(), who.as_deref(), mine.as_deref()) {
        Ok(()) => Ok(()),
        Err(e) if e.is::<CapReached>() => Err(e),
        Err(e) if cap.is_some() || mine.is_some() => Err(CapReached(format!("{kind} counter unusable: {e}")).into()),
        Err(_) => Ok(()),
    }
}

fn count_inner(kind: &str, cap: Option<&str>, who: Option<&str>, mine: Option<&str>) -> Result<()> {
    let path = path();
    std::fs::create_dir_all(path.parent().unwrap())?;
    let lock = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(path.with_extension("lock"))?;
    lock.lock_exclusive()?;
    let mut old: Map<String, Value> = match std::fs::read_to_string(&path) {
        Ok(t) => match serde_json::from_str(&t) {
            Ok(v) => v,
            Err(e) if cap.is_some() => return Err(e.into()),
            Err(_) => Map::new(),
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            if cap.is_some() && path.parent().unwrap().join("calls").exists() {
                return Err(e.into()); // lost mid-run: fail closed
            }
            Map::from_iter([("decision".into(), json!(0)), ("llm".into(), json!(0))])
        }
        Err(e) if cap.is_some() => return Err(e.into()),
        Err(_) => Map::new(),
    };
    if !old.contains_key("decision")
        && let Some(v) = old.remove("jev")
    {
        old.insert("decision".into(), v);
    }
    if let Some(cap) = cap {
        let total = old.get(kind).and_then(Value::as_i64).filter(|t| *t >= 0).ok_or_else(|| anyhow!("{kind} total is not a count"))?;
        if total >= cap.parse::<i64>()? {
            bail!(CapReached(format!("{kind} request cap LUDIUS_CAP_{}={cap} reached; nothing sent", kind.to_uppercase())));
        }
    }
    if let Some(who) = who {
        persona_count(who, kind, mine)?;
    }
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Secs, false);
    let n = |k: &str| old.get(k).and_then(Value::as_i64).unwrap_or(0) + i64::from(kind == k);
    let data = json!({
        "decision": n("decision"),
        "llm": n("llm"),
        "since": old.get("since").filter(|v| v.as_str().is_some_and(|s| !s.is_empty())).cloned().unwrap_or(json!(now)),
        "updated": now,
    });
    let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
    std::fs::write(&tmp, data.to_string())?;
    std::fs::rename(&tmp, &path)?;
    Ok(())
}

pub fn persona_path(user: &str) -> PathBuf {
    path().parent().unwrap().join("calls").join(format!("{user}.json"))
}

pub fn persona_day(user: &str, on: Option<NaiveDate>) -> (i64, i64) {
    let data: Map<String, Value> =
        std::fs::read_to_string(persona_path(user)).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    let got = data.get(&on.unwrap_or_else(today).to_string());
    let n = |k: &str| got.and_then(|g| g.get(k)).and_then(Value::as_i64).unwrap_or(0);
    (n("decision"), n("llm"))
}

fn persona_count(user: &str, kind: &str, cap: Option<&str>) -> Result<()> {
    let path = persona_path(user);
    let mut data: Map<String, Value> = match std::fs::read_to_string(&path) {
        Ok(t) => match serde_json::from_str(&t) {
            Ok(v) => v,
            Err(e) if cap.is_some() => return Err(e.into()),
            Err(_) => Map::new(),
        },
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Map::new(),
        Err(e) if cap.is_some() => return Err(e.into()),
        Err(_) => Map::new(),
    };
    let key = today().to_string();
    let mut counts = Map::from_iter([("decision".into(), json!(0)), ("llm".into(), json!(0))]);
    if let Some(Value::Object(o)) = data.get(&key) {
        counts.extend(o.clone());
    }
    let n = counts.get(kind).and_then(Value::as_i64).unwrap_or(0);
    if let Some(cap) = cap
        && n >= cap.parse::<i64>()?
    {
        bail!(CapReached(format!(
            "{user}: {kind} cap LUDIUS_CAP_PERSONA_{}={cap} for {key} reached; nothing sent",
            kind.to_uppercase()
        )));
    }
    counts.insert(kind.into(), json!(n + 1));
    data.insert(key, Value::Object(counts));
    std::fs::create_dir_all(path.parent().unwrap())?;
    let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
    std::fs::write(&tmp, Value::Object(data).to_string())?;
    std::fs::rename(&tmp, &path)?;
    Ok(())
}
