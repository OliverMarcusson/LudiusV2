//! Template admission (port of admission.py): run every variant of a proposed template on the VM from a restored
//! checkpoint; variants that pass at least 2/3 of their runs enter the catalog, the rest are cut.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::Result;
use rand::SeedableRng;
use rand::rngs::StdRng;
use serde_json::json;
use serde_yaml::{Mapping, Value};

use crate::day::Desk;
use crate::runner::{GroundTruth, now_ms};
use crate::tasks::{Task, TaskInstance, Template};

pub fn reliable(passes: usize, runs: usize) -> bool {
    runs > 0 && passes * 3 >= runs * 2
}

/// Statuses per variant, and why the runs stopped early ("" when they all ran).
pub fn validate(template: &Template, runs: usize, restore: &mut dyn FnMut() -> Result<()>, desk: &mut dyn Desk, log: &GroundTruth, report: &mut dyn FnMut(&str)) -> Result<(BTreeMap<usize, Vec<String>>, String)> {
    let mut results: BTreeMap<usize, Vec<String>> = BTreeMap::new();
    for v in 0..template.variants.len() {
        for n in 0..runs {
            if let Err(e) = restore() {
                let why = format!("restore failed before variant {v} run {}: {e:#}", n + 1);
                log.write(json!({"event": "admission_stopped", "host_ms": now_ms(), "template": template.task, "reason": why}))?;
                report(&format!("  {why}"));
                return Ok((results, why));
            }
            let inst = TaskInstance { template: template.task.clone(), params: template.example.clone(), variant: Some(v), context: "admission test run".into(), ..Default::default() };
            let mut task = Task::new(template.clone(), inst, &mut StdRng::seed_from_u64(n as u64))?;
            // ponytail: every run error counts as a failed run (Python let non-operational errors through); it can only cost admission
            let (status, steps, reason) = match desk.run(&mut task, None, true, &BTreeSet::new()) {
                Ok(o) => (o.status, o.steps, o.reason),
                Err(e) => ("error".into(), 0, format!("{e:#}").chars().take(300).collect()),
            };
            results.entry(v).or_default().push(status.clone());
            log.write(json!({"event": "admission_run", "host_ms": now_ms(), "template": template.task, "variant": v, "run": n + 1, "status": status, "steps": steps, "reason": reason}))?;
            report(&format!("  variant {v} run {}: {status} in {steps} steps{}", n + 1, if reason.is_empty() { String::new() } else { format!(" - {reason}") }));
        }
    }
    Ok((results, String::new()))
}

fn same(a: &Path, b: &Path) -> bool {
    let c = |p: &Path| p.canonicalize().unwrap_or_else(|_| p.to_path_buf());
    c(a) == c(b)
}

/// Record the results in `record`; write the reliable variants to `catalog`/<task>.yaml (dropping the proposal).
pub fn admit(template: &Template, results: &BTreeMap<usize, Vec<String>>, source: &Path, catalog: &Path, record: &Path) -> Result<bool> {
    let done = |st: &Vec<String>| st.iter().filter(|s| *s == "done").count();
    let keep: Vec<usize> = results.iter().filter(|(_, st)| reliable(done(st), st.len())).map(|(v, _)| *v).collect();
    let mut data: BTreeMap<String, Value> = if record.exists() { serde_yaml::from_str(&std::fs::read_to_string(record)?)? } else { BTreeMap::new() };
    let variants: BTreeMap<usize, String> = results.iter().map(|(v, st)| (*v, format!("{}/{}", done(st), st.len()))).collect();
    let mut entry = Mapping::new();
    entry.insert("date".into(), chrono::Local::now().date_naive().format("%Y-%m-%d").to_string().into());
    entry.insert("variants".into(), serde_yaml::to_value(variants)?);
    entry.insert("admitted_variants".into(), serde_yaml::to_value(&keep)?);
    entry.insert("admitted".into(), (!keep.is_empty()).into());
    data.insert(template.task.clone(), Value::Mapping(entry));
    std::fs::write(record, serde_json::to_string_pretty(&data)?)?;
    if keep.is_empty() {
        return Ok(false);
    }
    let target = catalog.join(format!("{}.yaml", template.task));
    if same(source, &target) && keep.len() == template.variants.len() {
        return Ok(true);
    }
    let mut raw: Mapping = serde_yaml::from_str(&std::fs::read_to_string(source)?)?;
    let variants: Vec<Vec<String>> = keep.iter().map(|v| template.variants[*v].clone()).collect();
    let used: BTreeSet<&String> = variants.iter().flatten().collect();
    if let Some(w) = &template.weights {
        raw.insert("weights".into(), serde_yaml::to_value(keep.iter().map(|v| w[*v]).collect::<Vec<_>>())?);
    }
    if let Some(Value::Mapping(subgoals)) = raw.get_mut("subgoals") {
        subgoals.retain(|k, _| k.as_str().is_some_and(|k| used.contains(&k.to_string())));
    }
    raw.insert("variants".into(), serde_yaml::to_value(&variants)?);
    std::fs::write(&target, serde_json::to_string_pretty(&raw)?)?;
    if !same(source, &target) {
        std::fs::remove_file(source)?;
    }
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::day::tests::{Calls, Fake};

    #[test]
    fn unreliable_variants_are_cut_from_the_catalog() {
        let log = crate::day::tests::log("admission");
        assert!(reliable(2, 3) && !reliable(1, 3) && !reliable(0, 0));
        let dir = std::env::temp_dir().join(format!("ludius-admission-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("proposed")).unwrap();
        let source = dir.join("proposed").join("note.yaml");
        let mut text = std::fs::read_to_string(crate::paths::data().join("templates").join("notepad_write_note.yaml")).unwrap();
        text = text.replacen("task: notepad_write_note", "task: notepad_admit_note", 1);
        std::fs::write(&source, &text).unwrap();
        let t = Template::from_yaml(&text).unwrap();
        assert!(t.variants.len() >= 2);
        // variant 0 fails every run (the Fake fails by file_name), the others pass
        let calls = Calls::default();
        let mut desk = Fake { calls: calls.clone(), ..Default::default() };
        let mut restores = 0;
        let mut results = BTreeMap::new();
        for v in 0..t.variants.len() {
            let one = Template { variants: vec![t.variants[v].clone()], weights: None, ..t.clone() };
            desk.fail = if v == 0 { [t.example["file_name"].clone()].into() } else { BTreeSet::new() };
            let (r, stopped) = validate(&one, 3, &mut || { restores += 1; Ok(()) }, &mut desk, &log, &mut |_| {}).unwrap();
            assert!(stopped.is_empty());
            results.insert(v, r[&0].clone());
        }
        assert_eq!(restores, 3 * t.variants.len());
        assert!(admit(&t, &results, &source, &dir, &dir.join("admission.yaml")).unwrap());
        assert!(!source.exists());
        let admitted = Template::from_yaml(&std::fs::read_to_string(dir.join("notepad_admit_note.yaml")).unwrap()).unwrap();
        assert_eq!(admitted.variants, t.variants[1..]);
        let record = std::fs::read_to_string(dir.join("admission.yaml")).unwrap();
        assert!(record.contains("\"0\": \"0/3\"") && record.contains("\"admitted\": true"), "{record}");
        let (r, stopped) = validate(&t, 1, &mut || anyhow::bail!("no checkpoint"), &mut desk, &log, &mut |_| {}).unwrap();
        assert!(r.is_empty() && stopped.contains("no checkpoint"));
    }
}
