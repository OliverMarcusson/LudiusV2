//! Ground-truth logs as an instructor timeline CSV (port of timeline.py).
use std::path::Path;

use anyhow::{Context, Result};
use chrono::DateTime;
use serde_json::Value;

pub const COLUMNS: [&str; 13] = ["time_utc", "clock", "source", "persona", "vm", "task", "subgoal", "event", "action", "target", "window", "process", "result"];
const EVENTS: [&str; 19] = [
    "day_start", "day_end", "item_start", "item_end", "task_start", "task_end", "subgoal_done", "step", "ladder", "interrupt", "resume", "break", "unlock", "unlocked", "normalized",
    "normalize_failed", "side_effects", "blocked", "item_skipped",
];

/// Python truthiness for a JSON value: null, false, 0, "" and empty containers are falsy.
fn truthy(v: Option<&Value>) -> Option<&Value> {
    v.filter(|v| match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    })
}

/// str(v) for the CSV: strings as-is, everything else as JSON.
pub fn text(v: Option<&Value>) -> String {
    match v {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
    }
}

fn first(r: &Value, keys: &[&str]) -> String {
    text(keys.iter().find_map(|k| truthy(r.get(*k))))
}

pub fn rows(paths: &[impl AsRef<Path>]) -> Result<Vec<[String; 13]>> {
    let mut out = Vec::new();
    for path in paths {
        let path = path.as_ref();
        let (mut persona, mut vm, mut task) = (String::new(), String::new(), String::new());
        for (n, line) in std::fs::read_to_string(path).with_context(|| path.display().to_string())?.lines().enumerate() {
            let r: Value = serde_json::from_str(line).with_context(|| format!("{}:{}", path.display(), n + 1))?;
            let ev = text(r.get("event"));
            if !EVENTS.contains(&ev.as_str()) {
                continue;
            }
            for (slot, keys) in [(&mut persona, &["persona"][..]), (&mut vm, &["vm"]), (&mut task, &["task", "template"])] {
                let v = first(&r, keys);
                if !v.is_empty() {
                    *slot = v;
                }
            }
            let guest = truthy(r.get("guest_ms"));
            let ms = guest.or(truthy(r.get("host_ms"))).and_then(Value::as_f64).unwrap_or(0.0);
            let at = DateTime::from_timestamp_millis(ms.floor() as i64).context("timestamp out of range")?;
            let target = match r.get("element") {
                Some(el @ Value::Object(o)) if !o.is_empty() => format!("{} '{}'", text(el.get("role")), text(el.get("name"))),
                _ => String::new(),
            };
            out.push([
                at.format("%Y-%m-%dT%H:%M:%S%.3f+00:00").to_string(),
                if guest.is_some() { "guest" } else { "host" }.into(),
                "noise".into(),
                persona.clone(),
                vm.clone(),
                task.clone(),
                text(r.get("subgoal")),
                ev,
                first(&r, &["action", "status", "by"]),
                target,
                text(r.get("window")),
                text(r.get("process")),
                result(&r),
            ]);
        }
    }
    out.sort_by(|a, b| a[0].cmp(&b[0]));
    Ok(out)
}

fn result(r: &Value) -> String {
    if r["event"] == "side_effects" {
        let opened: Vec<String> = r["opened"].as_array().map(|a| a.iter().map(|v| text(Some(v))).collect()).unwrap_or_default();
        return if opened.is_empty() { String::new() } else { format!("opened {}", opened.join("; ")) };
    }
    let mut parts: Vec<String> = ["result", "reason", "context"].iter().filter_map(|k| truthy(r.get(*k)).map(|v| text(Some(v)))).collect();
    if r["event"] == "break" {
        let m = match r.get("minutes") {
            None | Some(Value::Null) => "None".into(),
            v => text(v),
        };
        parts.push(format!("away {m} min"));
    }
    parts.join(" | ")
}

pub fn export(paths: &[impl AsRef<Path>], out: &Path) -> Result<usize> {
    let data = rows(paths)?;
    if let Some(dir) = out.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut w = csv::Writer::from_path(out)?;
    w.write_record(COLUMNS)?;
    for row in &data {
        w.write_record(row)?;
    }
    w.flush()?;
    Ok(data.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_carry_persona_and_sort_by_time() {
        let dir = std::env::temp_dir().join(format!("ludius-timeline-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("a.jsonl");
        let lines = [
            r#"{"event":"day_start","persona":"aberg","vm":"WS01","host_ms":2000}"#,
            r#"{"event":"llm_call","host_ms":1}"#,
            r#"{"event":"step","template":"note","guest_ms":1500,"host_ms":9,"action":"click","element":{"role":"Button","name":"Save"},"result":"ok"}"#,
            r#"{"event":"break","host_ms":3000,"minutes":5}"#,
            r#"{"event":"side_effects","host_ms":4000,"opened":["Edge","Teams"]}"#,
        ];
        std::fs::write(&log, lines.join("\n")).unwrap();
        let got = rows(&[&log]).unwrap();
        let col = |i: usize, c: &str| got[i][COLUMNS.iter().position(|x| *x == c).unwrap()].clone();
        assert_eq!(got.len(), 4);
        assert_eq!(col(0, "time_utc"), "1970-01-01T00:00:01.500+00:00");
        // persona carries forward in file order, before the sort by time
        assert_eq!([col(0, "clock"), col(0, "persona"), col(0, "task"), col(0, "target")], ["guest", "aberg", "note", "Button 'Save'"]);
        assert_eq!([col(1, "persona"), col(1, "vm"), col(1, "clock")], ["aberg", "WS01", "host"]);
        assert_eq!(col(2, "result"), "away 5 min");
        assert_eq!(col(3, "result"), "opened Edge; Teams");
        assert_eq!(export(&[&log], &dir.join("t.csv")).unwrap(), 4);
    }
}
