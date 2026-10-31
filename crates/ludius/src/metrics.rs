//! Range metrics and the filterability check over ground-truth logs (port of metrics.py).
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub const CHANCE_MARGIN: f64 = 0.6;
pub const HOLDOUT_FRACTION: f64 = 0.3;
pub const HOLDOUT_SALT: &str = "ludius-holdout-v1";
const HOLDOUT_LIMITS: &str = "logs are disjoint, content is not: the same template or near-duplicate sessions in different logs can still leak across the split; with stand-in labels this says nothing about performance against genuine humans";
const LABELS: &str = "stand-in: positive = logs given as noise, negative = logs given with --human; operator-supplied, not verified as genuine human sessions";

#[derive(Debug, Clone)]
pub struct Instance {
    pub persona: String,
    pub template: String,
    pub launch: String,
    pub status: String,
    pub steps: Vec<Value>,
    pub source: String,
    pub line: usize,
}

#[derive(Default)]
pub struct Logs {
    pub instances: Vec<Instance>,
    pub orphans: usize,
    pub blocked: Map<String, Value>,
    pub span_ms: BTreeMap<String, i64>,
}

/// Python's round(x, n): the exact binary value rounded half to even.
fn round(x: f64, n: usize) -> f64 {
    format!("{x:.n$}").parse().unwrap_or(x)
}

/// repr of a list of strings, as Python prints it in a reason.
pub fn py_list<S: AsRef<str>>(xs: impl IntoIterator<Item = S>) -> String {
    format!("[{}]", xs.into_iter().map(|s| Feat::S(s.as_ref().into()).repr()).collect::<Vec<_>>().join(", "))
}

/// str(v) as Python prints a JSON value: None for missing, strings bare.
fn py_str(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(Value::Bool(b)) => if *b { "True" } else { "False" }.into(),
        Some(v) => v.to_string(),
    }
}

fn truthy_ms(v: &Value) -> Option<i64> {
    v.as_f64().filter(|x| *x != 0.0).map(|x| x as i64)
}

fn ms(r: &Value) -> i64 {
    truthy_ms(&r["guest_ms"]).or(truthy_ms(&r["host_ms"])).unwrap_or(0)
}

fn resolve(p: &Path) -> Result<PathBuf> {
    std::fs::canonicalize(p).with_context(|| p.display().to_string())
}

pub fn root_of(paths: &[PathBuf]) -> Result<PathBuf> {
    let mut dirs = paths.iter().map(|p| Ok(resolve(p)?.parent().unwrap_or(Path::new("")).to_path_buf()));
    let Some(first) = dirs.next() else { return Ok(PathBuf::new()) };
    dirs.try_fold(first?, |acc, d: Result<PathBuf>| {
        let d = d?;
        Ok(acc.components().zip(d.components()).take_while(|(a, b)| a == b).map(|(a, _)| a).collect())
    })
}

fn source(p: &Path, root: &Path) -> Result<String> {
    let rel = resolve(p)?.strip_prefix(root).map(Path::to_path_buf).with_context(|| format!("{} is outside {}", p.display(), root.display()))?;
    Ok(rel.components().map(|c| c.as_os_str().to_string_lossy()).collect::<Vec<_>>().join("/"))
}

pub fn load(paths: &[PathBuf], root: &Path) -> Result<Logs> {
    let mut logs = Logs::default();
    for path in paths {
        let (mut persona, mut cur, mut first, mut last) = (String::new(), None::<usize>, 0i64, 0i64);
        let src = source(path, root)?;
        for (n, line) in std::fs::read_to_string(path).with_context(|| path.display().to_string())?.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let r: Value = serde_json::from_str(line).with_context(|| format!("{}:{}", path.display(), n + 1))?;
            let ms = r["host_ms"].as_f64().unwrap_or(0.0) as i64;
            if ms != 0 {
                first = if first != 0 { first } else { ms };
                last = ms;
            }
            if let Some(p) = r["persona"].as_str().filter(|p| !p.is_empty()) {
                persona = p.into();
            }
            match r["event"].as_str() {
                Some("task_start") => {
                    let launch = r["subgoals"].get(0).and_then(Value::as_str).unwrap_or("").to_string();
                    let template = r["task"].as_str().unwrap_or("").to_string();
                    logs.instances.push(Instance { persona: persona.clone(), template, launch, status: "open".into(), steps: vec![], source: src.clone(), line: n + 1 });
                    cur = Some(logs.instances.len() - 1);
                }
                Some("task_end") => {
                    if let Some(i) = cur.take() {
                        logs.instances[i].status = r["status"].as_str().unwrap_or("").into();
                    }
                }
                Some("step") => match cur {
                    Some(i) => logs.instances[i].steps.push(r),
                    None => logs.orphans += 1,
                },
                Some("blocked") => {
                    let reason = r.get("reason").map_or(String::new(), |v| py_str(Some(v)));
                    let n = logs.blocked.get(&reason).and_then(Value::as_u64).unwrap_or(0);
                    logs.blocked.insert(reason, (n + 1).into());
                }
                _ => {}
            }
        }
        if !persona.is_empty() {
            *logs.span_ms.entry(persona).or_default() += last - first;
        }
    }
    Ok(logs)
}

