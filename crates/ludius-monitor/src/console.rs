//! Console data: the company narrative, personas, plans and day recordings, the warp journal and the
//! admission log. Every source is read-only; an unreadable or malformed source is a 503, never empty.
use crate::{App, now_ms, problem};
use axum::Router;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use chrono::{DateTime, FixedOffset, Local, NaiveDate, NaiveDateTime, TimeZone};
use regex::Regex;
use serde_json::{Map, Value, json};
use std::collections::HashMap;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path as FsPath, PathBuf};
use std::sync::LazyLock;
use std::time::UNIX_EPOCH;

const MAX_READ: u64 = 16 << 20;

pub type Obj = Map<String, Value>;

/// An unreadable or malformed source.
#[derive(Debug)]
pub struct Bad;

pub type Rd<T> = Result<T, Bad>;

pub static USER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[a-z][a-z0-9_-]{0,63}$").unwrap());
static DAY_FILE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(\d{4}-\d{2}-\d{2})-([a-z][a-z0-9_-]{0,63})$").unwrap());

const PERSON: &[&str] = &["user", "name", "role", "department", "manager"];
const TRAIT: &[&str] = &["user", "name", "role", "department", "hours", "style", "risk_profile", "tz", "typing_mean_ms", "typing_jitter_ms", "think_mean_s", "pointer_ms"];
const ITEM: &[&str] = &["at", "template", "context", "minutes", "interrupt", "handoff"];
const EVENT: &[&str] = &["event", "host_ms", "logical", "at", "template", "task", "status", "reason", "subgoal", "steps", "by", "clock", "done", "items"];
const TEMPLATE: &[&str] = &["task", "app", "capability", "about", "params", "budget_minutes"];

pub fn routes() -> Router<App> {
    Router::new()
        .route("/templates", get(|State(a): State<App>| async move { read(|| templates(&a.cfg.templates)) }))
        .route("/console", get(|State(a): State<App>| async move { read(|| catalog(&a.cfg.runs, &a.cfg.personas)) }))
        .route("/run", get(|State(a): State<App>| async move { read(|| run(&a.cfg.runs, now_ms())) }))
        .route(
            "/daybook/{date}/{user}",
            get(|State(a): State<App>, Path((date, user)): Path<(String, String)>| async move {
                if !valid_date(&date) || !USER.is_match(&user) {
                    return StatusCode::BAD_REQUEST.into_response();
                }
                read(|| day(&a.cfg.runs, &date, &user).map(|d| d.json()))
            }),
        )
}

fn read(f: impl FnOnce() -> Rd<Value>) -> Response {
    match f() {
        Ok(v) => axum::Json(v).into_response(),
        Err(Bad) => problem(StatusCode::SERVICE_UNAVAILABLE, "Console source is unreadable or malformed"),
    }
}

pub fn valid_date(s: &str) -> bool {
    s.len() == 10 && s.as_bytes()[4] == b'-' && s.as_bytes()[7] == b'-' && NaiveDate::parse_from_str(s, "%Y-%m-%d").is_ok()
}

pub fn text<'a>(o: &'a Obj, k: &str) -> &'a str {
    o.get(k).and_then(Value::as_str).unwrap_or("")
}

fn pick(root: &Obj, fields: &[&str]) -> Obj {
    fields.iter().filter_map(|k| root.get(*k).filter(|v| !v.is_null()).map(|v| (k.to_string(), v.clone()))).collect()
}

fn project(root: &Obj, key: &str, fields: &[&str]) -> Rd<Value> {
    match root.get(key) {
        None | Some(Value::Null) => Ok(json!([])),
        Some(Value::Array(list)) => list.iter().map(|n| n.as_object().map(|o| Value::Object(pick(o, fields))).ok_or(Bad)).collect::<Rd<_>>().map(Value::Array),
        _ => Err(Bad),
    }
}

/// The file's text, None if it does not exist; too large or unreadable is Bad.
fn file_text(path: &FsPath) -> Rd<Option<String>> {
    let f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(Bad),
    };
    let mut text = String::new();
    f.take(MAX_READ + 1).read_to_string(&mut text).map_err(|_| Bad)?;
    if text.len() as u64 > MAX_READ {
        return Err(Bad);
    }
    Ok(Some(text))
}

/// YAML as JSON with every scalar a string (as the console has always seen it).
fn yaml(path: &FsPath) -> Rd<Option<Obj>> {
    let Some(text) = file_text(path)? else { return Ok(None) };
    match serde_yaml::from_str::<serde_yaml::Value>(&text).map(convert) {
        Ok(Value::Object(o)) => Ok(Some(o)),
        _ => Err(Bad),
    }
}

