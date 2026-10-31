//! The planner's world on disk (planner.World): narrative, plans, day logs and the content-history journal.
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs::File;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use chrono::NaiveDate;
use serde::Serialize;
use serde_json::Value;

use super::{DayPlan, Narrative, check::content_values, load_plan, stem, templates};
use crate::tasks::Template;
use crate::template::{casefold, format_map};

pub struct World {
    pub root: PathBuf,
    pub narrative_path: PathBuf,
    pub history_path: PathBuf,
    pub lock_wait: Duration,
}

/// A held content-history lock; released on drop.
struct Journal(#[allow(dead_code)] File);

impl World {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            narrative_path: root.join("world").join("narrative.yaml"),
            history_path: root.join("world").join("content.jsonl"),
            lock_wait: Duration::from_secs(10),
        }
    }

    pub fn narrative(&self) -> Result<Narrative> {
        Narrative::load(&self.narrative_path)
    }

    pub fn save_narrative(&self, n: &Narrative) -> Result<PathBuf> {
        dump(&self.narrative_path, n, false)?;
        if !n.revised.is_empty() {
            dump(&self.root.join("world").join("revisions").join(format!("{}.yaml", n.revised)), n, false)?;
        }
        Ok(self.narrative_path.clone())
    }

    pub fn plan_path(&self, user: &str, date: &str) -> PathBuf {
        self.root.join("plans").join(format!("{date}-{user}.yaml"))
    }

    pub fn day_log(&self, user: &str, date: &str) -> PathBuf {
        self.root.join("days").join(format!("{date}-{user}.jsonl"))
    }

    /// Write the plan (`new`: never over an existing one) and journal its content for near-duplicate checks.
    pub fn save_plan(&self, plan: &DayPlan, templates: &BTreeMap<String, Template>, new: bool) -> Result<PathBuf> {
        let path = self.plan_path(&plan.persona, &plan.date);
        dump(&path, plan, new)?;
        let _j = self.journal()?;
        self.append(plan, plan.items.iter().flat_map(|it| content_values(it, templates)).collect())?;
        Ok(path)
    }

    /// Journal the plan's content the history lacks (a crash between plan and journal); returns how many.
    pub fn repair_history(&self, plan: &DayPlan, templates: &BTreeMap<String, Template>) -> Result<usize> {
        let _j = self.journal()?;
        let mut have: HashMap<String, usize> = HashMap::new();
        for r in self.rows()? {
            if r[0] == plan.date && r[1] == plan.persona {
                *have.entry(r[2].clone()).or_default() += 1;
            }
        }
        let mut missing = Vec::new();
        for v in plan.items.iter().flat_map(|it| content_values(it, templates)) {
            match have.get_mut(&v) {
                Some(n) if *n > 0 => *n -= 1,
                _ => missing.push(v),
            }
        }
        let n = missing.len();
        self.append(plan, missing)?;
        Ok(n)
    }

    pub fn history(&self, user: &str) -> Result<Vec<String>> {
        let _j = self.journal()?;
        Ok(self.rows()?.into_iter().filter(|r| r[1] == user).map(|[_, _, t]| t).collect())
    }

    /// The journal lock (world/content.lock), shared with Python's msvcrt/flock lock on the same file.
    fn journal(&self) -> Result<Journal> {
        let path = self.root.join("world").join("content.lock");
        std::fs::create_dir_all(path.parent().unwrap())?;
        let f = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(&path)?;
        let deadline = Instant::now() + self.lock_wait;
        while f.try_lock().is_err() {
            if Instant::now() >= deadline {
                bail!("another process is using the content history ({}); retry", path.display());
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        Ok(Journal(f))
    }

    /// [date, persona, text] rows; a malformed row is an error, never skipped.
    fn rows(&self) -> Result<Vec<[String; 3]>> {
        let Ok(text) = std::fs::read_to_string(&self.history_path) else {
            if self.history_path.exists() {
                bail!("{} is unreadable", self.history_path.display());
            }
            return Ok(vec![]);
        };
        let mut rows = Vec::new();
        for (n, line) in text.lines().enumerate().filter(|(_, l)| !l.is_empty()) {
            let r: Value = serde_json::from_str(line).with_context(|| format!("{} line {} is not a history row", self.history_path.display(), n + 1))?;
            let field = |k: &str| match &r[k] {
                Value::String(s) => Some(s.clone()),
                Value::Null => None,
                v => Some(v.to_string()),
            };
            let (Some(d), Some(p), Some(t)) = (field("date"), field("persona"), field("text")) else {
                bail!("{} line {} is not a history row", self.history_path.display(), n + 1);
            };
            rows.push([d, p, t]);
        }
        Ok(rows)
    }

    fn append(&self, plan: &DayPlan, values: Vec<String>) -> Result<()> {
        std::fs::create_dir_all(self.history_path.parent().unwrap())?;
        let mut f = std::fs::OpenOptions::new().read(true).append(true).create(true).open(&self.history_path)?;
        if f.seek(SeekFrom::End(0))? > 0 {
            f.seek(SeekFrom::End(-1))?;
            let mut last = [0u8];
            f.read_exact(&mut last)?;
            if last[0] != b'\n' {
                f.write_all(b"\n")?;
            }
        }
        let mut out = String::new();
        for v in values {
            out += &serde_json::json!({"date": plan.date, "persona": plan.persona, "text": v}).to_string();
            out.push('\n');
        }
        f.write_all(out.as_bytes())?;
        Ok(())
    }

    /// What the persona really did on the last days before `before`, and threads colleagues handed them.
    pub fn summary(&self, user: &str, before: NaiveDate, days: i64) -> Result<String> {
        let before_s = before.format("%Y-%m-%d").to_string();
        let mut out = Vec::new();
        for d in sorted_glob(&self.root.join("days"), &format!("-{user}.jsonl"))? {
            let day = file_name(&d)[..10].to_string();
            if day >= before_s {
                continue;
            }
            for r in day_records(&d)? {
                if r["event"] != "item_end" {
                    continue;
                }
                let params = r["params"].as_object().map(|p| {
                    p.iter().filter(|(k, _)| *k != "body").map(|(k, v)| format!("{k}={}", head(&text(v), 60))).collect::<Vec<_>>().join(", ")
                });
                let line = format!("- {day} {} {} {}: {} {}", text(&r["at"]), text(&r["template"]), text(&r["status"]), text(&r["context"]), params.unwrap_or_default());
                out.push(line.trim_end().to_string());
            }
        }
        let threads = self.threads(user, before, days)?;
        let keep = out.len().saturating_sub((12 * days) as usize);
        let mut lines = out.split_off(keep);
        if !threads.is_empty() {
            lines.push("Threads colleagues handed to this persona:".into());
            lines.extend(threads);
        }
        Ok(lines.join("\n"))
    }

    /// Done items of the last `days` days that colleagues handed to `user`.
    pub fn threads(&self, user: &str, before: NaiveDate, days: i64) -> Result<Vec<String>> {
        let start = (before - chrono::Duration::days(days)).format("%Y-%m-%d").to_string();
        let end = before.format("%Y-%m-%d").to_string();
        let mut out = Vec::new();
        for path in sorted_glob(&self.root.join("plans"), ".yaml")? {
            let s = stem_of(&path);
            let (date, who) = (&s[..10], s.get(11..).unwrap_or(""));
            if who == user || !(start.as_str() <= date && date < end.as_str()) {
                continue;
            }
            let mine: Vec<_> = load_plan(&path, true)?.items.into_iter().filter(|it| it.handoff == user).collect();
            let log = self.day_log(who, date);
            if mine.is_empty() || !log.exists() {
                continue;
            }
            let done = done_slots(&log)?;
            for it in mine.iter().filter(|it| done.contains(&(it.at.clone(), it.template.clone()))) {
                let detail = it.params.iter().filter(|(k, _)| *k != "body").map(|(k, v)| format!("{k}={}", head(v, 60))).collect::<Vec<_>>().join(", ");
                out.push(format!("- {date} {} from {who}, {}: {} {detail}", it.at, it.template, it.context).trim_end().to_string());
            }
        }
        Ok(out)
    }
}

fn head(s: &str, n: usize) -> String {
    s.chars().take(n).collect()
}

/// Python's str() of a JSON scalar; missing is empty.
fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        v => v.to_string(),
    }
}

