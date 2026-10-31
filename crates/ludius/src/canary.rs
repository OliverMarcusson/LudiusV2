//! Drift canary (port of canary.py): one admitted task per app, run until done (at most twice); an app whose
//! canary no longer finishes has drifted. Results go to the health file the planner reads.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::Result;
use indexmap::IndexMap;
use rand::SeedableRng;
use rand::rngs::StdRng;
use serde_json::{Map, Value, json};

use crate::day::Desk;
use crate::runner::{GroundTruth, now_ms};
use crate::tasks::{Task, TaskInstance, Template};

pub const RUNS_PER_CANARY: u64 = 2;

/// Per app: the first admitted template (by name) and its first admitted variant, else an open probe.
pub fn pick(templates: &BTreeMap<String, Template>, admission: &serde_yaml::Value) -> BTreeMap<String, (Template, usize)> {
    let mut out: BTreeMap<String, (Template, usize)> = BTreeMap::new();
    for (name, t) in templates {
        let rec = &admission[name.as_str()];
        let first = rec["admitted_variants"].as_sequence().and_then(|v| v.first()).and_then(serde_yaml::Value::as_u64);
        if let (Some(true), Some(v)) = (rec["admitted"].as_bool(), first)
            && !out.contains_key(&t.app)
        {
            out.insert(t.app.clone(), (t.clone(), v as usize));
        }
    }
    // an app with no admitted task still gets checked that it opens and closes
    for t in templates.values() {
        if !out.contains_key(&t.app)
            && let Some(p) = open_probe(t)
        {
            out.insert(p.app.clone(), (p, 0));
        }
    }
    out
}

/// Only t's launch and its app-close subgoal: no writes, no sends. A broken Outlook profile fails the launch.
pub fn open_probe(t: &Template) -> Option<Template> {
    let v = t.variants.first()?;
    let (first, last) = (v.first()?, v.last()?);
    let w = t.subgoals.get(last)?.done.as_ref()?.window.as_ref()?;
    if !t.subgoals.get(first)?.launch || !w.absent || w.process.as_deref().unwrap_or_default().is_empty() || w.class_name.as_deref().is_some_and(|c| !c.is_empty()) {
        return None;
    }
    let subgoals: IndexMap<_, _> = [first, last].into_iter().map(|k| (k.clone(), t.subgoals[k].clone())).collect();
    Some(Template {
        task: format!("{}_open_probe", t.app),
        capability: None,
        variants: vec![vec![first.clone(), last.clone()]],
        weights: None,
        subgoals,
        writes: vec![],
        removes: vec![],
        key_commits: vec![],
        ..t.clone()
    })
}

pub fn judge(statuses: &[String]) -> &'static str {
    if statuses.iter().any(|s| s == "done") {
        "ok"
    } else if statuses.iter().all(|s| s == "error") {
        "unknown"
    } else {
        "drift"
    }
}

/// The health file; missing is empty, unreadable is an error.
pub fn load_health(path: &Path) -> Result<Map<String, Value>> {
    if !path.exists() {
        return Ok(Map::new());
    }
    Ok(serde_json::from_str(&std::fs::read_to_string(path)?)?)
}

pub fn drifted(path: &Path) -> Result<BTreeSet<String>> {
    Ok(load_health(path)?.into_iter().filter(|(_, h)| h["state"] == "drift").map(|(app, _)| app).collect())
}

/// Run the canaries (only `apps` when given) and write each result to the health file as it comes.
pub fn run(templates: &BTreeMap<String, Template>, admission_record: &Path, desk: &mut dyn Desk, log: &GroundTruth, health_path: &Path, apps: &BTreeSet<String>, report: &mut dyn FnMut(&str)) -> Result<BTreeMap<String, String>> {
    let admission: serde_yaml::Value = serde_yaml::from_str(&std::fs::read_to_string(admission_record)?)?;
    let mut health = load_health(health_path)?;
    let mut states = BTreeMap::new();
    for (app, (t, variant)) in pick(templates, &admission) {
        if !apps.is_empty() && !apps.contains(&app) {
            continue;
        }
        let inst = TaskInstance { template: t.task.clone(), params: t.example.clone(), context: "drift canary".into(), variant: Some(variant), ..Default::default() };
        let (mut statuses, mut reason) = (vec![], String::new());
        for n in 0..RUNS_PER_CANARY {
            // a run error (connection, LLM, decider) is "error": all errors judge as unknown, never drift
            let status = match Task::new(t.clone(), inst.clone(), &mut StdRng::seed_from_u64(n)).and_then(|mut task| desk.run(&mut task, None, true, &BTreeSet::new())) {
                Ok(o) => {
                    reason = o.reason;
                    o.status
                }
                Err(e) => {
                    reason = format!("{e:#}").chars().take(300).collect();
                    "error".into()
                }
            };
            statuses.push(status);
            if statuses.last().is_some_and(|s| s == "done") {
                break;
            }
        }
        let state = judge(&statuses);
        states.insert(app.clone(), state.to_string());
        let checked = chrono::Utc::now().format("%Y-%m-%dT%H:%M:%S+00:00").to_string();
        health.insert(app.clone(), json!({"state": state, "template": t.task, "variant": variant, "runs": statuses, "reason": reason, "checked": checked}));
        log.write(json!({"event": "canary", "host_ms": now_ms(), "app": app, "template": t.task, "variant": variant, "runs": statuses, "state": state, "reason": reason}))?;
        report(&format!("  {app}: {state} ({} v{variant}: {}){}", t.task, statuses.join(" "), if reason.is_empty() { String::new() } else { format!(" - {reason}") }));
        if let Some(dir) = health_path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        std::fs::write(health_path, serde_json::to_string_pretty(&health)?)?;
    }
    Ok(states)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::day::tests::{Calls, Fake};

    #[test]
    fn canaries_judge_drift_and_probe_unadmitted_apps() {
        let log = crate::day::tests::log("canary");
        let templates = crate::planner::templates().unwrap();
        let dir = std::env::temp_dir().join(format!("ludius-canary-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let record = dir.join("admission.yaml");
        std::fs::write(&record, "notepad_write_note:\n  admitted: true\n  admitted_variants: [1]\n").unwrap();
        let picked = pick(&templates, &serde_yaml::from_str(&std::fs::read_to_string(&record).unwrap()).unwrap());
        assert_eq!((picked["notepad"].0.task.as_str(), picked["notepad"].1), ("notepad_write_note", 1));
        assert!(picked.values().filter(|(t, _)| t.task.ends_with("_open_probe")).all(|(t, _)| t.writes.is_empty() && t.variants[0].len() == 2));
        assert_eq!((judge(&["failed".into(), "done".into()]), judge(&["error".into(), "error".into()]), judge(&["error".into(), "failed".into()])), ("ok", "unknown", "drift"));

        let calls = Calls::default();
        let name = templates["notepad_write_note"].example["file_name"].clone();
        let mut desk = Fake { calls: calls.clone(), fail: [name].into(), ..Default::default() };
        let health = dir.join("health.json");
        let states = run(&templates, &record, &mut desk, &log, &health, &["notepad".to_string()].into(), &mut |_| {}).unwrap();
        assert_eq!(states, BTreeMap::from([("notepad".to_string(), "drift".to_string())]));
        assert_eq!(calls.borrow().iter().filter(|c| c.starts_with("run")).count(), 2);
        assert_eq!(drifted(&health).unwrap(), BTreeSet::from(["notepad".to_string()]));
    }
}