fn convert(v: serde_yaml::Value) -> Value {
    use serde_yaml::Value as Y;
    match v {
        Y::Null => Value::String(String::new()),
        Y::Bool(b) => Value::String(b.to_string()),
        Y::Number(n) => Value::String(n.to_string()),
        Y::String(s) => Value::String(s),
        Y::Sequence(s) => Value::Array(s.into_iter().map(convert).collect()),
        Y::Mapping(m) => Value::Object(
            m.into_iter()
                .map(|(k, v)| {
                    let k = match convert(k) {
                        Value::String(s) => s,
                        other => other.to_string(),
                    };
                    (k, convert(v))
                })
                .collect(),
        ),
        Y::Tagged(t) => convert(t.value),
    }
}

/// JSON lines; a last line without its newline is still being written and is skipped.
fn lines(path: &FsPath) -> Rd<Option<Vec<Obj>>> {
    let Some(text) = file_text(path)? else { return Ok(None) };
    let parts: Vec<&str> = text.split('\n').collect();
    let mut out = Vec::new();
    for (i, line) in parts.iter().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        if i == parts.len() - 1 && !text.ends_with('\n') {
            break;
        }
        match serde_json::from_str::<Value>(line) {
            Ok(Value::Object(o)) => out.push(o),
            _ => return Err(Bad),
        }
    }
    Ok(Some(out))
}

/// The last `count` parseable records of a JSON-lines file (from its last 64 KB).
pub fn last_lines(path: &FsPath, count: usize) -> Vec<Value> {
    let Ok(mut f) = std::fs::File::open(path) else { return vec![] };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let start = len.saturating_sub(64 << 10);
    let mut buf = Vec::new();
    if f.seek(SeekFrom::Start(start)).is_err() || f.read_to_end(&mut buf).is_err() {
        return vec![];
    }
    let text = String::from_utf8_lossy(&buf);
    let mut parts: Vec<&str> = text.split('\n').collect();
    if start > 0 && !parts.is_empty() {
        parts.remove(0);
    }
    if !text.ends_with('\n') {
        parts.pop();
    }
    let records: Vec<Value> = parts.iter().filter_map(|l| serde_json::from_str::<Value>(l).ok().filter(Value::is_object)).collect();
    records[records.len().saturating_sub(count)..].to_vec()
}

fn yaml_files(dir: &FsPath) -> Rd<Vec<PathBuf>> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(dir)
        .map_err(|_| Bad)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "yaml"))
        .collect();
    files.sort();
    Ok(files)
}

fn templates(dir: &FsPath) -> Rd<Value> {
    yaml_files(dir)?.iter().map(|p| yaml(p)?.map(|o| Value::Object(pick(&o, TEMPLATE))).ok_or(Bad)).collect::<Rd<_>>().map(Value::Array)
}

fn mtime(path: &FsPath) -> std::time::SystemTime {
    std::fs::metadata(path).and_then(|m| m.modified()).unwrap_or(UNIX_EPOCH)
}

pub struct Day {
    date: String,
    user: String,
    plan: Option<Value>,
    events: Option<Vec<Obj>>,
}

impl Day {
    fn json(&self) -> Value {
        json!({
            "date": self.date,
            "user": self.user,
            "plan": self.plan,
            "events": self.events,
            "sources": {
                "plan": if self.plan.is_some() { "ok" } else { "missing" },
                "recording": if self.events.is_some() { "ok" } else { "missing" },
            },
        })
    }
}

pub fn day(runs: &FsPath, date: &str, user: &str) -> Rd<Day> {
    if !valid_date(date) || !USER.is_match(user) {
        return Err(Bad);
    }
    let stem = format!("{date}-{user}");
    let plan = yaml(&runs.join("plans").join(format!("{stem}.yaml")))?;
    if let Some(p) = &plan
        && (text(p, "persona") != user || text(p, "date") != date)
    {
        return Err(Bad);
    }
    let events = lines(&runs.join("days").join(format!("{stem}.jsonl")))?;
    Ok(Day {
        date: date.into(),
        user: user.into(),
        plan: plan.map(|p| project(&p, "items", ITEM)).transpose()?,
        events: events.map(|e| e.iter().map(|x| pick(x, EVENT)).collect()),
    })
}