fn gaps(steps: &[Value]) -> Vec<f64> {
    let ts: Vec<i64> = steps.iter().map(ms).collect();
    ts.windows(2).filter(|w| w[1] > w[0]).map(|w| (w[1] - w[0]) as f64 / 1000.0).collect()
}

// ponytail: plain f64 sums; Python's statistics uses exact fractions, so the last digit can differ
fn mean(xs: &[f64]) -> f64 {
    xs.iter().sum::<f64>() / xs.len() as f64
}

fn cv(xs: &[f64]) -> f64 {
    if xs.len() < 2 || mean(xs) == 0.0 {
        return 0.0;
    }
    let m = mean(xs);
    (xs.iter().map(|x| (x - m).powi(2)).sum::<f64>() / xs.len() as f64).sqrt() / m
}

fn role(step: &Value) -> String {
    step["element"].get("role").map_or(String::new(), |v| py_str(Some(v)))
}

fn repeat(xs: &[&Instance]) -> f64 {
    let sigs: Vec<Vec<String>> = xs.iter().map(|i| i.steps.iter().map(|s| format!("{}:{}", py_str(s.get("action")), role(s))).collect()).collect();
    if sigs.is_empty() { 0.0 } else { 1.0 - sigs.iter().collect::<BTreeSet<_>>().len() as f64 / sigs.len() as f64 }
}

pub fn range_metrics(logs: &Logs) -> Value {
    let mut by_tpl: BTreeMap<&str, Vec<&Instance>> = BTreeMap::new();
    let mut by_persona: BTreeMap<&str, Vec<&Instance>> = BTreeMap::new();
    let mut apps: BTreeMap<String, Vec<&Instance>> = BTreeMap::new();
    for i in &logs.instances {
        by_tpl.entry(&i.template).or_default().push(i);
        by_persona.entry(&i.persona).or_default().push(i);
        let procs: BTreeSet<String> = i.steps.iter().filter(|s| truthy(&s["process"])).map(|s| py_str(s.get("process"))).collect();
        for p in procs {
            apps.entry(p).or_default().push(i);
        }
    }
    let steps: usize = logs.instances.iter().map(|i| i.steps.len()).sum();
    let rate = |n: usize, d: usize| round(n as f64 / d as f64, 3);
    let failed = |xs: &[&Instance]| xs.iter().filter(|i| i.status != "done" && i.status != "open").count();
    let per_hour = |n: usize, span: i64| if span != 0 { json!(round(n as f64 / (span as f64 / 3.6e6), 1)) } else { Value::Null };
    let total = steps + logs.orphans;
    json!({
        "coherence": {
            "completion_by_template": by_tpl.iter().map(|(t, xs)| (t.to_string(), json!(rate(xs.iter().filter(|i| i.status == "done").count(), xs.len())))).collect::<Map<_, _>>(),
            "orphan_steps": logs.orphans,
            "orphan_rate": if total != 0 { round(logs.orphans as f64 / total as f64, 4) } else { 0.0 },
        },
        "guardrails": {
            "blocked": logs.blocked,
            "failure_rate_by_app": apps.iter().map(|(a, xs)| (a.clone(), json!(rate(failed(xs), xs.len())))).collect::<Map<_, _>>(),
        },
        "volume": by_persona.iter().map(|(p, xs)| {
            let span = logs.span_ms.get(*p).copied().unwrap_or(0);
            (p.to_string(), json!({"tasks_per_hour": per_hour(xs.len(), span), "steps_per_hour": per_hour(xs.iter().map(|i| i.steps.len()).sum(), span)}))
        }).collect::<Map<_, _>>(),
        "repetition": {
            "sequence_repeat_by_template": by_tpl.iter().map(|(t, xs)| (t.to_string(), json!(round(repeat(xs), 3)))).collect::<Map<_, _>>(),
            "launch_methods": by_tpl.iter().map(|(t, xs)| {
                let mut c = Map::new();
                for i in xs {
                    let n = c.get(&i.launch).and_then(Value::as_u64).unwrap_or(0);
                    c.insert(i.launch.clone(), (n + 1).into());
                }
                (t.to_string(), Value::Object(c))
            }).collect::<Map<_, _>>(),
            "step_gap_cv_by_persona": by_persona.iter().map(|(p, xs)| (p.to_string(), json!(round(cv(&xs.iter().flat_map(|i| gaps(&i.steps)).collect::<Vec<_>>()), 3)))).collect::<Map<_, _>>(),
        },
    })
}