fn file_name(p: &Path) -> String {
    p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

fn stem_of(p: &Path) -> String {
    p.file_stem().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// Files in `dir` ending in `suffix` whose names start with a YYYY-MM-DD date, sorted; a missing dir is empty.
fn sorted_glob(dir: &Path, suffix: &str) -> Result<Vec<PathBuf>> {
    let Ok(entries) = std::fs::read_dir(dir) else { return Ok(vec![]) };
    let mut out: Vec<PathBuf> = entries.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| file_name(p).ends_with(suffix) && file_name(p).len() >= 10).collect();
    out.sort();
    Ok(out)
}

/// The day log's JSON object lines; other lines are skipped.
pub fn day_records(log: &Path) -> Result<Vec<serde_json::Map<String, Value>>> {
    Ok(std::fs::read_to_string(log)?.lines().filter_map(|l| serde_json::from_str::<Value>(l).ok()).filter_map(|v| v.as_object().cloned()).collect())
}

fn slots(log: &Path, keep: impl Fn(&serde_json::Map<String, Value>) -> bool) -> Result<BTreeSet<(String, String)>> {
    if !log.exists() {
        return Ok(BTreeSet::new());
    }
    Ok(day_records(log)?.iter().filter(|r| keep(r)).map(|r| (text(&r["at"]), text(&r["template"]))).collect())
}

pub fn done_slots(log: &Path) -> Result<BTreeSet<(String, String)>> {
    slots(log, |r| r.get("event").and_then(Value::as_str) == Some("item_end") && r.get("status").and_then(Value::as_str) == Some("done"))
}

pub fn started_slots(log: &Path) -> Result<BTreeSet<(String, String)>> {
    slots(log, |r| r.get("event").and_then(Value::as_str) == Some("item_start"))
}

/// The persona's own files (casefolded profile path -> last day touched) from done items before `before`.
pub fn owned_files(root: &Path, user: &str, before: &str) -> Result<BTreeMap<String, String>> {
    let (templates, world) = (templates()?, World::new(root));
    let mut owned: BTreeMap<String, String> = BTreeMap::new();
    for path in sorted_glob(&root.join("plans"), ".yaml")? {
        let s = stem_of(&path);
        let date = s[..10].to_string();
        if s.get(11..) != Some(user) || date.as_str() >= before {
            continue;
        }
        let done = done_slots(&world.day_log(user, &date))?;
        let mut items = load_plan(&path, true)?.items;
        items.sort_by(|a, b| a.at.cmp(&b.at));
        for it in items {
            let Some(t) = templates.get(&it.template).filter(|_| done.contains(&(it.at.clone(), it.template.clone()))) else { continue };
            let values = it.values();
            let fill = |ps: &[String]| ps.iter().map(|p| format_map(p, |k| values.get(k).cloned()).map(|s| casefold(&s))).collect::<Result<Vec<_>>>();
            let (Ok(gone), Ok(made)) = (fill(&t.removes), fill(&t.writes)) else { continue };
            let removed: Vec<String> = owned.keys().filter(|p| gone.iter().any(|g| *p == g || g.strip_suffix(".*").is_some_and(|g| stem(p) == g))).cloned().collect();
            let mut exts: BTreeSet<String> = removed.iter().map(|p| p[stem(p).len()..].to_string()).collect();
            for p in &removed {
                owned.remove(p);
            }
            for w in made {
                match w.strip_suffix(".*") {
                    None => {
                        owned.insert(w, date.clone());
                    }
                    Some(base) if exts.len() == 1 => {
                        owned.insert(format!("{base}{}", exts.pop_first().unwrap()), date.clone());
                    }
                    Some(_) => {}
                }
            }
        }
    }
    Ok(owned)
}

/// Atomic YAML write: replace (`new` false) or publish only if absent (`new` true, a hard link never replaces).
pub fn dump(path: &Path, data: &impl Serialize, new: bool) -> Result<()> {
    let dir = path.parent().context("no parent dir")?;
    std::fs::create_dir_all(dir)?;
    let text = serde_yaml::to_string(data)?;
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos();
    let tmp = dir.join(format!(".{}.{}.{nanos}.tmp", file_name(path), std::process::id()));
    let result = (|| -> Result<()> {
        let mut f = File::create_new(&tmp)?;
        f.write_all(text.as_bytes())?;
        f.sync_all()?;
        drop(f);
        if new {
            return match std::fs::hard_link(&tmp, path) {
                Ok(()) => Ok(()),
                Err(e) if path.exists() => Err(anyhow::Error::new(e).context(format!("plan already exists: {}", path.display()))),
                Err(e) => Err(anyhow::anyhow!("cannot publish {} atomically without replacing: hard links unavailable ({e})", path.display())),
            };
        }
        // Windows refuses a replace while another thread has the target open: retry briefly.
        for n in 0..10 {
            match std::fs::rename(&tmp, path) {
                Ok(()) => return Ok(()),
                Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied && n < 9 => std::thread::sleep(Duration::from_millis(50 * (n + 1))),
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    })();
    let _ = std::fs::remove_file(&tmp);
    result
}