pub fn catalog(runs: &FsPath, persona_dir: &FsPath) -> Rd<Value> {
    let narrative = yaml(&runs.join("world").join("narrative.yaml"))?;
    let company = match &narrative {
        Some(n) => {
            let mut c = pick(n, &["company", "city", "summary", "projects", "revised"]);
            c.insert("people".into(), project(n, "people", PERSON)?);
            c.insert("events".into(), project(n, "events", &["date", "what"])?);
            c.insert("history".into(), project(n, "history", &["date", "what"])?);
            Some(c)
        }
        None => None,
    };
    let people: Option<Vec<Obj>> = if persona_dir.is_dir() {
        let people = yaml_files(persona_dir)?.iter().map(|p| yaml(p)?.map(|o| pick(&o, TRAIT)).ok_or(Bad)).collect::<Rd<Vec<_>>>()?;
        if people.iter().any(|p| !USER.is_match(text(p, "user"))) {
            return Err(Bad);
        }
        Some(people)
    } else {
        None
    };
    let mut index: Vec<(String, String, &str)> = vec![];
    for folder in ["plans", "days"] {
        let Ok(dir) = std::fs::read_dir(runs.join(folder)) else { continue };
        let ext = if folder == "plans" { "yaml" } else { "jsonl" };
        for p in dir.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|x| x == ext)) {
            let stem = p.file_stem().unwrap_or_default().to_string_lossy().into_owned();
            if let Some(m) = DAY_FILE.captures(&stem)
                && valid_date(&m[1])
            {
                index.push((m[1].to_string(), m[2].to_string(), folder));
            }
        }
    }
    let written = |date: &str, user: &str, source: &str| {
        let ext = if source == "plans" { "yaml" } else { "jsonl" };
        mtime(&runs.join(source).join(format!("{date}-{user}.{ext}")))
    };
    let latest = match &people {
        None => None,
        Some(people) => Some(
            people
                .iter()
                .map(|p| {
                    let user = text(p, "user");
                    // Most recently written, not the highest date: a warp can play a day before an earlier warp's.
                    let newest = |days_only: bool| {
                        index
                            .iter()
                            .filter(|(_, u, s)| u == user && (!days_only || *s == "days"))
                            .max_by_key(|(d, u, s)| written(d, u, s))
                            .map(|(d, _, _)| d.clone())
                    };
                    let date = newest(true).or_else(|| newest(false));
                    let day = date.as_deref().map(|d| day(runs, d, user)).transpose()?;
                    let events = day.as_ref().and_then(|d| d.events.as_deref());
                    let since: Option<&[Obj]> = events.map(|e| {
                        let start = e.iter().rposition(|x| text(x, "event") == "day_start").unwrap_or(0);
                        &e[start..]
                    });
                    let ends: Option<Vec<&Obj>> = since.map(|s| s.iter().filter(|x| text(x, "event") == "item_end").collect());
                    let plan = day.as_ref().and_then(|d| d.plan.as_ref()).and_then(Value::as_array);
                    let next = plan.and_then(|items| {
                        items.iter().filter_map(Value::as_object).find(|item| {
                            !ends.as_ref().is_some_and(|ends| ends.iter().any(|e| text(e, "at") == text(item, "at") && text(e, "template") == text(item, "template")))
                        })
                    });
                    let reacting = since.and_then(|s| {
                        let i = s.iter().rposition(|x| text(x, "event") == "reaction_start")?;
                        if s[i..].iter().any(|x| text(x, "event") == "reaction_end") {
                            return None;
                        }
                        let at = text(&s[i], "logical").rsplit('T').next().unwrap_or("");
                        Some(json!({ "at": at, "template": text(&s[i], "template"), "context": text(&s[i], "context") }))
                    });
                    let count = |status: &str| ends.as_ref().map(|e| e.iter().filter(|x| text(x, "status") == status).count());
                    Ok(json!({
                        "user": user, "date": date, "done": count("done"), "abandoned": count("abandoned"),
                        "next": next, "reacting": reacting,
                    }))
                })
                .collect::<Rd<Vec<Value>>>()?,
        ),
    };
    let ok = |b: bool| if b { "ok" } else { "missing" };
    Ok(json!({
        "company": company,
        "people": people,
        "index": index.iter().map(|(d, u, s)| json!({ "date": d, "user": u, "source": s })).collect::<Vec<_>>(),
        "latest": latest,
        "sources": {
            "narrative": ok(company.is_some()), "people": ok(people.is_some()),
            "plans": ok(runs.join("plans").is_dir()), "days": ok(runs.join("days").is_dir()),
        },
    }))
}