fn truthy(v: &Value) -> bool {
    !matches!(v, Value::Null | Value::Bool(false)) && v != &json!(0) && v != &json!("") && v.as_array().is_none_or(|a| !a.is_empty()) && v.as_object().is_none_or(|o| !o.is_empty())
}

#[derive(Debug, Clone, PartialEq, PartialOrd)]
pub enum Feat {
    I(i64),
    F(f64),
    S(String),
}

impl Feat {
    fn num(&self) -> f64 {
        match self {
            Feat::I(i) => *i as f64,
            Feat::F(f) => *f,
            Feat::S(s) => s.parse().unwrap_or(f64::NAN),
        }
    }
    /// str(v) for the CSV.
    fn csv(&self) -> String {
        match self {
            Feat::I(i) => i.to_string(),
            Feat::F(f) => format!("{f:?}"),
            Feat::S(s) => s.clone(),
        }
    }
    /// repr(v) for the rule text.
    fn repr(&self) -> String {
        match self {
            Feat::S(s) if s.contains('\'') && !s.contains('"') => format!("\"{s}\""),
            Feat::S(s) => format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'")),
            v => v.csv(),
        }
    }
}

pub fn features(i: &Instance) -> Vec<(&'static str, Feat)> {
    let g = gaps(&i.steps);
    let ts: Vec<i64> = i.steps.iter().map(ms).filter(|t| *t != 0).collect();
    let first = |k: &str| i.steps.first().map_or(String::new(), |s| py_str(s.get(k)));
    vec![
        ("steps", Feat::I(i.steps.len() as i64)),
        ("mean_gap_s", Feat::F(if g.is_empty() { 0.0 } else { mean(&g) })),
        ("gap_cv", Feat::F(cv(&g))),
        ("hour_utc", Feat::I(ts.first().map_or(-1, |t| t.div_euclid(3_600_000).rem_euclid(24)))),
        ("launch", Feat::S(i.launch.clone())),
        ("first_action", Feat::S(first("action"))),
        ("process", Feat::S(first("process"))),
    ]
}