/// An ISO time; without an offset it is the box's local time (the journal writes naive local times).
fn parse_time(s: &str) -> Option<DateTime<FixedOffset>> {
    if let Ok(t) = DateTime::parse_from_rfc3339(s) {
        return Some(t);
    }
    ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S%.f"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(s, f).ok())
        .and_then(|n| Local.from_local_datetime(&n).earliest())
        .map(|t| t.fixed_offset())
}

/// The warp journal: phase, day, the range clock now, and per-persona progress.
pub fn run(runs: &FsPath, now: i64) -> Rd<Value> {
    let journal = match lines(&runs.join("range").join("warp.jsonl"))? {
        Some(j) if !j.is_empty() => j,
        _ => return Ok(json!({ "phase": "none" })),
    };
    let (mut phase, mut date, mut error) = ("none", String::new(), String::new());
    let mut logical: Option<String> = None;
    let (mut set_at, mut last, mut day, mut planned) = (0i64, 0i64, 0, false);
    let (mut users, mut unplanned): (Vec<String>, Vec<String>) = (vec![], vec![]);
    let mut results: HashMap<String, (&str, i64)> = HashMap::new();
    for e in &journal {
        last = parse_time(text(e, "at")).map_or(0, |t| t.timestamp_millis());
        let persona = text(e, "persona");
        match text(e, "event") {
            "clock_set" => {
                if matches!(phase, "finished" | "stopped" | "failed") {
                    (day, planned, error) = (0, false, String::new());
                }
                (logical, set_at) = (Some(text(e, "logical").to_string()), last);
                if planned {
                    (phase, planned) = ("preparing", false);
                } else {
                    (phase, date) = ("planning", String::new());
                    day += 1;
                    users.clear();
                    unplanned.clear();
                    results.clear();
                }
            }
            "no_plan" if USER.is_match(persona) => unplanned.push(persona.into()),
            "planned" => {
                date = if valid_date(text(e, "date")) { text(e, "date").into() } else { String::new() };
                users = e
                    .get("items")
                    .and_then(Value::as_object)
                    .map(|items| items.iter().filter(|(k, v)| USER.is_match(k) && v.as_i64().is_some_and(|n| n > 0)).map(|(k, _)| k.clone()).collect())
                    .unwrap_or_default();
                planned = !users.is_empty();
            }
            "day" if USER.is_match(persona) => {
                results.insert(persona.into(), (if e.get("result").is_some_and(Value::is_array) { "done" } else { "failed" }, last));
            }
            "interrupted" => phase = "stopped",
            "warp_failed" | "clock_reset_failed" => {
                phase = "failed";
                error = text(e, "error").into();
            }
            "clock_reset" => {
                if !matches!(phase, "stopped" | "failed") {
                    phase = "finished";
                }
                logical = None;
            }
            _ => {}
        }
    }
    let personas: Vec<Value> = users
        .iter()
        .map(|user| {
            let log = if date.is_empty() || phase == "planning" { None } else { lines(&runs.join("days").join(format!("{date}-{user}.jsonl")))? };
            let run: &[Obj] = match &log {
                Some(l) => match l.iter().rposition(|x| text(x, "event") == "day_start") {
                    Some(i) => &l[i..],
                    None => &[],
                },
                None => &[],
            };
            let started = run.first().and_then(|x| x.get("host_ms")).and_then(Value::as_i64).unwrap_or(0);
            let has = |ev: &str| run.iter().any(|x| text(x, "event") == ev);
            let status = match results.get(user) {
                Some((s, at)) if started <= *at => *s,
                _ if has("day_aborted") => "failed",
                _ if has("day_end") => "done",
                _ if !run.is_empty() => "running",
                _ => "waiting",
            };
            let items = run.first().and_then(|x| x.get("items")).and_then(Value::as_i64).unwrap_or(0);
            Ok(json!({ "user": user, "status": status, "items": items, "done": run.iter().filter(|x| text(x, "event") == "item_end").count() }))
        })
        .collect::<Rd<_>>()?;
    if phase == "preparing" && personas.iter().any(|p| p["status"] != "waiting") {
        phase = "running";
    }
    let active = matches!(phase, "planning" | "preparing" | "running");
    let l = logical.as_deref().and_then(parse_time);
    let clock = l.filter(|_| active).map(|l| json!({ "ms": l.timestamp_millis() + (now - set_at), "offset": l.offset().local_minus_utc() / 60 }));
    let plans = match l {
        Some(eve) if phase == "planning" => {
            let next = (eve.date_naive() + chrono::Days::new(1)).format("%Y-%m-%d").to_string();
            std::fs::read_dir(runs.join("plans"))
                .map(|d| {
                    d.filter_map(|e| e.ok().map(|e| e.path()))
                        .filter(|p| {
                            let name = p.file_name().unwrap_or_default().to_string_lossy();
                            name.starts_with(&format!("{next}-")) && name.ends_with(".yaml")
                        })
                        .filter(|p| mtime(p).duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64) >= set_at)
                        .count()
                })
                .unwrap_or(0)
        }
        _ => 0,
    };
    Ok(json!({ "phase": phase, "day": day, "date": date, "ageMs": (now - last).max(0), "clock": clock, "unplanned": unplanned, "plans": plans, "personas": personas, "error": error }))
}

// --- admission ----------------------------------------------------------------------------------

static ADMISSION_LOG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\d{8}-\d{6}-([a-z0-9_]{1,64})\.jsonl$").unwrap());
static TOKEN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_.:+-]{1,64}$").unwrap());
const MAX_ADMISSION_READ: u64 = 8 << 20;
const ACTIVE_MS: i64 = 90_000;

fn tok(e: &Obj, k: &str) -> Option<String> {
    let s = match e.get(k)? {
        Value::String(s) => s.clone(),
        Value::Number(n) => n.to_string(),
        _ => return None,
    };
    TOKEN.is_match(&s).then_some(s)
}

/// The newest admission log, summarised for the console.
pub fn admission(dir: &FsPath, now: i64) -> Value {
    let newest = std::fs::read_dir(dir).ok().and_then(|d| {
        d.filter_map(|e| e.ok().map(|e| e.file_name().to_string_lossy().into_owned())).filter(|n| ADMISSION_LOG.is_match(n)).max()
    });
    let Some(name) = newest else { return json!({ "state": "none" }) };
    let path = dir.join(&name);
    let Ok(mut f) = std::fs::File::open(&path) else { return json!({ "state": "none" }) };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let truncated = len > MAX_ADMISSION_READ;
    if truncated && f.seek(SeekFrom::End(-(MAX_ADMISSION_READ as i64))).is_err() {
        return json!({ "state": "none" });
    }
    let mut buf = Vec::new();
    let _ = f.read_to_end(&mut buf);
    let text = String::from_utf8_lossy(&buf);

    let mut last: Option<i64> = None;
    let [mut variant, mut subgoal, mut action, mut result, mut status]: [Option<String>; 5] = Default::default();
    let (mut run, mut step, mut done, mut failed, mut run_done, mut run_failed) = (0, 0i64, 0, 0, 0, 0);
    let (mut in_run, mut ended, mut per_run, mut stopped) = (false, false, false, false);
    let mut admitted: Option<bool> = None;
    let mut runs: HashMap<String, i32> = HashMap::new();
    for line in text.lines() {
        let Ok(Value::Object(e)) = serde_json::from_str::<Value>(line) else { continue };
        if let Some(t) = e.get("host_ms").and_then(Value::as_i64) {
            last = Some(t);
        }
        match tok(&e, "event").as_deref() {
            Some("task_start") => {
                variant = tok(&e, "variant");
                let n = runs.entry(variant.clone().unwrap_or_default()).or_default();
                *n += 1;
                run = *n;
                (in_run, step, subgoal, action, result, status) = (true, 0, None, None, None, None);
            }
            Some("step") => {
                subgoal = tok(&e, "subgoal");
                action = tok(&e, "action");
                result = tok(&e, "result");
                step = tok(&e, "step").and_then(|s| s.parse().ok()).unwrap_or(step);
            }
            Some("task_end") => {
                status = tok(&e, "status");
                if status.as_deref() == Some("done") {
                    done += 1;
                } else {
                    failed += 1;
                }
                in_run = false;
            }
            Some("admission_run") => {
                per_run = true;
                if tok(&e, "status").as_deref() == Some("done") {
                    run_done += 1;
                } else {
                    run_failed += 1;
                }
                in_run = false;
            }
            Some("admission_end") => {
                (ended, in_run) = (true, false);
                stopped = e.get("stopped").is_some_and(Value::is_string);
                admitted = e.get("admitted").and_then(Value::as_bool);
            }
            _ => {}
        }
    }
    if per_run {
        (done, failed) = (run_done, run_failed);
    }
    let written = mtime(&path).duration_since(UNIX_EPOCH).map_or(0, |d| d.as_millis() as i64);
    let age = now - last.unwrap_or(written);
    let state = if ended {
        "completed"
    } else if age < ACTIVE_MS {
        "running"
    } else if in_run {
        "stale"
    } else {
        "idle"
    };
    let task = ADMISSION_LOG.captures(&name).map(|m| m[1].to_string()).unwrap_or_default();
    json!({
        "state": state, "task": task, "variant": variant, "run": run, "inRun": in_run, "step": step, "subgoal": subgoal,
        "action": action, "result": result, "status": status, "done": done, "failed": failed, "admitted": admitted,
        "stopped": stopped, "ageMs": age.max(0), "truncated": truncated,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("ludius-monitor-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn dates_and_yaml_scalars() {
        assert!(valid_date("2027-02-03") && !valid_date("2027-2-03") && !valid_date("2027-02-30"));
        let d = tmp("yaml");
        std::fs::write(d.join("a.yaml"), "task: x\nbudget_minutes: 30\nparams: [1, true]\n").unwrap();
        let o = yaml(&d.join("a.yaml")).unwrap().unwrap();
        assert_eq!(o["budget_minutes"], "30");
        assert_eq!(o["params"], json!(["1", "true"]));
        std::fs::write(d.join("b.yaml"), "- not a mapping\n").unwrap();
        assert!(yaml(&d.join("b.yaml")).is_err());
    }

    #[test]
    fn day_and_catalog() {
        let runs = tmp("runs");
        let personas = runs.join("personas");
        for sub in ["plans", "days", "world"] {
            std::fs::create_dir_all(runs.join(sub)).unwrap();
        }
        std::fs::create_dir_all(&personas).unwrap();
        std::fs::write(personas.join("aberg.yaml"), "user: aberg\nname: A Berg\n").unwrap();
        std::fs::write(runs.join("plans/2027-02-03-aberg.yaml"), "persona: aberg\ndate: 2027-02-03\nitems:\n- {at: '09:00', template: t1}\n- {at: '10:00', template: t2}\n").unwrap();
        std::fs::write(
            runs.join("days/2027-02-03-aberg.jsonl"),
            "{\"event\":\"day_start\",\"host_ms\":1}\n{\"event\":\"item_end\",\"at\":\"09:00\",\"template\":\"t1\",\"status\":\"done\"}\n{\"event\":\"reaction_start\",\"logical\":\"2027-02-03T09:15\",\"template\":\"r\",\"context\":\"c\"}\n{\"partial\":",
        )
        .unwrap();
        let c = catalog(&runs, &personas).unwrap();
        let l = &c["latest"][0];
        assert_eq!(l["date"], "2027-02-03");
        assert_eq!(l["done"], 1);
        assert_eq!(l["next"]["template"], "t2");
        assert_eq!(l["reacting"]["at"], "09:15");
        assert_eq!(c["sources"]["narrative"], "missing");
        std::fs::write(runs.join("plans/2027-02-04-aberg.yaml"), "persona: lnord\ndate: 2027-02-04\n").unwrap();
        assert!(day(&runs, "2027-02-04", "aberg").is_err());
    }

    #[test]
    fn warp_journal() {
        let runs = tmp("warp");
        std::fs::create_dir_all(runs.join("range")).unwrap();
        std::fs::create_dir_all(runs.join("days")).unwrap();
        std::fs::write(
            runs.join("range/warp.jsonl"),
            "{\"at\":\"2026-10-30T23:09:13\",\"event\":\"clock_set\",\"logical\":\"2027-02-01T22:00:00+01:00\"}\n\
             {\"at\":\"2026-10-30T23:10:55\",\"event\":\"planned\",\"date\":\"2027-02-02\",\"items\":{\"aberg\":9,\"lnord\":0}}\n\
             {\"at\":\"2026-10-30T23:11:00\",\"event\":\"clock_set\",\"logical\":\"2027-02-02T08:00:00+01:00\"}\n",
        )
        .unwrap();
        std::fs::write(runs.join("days/2027-02-02-aberg.jsonl"), "{\"event\":\"day_start\",\"host_ms\":5,\"items\":9}\n").unwrap();
        let r = run(&runs, now_ms()).unwrap();
        assert_eq!(r["phase"], "running");
        assert_eq!(r["day"], 1);
        assert_eq!(r["personas"], json!([{ "user": "aberg", "status": "running", "items": 9, "done": 0 }]));
        assert_eq!(r["clock"]["offset"], 60);
    }
}