type Rule = (&'static str, &'static str, Feat, bool);
type Row = Vec<(&'static str, Feat)>;

fn balanced(pred: &[bool], noise: &[bool]) -> f64 {
    let (mut pos, mut np, mut neg, mut nn) = (0.0, 0.0, 0.0, 0.0);
    for (p, n) in pred.iter().zip(noise) {
        if *n {
            np += 1.0;
            pos += f64::from(*p as u8);
        } else {
            nn += 1.0;
            neg += f64::from(!*p as u8);
        }
    }
    (pos / np + neg / nn) / 2.0
}

fn hits(x: &Feat, op: &str, v: &Feat) -> bool {
    if op == "==" { x == v } else { x.num() <= v.num() }
}

fn text(rule: Option<&Rule>) -> String {
    rule.map_or(String::new(), |(name, op, v, noise)| format!("{} if {name} {op} {}", if *noise { "noise" } else { "human" }, v.repr()))
}

fn fit(rows: &[Row], label: &[bool]) -> Vec<(&'static str, f64, Option<Rule>)> {
    if rows.is_empty() || !label.iter().any(|l| *l) || label.iter().all(|l| *l) {
        return vec![];
    }
    let mut out = vec![];
    for (k, (name, _)) in rows[0].iter().enumerate() {
        let vals: Vec<&Feat> = rows.iter().map(|r| &r[k].1).collect();
        let mut uniq: Vec<&Feat> = vec![];
        for v in &vals {
            if !uniq.contains(v) {
                uniq.push(v);
            }
        }
        // ponytail: values in first-seen order; Python iterates a set, so ties may pick another equal rule
        let mut best: (f64, Option<Rule>) = (0.5, None);
        for v in uniq {
            let op = if matches!(v, Feat::S(_)) { "==" } else { "<=" };
            let acc = balanced(&vals.iter().map(|x| hits(x, op, v)).collect::<Vec<_>>(), label);
            for (score, noise) in [(acc, true), (1.0 - acc, false)] {
                if score > best.0 {
                    best = (round(score, 3), Some((*name, op, v.clone(), noise)));
                }
            }
        }
        out.push((*name, best.0, best.1));
    }
    out
}

fn score(rule: &Rule, rows: &[Row], label: &[bool]) -> f64 {
    let (name, op, v, noise) = rule;
    let k = rows.first().and_then(|r| r.iter().position(|(n, _)| n == name)).unwrap_or(0);
    balanced(&rows.iter().map(|r| hits(&r[k].1, op, v) == *noise).collect::<Vec<_>>(), label)
}

pub fn filterability(noise: &[Instance], human: &[Instance]) -> Value {
    let rows: Vec<Row> = noise.iter().chain(human).map(features).collect();
    let label: Vec<bool> = noise.iter().map(|_| true).chain(human.iter().map(|_| false)).collect();
    let mut rules: Vec<(&str, f64, String)> = fit(&rows, &label).into_iter().map(|(n, s, r)| (n, s, text(r.as_ref()))).collect();
    let mut top = (0.5, String::new());
    for (_, s, t) in &rules {
        if (*s, t) > (top.0, &top.1) {
            top = (*s, t.clone());
        }
    }
    rules.sort_by(|a, b| b.1.total_cmp(&a.1));
    json!({
        "noise": noise.len(),
        "human": human.len(),
        "rules": rules.iter().map(|(n, s, t)| (n.to_string(), json!([s, t]))).collect::<Map<_, _>>(),
        "best": [top.0, top.1],
        "pass": top.0 <= CHANCE_MARGIN,
        "held_out": held_out(noise, human, HOLDOUT_FRACTION, HOLDOUT_SALT),
    })
}

/// Whole source logs to train or test, per label; a reason when a fair split is impossible.
pub fn split(noise: &[Instance], human: &[Instance], fraction: f64, salt: &str) -> Result<BTreeMap<String, &'static str>, String> {
    let groups = |xs: &[Instance]| xs.iter().map(|i| i.source.clone()).collect::<BTreeSet<_>>();
    let (n, h) = (groups(noise), groups(human));
    let both: Vec<&String> = n.intersection(&h).collect();
    if !both.is_empty() {
        return Err(format!("source logs carry both labels: {}", py_list(both)));
    }
    let mut out = BTreeMap::new();
    for (lab, gs) in [("noise", n), ("human", h)] {
        if gs.len() < 2 {
            return Err(format!("{} independent {lab} source log(s); a held-out split needs at least 2 per label", gs.len()));
        }
        let mut ordered: Vec<String> = gs.into_iter().collect();
        ordered.sort_by_cached_key(|g| format!("{:x}", Sha256::digest(format!("{salt}:{g}"))));
        let k = ((fraction * ordered.len() as f64).round_ties_even() as usize).max(1);
        out.extend(ordered.into_iter().enumerate().map(|(i, g)| (g, if i < k { "test" } else { "train" })));
    }
    Ok(out)
}

pub fn held_out(noise: &[Instance], human: &[Instance], fraction: f64, salt: &str) -> Value {
    let mut out = json!({"grouping": "source log", "fraction": fraction, "salt": salt, "limits": HOLDOUT_LIMITS});
    let mut put = |k: &str, v: Value| _ = out.as_object_mut().unwrap().insert(k.into(), v);
    let sides = match split(noise, human, fraction, salt) {
        Ok(s) => s,
        Err(why) => {
            put("available", false.into());
            put("reason", why.into());
            return out;
        }
    };
    let data: Vec<(&Instance, bool)> = noise.iter().map(|i| (i, true)).chain(human.iter().map(|i| (i, false))).collect();
    let part = |s: &str| data.iter().filter(|(i, _)| sides[&i.source] == s).cloned().collect::<Vec<_>>();
    let (train, test) = (part("train"), part("test"));
    for (s, xs) in [("train", &train), ("test", &test)] {
        let sources: BTreeSet<&str> = xs.iter().map(|(i, _)| i.source.as_str()).collect();
        put(s, json!({"noise": xs.iter().filter(|x| x.1).count(), "human": xs.iter().filter(|x| !x.1).count(), "sources": sources}));
    }
    let feats = |xs: &[(&Instance, bool)]| xs.iter().map(|(i, _)| features(i)).collect::<Vec<_>>();
    let labels = |xs: &[(&Instance, bool)]| xs.iter().map(|x| x.1).collect::<Vec<_>>();
    let mut best: (f64, Option<Rule>) = (0.5, None);
    let mut first = true;
    for (_, s, r) in fit(&feats(&train), &labels(&train)) {
        if first || s > best.0 {
            (best, first) = ((s, r), false);
        }
    }
    let Some(rule) = best.1 else {
        put("available", false.into());
        put("reason", "no training rule beats chance".into());
        return out;
    };
    let held = round(score(&rule, &feats(&test), &labels(&test)), 3);
    for (k, v) in [("available", json!(true)), ("rule", json!(text(Some(&rule)))), ("train_score", json!(best.0)), ("held_out_score", json!(held)), ("pass", json!(held <= CHANCE_MARGIN))] {
        put(k, v);
    }
    out
}

fn provenance(paths: &[PathBuf], root: &Path) -> Result<Value> {
    paths.iter().map(|p| Ok(json!({"source": source(p, root)?, "sha256": format!("{:x}", Sha256::digest(std::fs::read(p)?))}))).collect::<Result<Vec<_>>>().map(Value::from)
}

fn write_features(out: &Path, noise: &[Instance], human: &[Instance]) -> Result<usize> {
    let sides = split(noise, human, HOLDOUT_FRACTION, HOLDOUT_SALT).unwrap_or_default();
    let rows: Vec<(&Instance, &str)> = noise.iter().map(|i| (i, "noise")).chain(human.iter().map(|i| (i, "human"))).collect();
    let mut w = csv::Writer::from_path(out)?;
    let mut head: Vec<String> = ["label", "split", "source", "line", "persona", "template", "status"].map(String::from).to_vec();
    head.extend(rows.first().map(|(i, _)| features(i).into_iter().map(|(n, _)| n.to_string()).collect::<Vec<_>>()).unwrap_or_default());
    w.write_record(&head)?;
    for (i, lab) in &rows {
        let mut rec = vec![lab.to_string(), sides.get(&i.source).unwrap_or(&"").to_string(), i.source.clone(), i.line.to_string(), i.persona.clone(), i.template.clone(), i.status.clone()];
        rec.extend(features(i).into_iter().map(|(_, f)| f.csv()));
        w.write_record(&rec)?;
    }
    w.flush()?;
    Ok(rows.len())
}

pub fn report(noise: &[PathBuf], human: &[PathBuf], features_csv: Option<&Path>) -> Result<Value> {
    let root = root_of(&[noise, human].concat())?;
    let logs = load(noise, &root)?;
    let mut out = range_metrics(&logs);
    let comparison = if human.is_empty() { vec![] } else { load(human, &root)?.instances };
    if !human.is_empty() {
        out["filterability"] = filterability(&logs.instances, &comparison);
    }
    let mut dataset = json!({"labels": LABELS, "human_baseline": false, "noise": provenance(noise, &root)?, "comparison": provenance(human, &root)?});
    if let Some(f) = features_csv {
        dataset["features_csv"] = json!({"path": f.display().to_string(), "rows": write_features(f, &logs.instances, &comparison)?});
    }
    out["dataset"] = dataset;
    Ok(out)
}

#[cfg(test)]
mod tests;
