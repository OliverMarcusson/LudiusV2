//! Manager requests: at run time, is a delivery's file proven and does a task fulfil its request;
//! the planner half is in plan.rs (manager) and map.rs (report).
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

use crate::tasks::{Task, Template, write_paths};
use crate::{guestfile, mail};

mod map;
mod plan;
pub use map::*;
pub use plan::*;

pub const WRITTEN: &str = "written and checked";

pub fn root() -> PathBuf {
    crate::paths::runs()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Request {
    pub to: String,
    pub task: String,
    pub subject: String,
    pub body: String,
    #[serde(default)]
    pub file_name: String,
    #[serde(default)]
    pub include: Vec<String>,
    #[serde(default)]
    pub colleague: String,
    pub manager: String,
    pub msgid: String,
    #[serde(default)]
    pub status: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Day {
    pub manager: String,
    pub date: String,
    #[serde(default)]
    pub requests: Vec<Request>,
    #[serde(default)]
    pub rejects: Vec<String>,
}

fn dir(root: &Path) -> PathBuf {
    root.join("world").join("requests")
}

/// Every manager day (world/requests/2*.yaml), oldest first; an unreadable one is an error, never skipped.
pub fn days(root: &Path) -> Result<Vec<Day>> {
    let mut paths: Vec<PathBuf> = match std::fs::read_dir(dir(root)) {
        Ok(rd) => rd.filter_map(|e| e.ok().map(|e| e.path())).collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => return Err(e.into()),
    };
    paths.retain(|p| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.starts_with('2') && n.ends_with(".yaml")));
    paths.sort();
    paths
        .iter()
        .map(|p| {
            let d: Day = serde_yaml::from_str(&std::fs::read_to_string(p)?).with_context(|| p.display().to_string())?;
            for s in d.requests.iter().filter_map(|r| r.status.as_ref()) {
                anyhow::ensure!(["planned", "sent", "failed", "rejected"].contains(&s.as_str()), "{}: status {s:?}", p.display());
            }
            Ok(d)
        })
        .collect()
}

pub fn find(msgid: &str, root: &Path) -> Result<Option<Request>> {
    Ok(days(root)?.into_iter().flat_map(|d| d.requests).find(|r| r.msgid == msgid))
}

fn seen_path(root: &Path, user: &str) -> PathBuf {
    dir(root).join(format!("seen-{user}.yaml"))
}

pub fn seen(root: &Path, user: &str) -> Result<IndexMap<String, String>> {
    let p = seen_path(root, user);
    if !p.exists() {
        return Ok(IndexMap::new());
    }
    Ok(serde_yaml::from_str::<Option<IndexMap<String, String>>>(&std::fs::read_to_string(&p)?)?.unwrap_or_default())
}

pub fn remember(root: &Path, user: &str, msgid: &str, why: &str) -> Result<()> {
    let mut all = seen(root, user)?;
    all.insert(msgid.into(), why.into());
    let p = seen_path(root, user);
    std::fs::create_dir_all(p.parent().unwrap())?;
    let tmp = p.with_extension(format!("yaml.{}.tmp", std::process::id()));
    std::fs::write(&tmp, serde_yaml::to_string(&all)?)?;
    std::fs::rename(&tmp, &p)?;
    Ok(())
}

fn delivery(t: &Template) -> bool {
    t.cap() == "mail.send" && !t.attaches().is_empty() && t.writes.is_empty()
}

/// A delivery sends only a file its request's write proved: an abandoned or wrong write is never sent.
pub fn waiting(task: &Task, root: &Path) -> Option<String> {
    if task.inst.request.is_empty() || !delivery(&task.template) {
        return None;
    }
    match seen(root, &task.owner) {
        Err(e) => Some(format!("request records unreadable: {e}")),
        Ok(s) if s.get(&task.inst.request).map(String::as_str) == Some(WRITTEN) => None,
        Ok(_) => Some("the requested file was not written and checked first, nothing to send".into()),
    }
}

fn get<'a>(values: &'a std::collections::HashMap<String, String>, k: &str) -> &'a str {
    values.get(k).map(String::as_str).unwrap_or("")
}

fn lacks(include: &[String], content: &str) -> Vec<String> {
    let have = mail::letters(content);
    include.iter().filter(|x| !have.contains(&mail::letters(x))).cloned().collect()
}

pub fn mismatch(ask: &Request, t: &Template, values: &std::collections::HashMap<String, String>) -> Option<String> {
    let manager = format!("{}@{}", ask.manager, mail::domain());
    if delivery(t) && !ask.file_name.is_empty() {
        let name = get(values, "file_name");
        if get(values, "to").trim().to_lowercase() != manager {
            return Some(format!("to {:?}, the file goes back to {}", get(values, "to"), ask.manager));
        }
        let stem = name.rsplit_once('.').map_or("", |x| x.0);
        return (crate::template::casefold(stem) != crate::template::casefold(&ask.file_name))
            .then(|| format!("attaches {name:?}, asked for {:?}", ask.file_name));
    }
    if t.task != ask.task {
        return Some(format!("asked for {}, planned {}", ask.task, t.task));
    }
    let content = match t.cap() {
        "mail.reply" => return None,
        "file.write" => {
            if crate::template::casefold(get(values, "file_name")) != crate::template::casefold(&ask.file_name) {
                return Some(format!("file {:?}, asked for {:?}", get(values, "file_name"), ask.file_name));
            }
            // ponytail: template param order, where Python joins in values-dict order; differs only for an
            // include that straddles two values. Make Task.values an IndexMap if that ever matters.
            t.params.iter().filter(|k| *k != "file_name").filter_map(|k| values.get(k).cloned()).collect::<Vec<_>>()
        }
        _ => {
            if get(values, "to").trim().to_lowercase() != format!("{}@{}", ask.colleague, mail::domain()) {
                return Some(format!("to {:?}, asked for {}", get(values, "to"), ask.colleague));
            }
            vec![get(values, "subject").to_string(), get(values, "body").to_string()]
        }
    };
    let missing = lacks(&ask.include, &content.join(" "));
    (!missing.is_empty()).then(|| format!("content lacks {missing:?}"))
}

fn text(rel: &str, data: &[u8]) -> String {
    let l = rel.to_lowercase();
    if l.ends_with(".xlsx") {
        return guestfile::xlsx_cells(data).unwrap_or_default().into_values().collect::<Vec<_>>().join(" ");
    }
    if l.ends_with(".docx") {
        return guestfile::docx_paragraphs(data).unwrap_or_default().join(" ");
    }
    if l.ends_with(".pptx") {
        return guestfile::pptx_slides(data).unwrap_or_default().concat().join(" ");
    }
    if data.starts_with(b"\xff\xfe") || data.starts_with(b"\xfe\xff") {
        let be = data.starts_with(b"\xfe\xff");
        let units: Vec<u16> = data[2..].chunks_exact(2).map(|c| if be { u16::from_be_bytes([c[0], c[1]]) } else { u16::from_le_bytes([c[0], c[1]]) }).collect();
        return String::from_utf16_lossy(&units);
    }
    let text = String::from_utf8_lossy(data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data)).into_owned();
    if l.ends_with(".rtf") {
        let groups = regex::Regex::new(r"\{\\\*[^{}]*\}").unwrap().replace_all(&text, " ").into_owned();
        return regex::Regex::new(r"(?i)\\'[0-9a-f]{2}|\\[a-z]+-?\d* ?|[{}]").unwrap().replace_all(&groups, " ").into_owned();
    }
    text
}

pub fn fulfilled(task: &Task, root: &Path) -> (bool, String) {
    let ask = match find(&task.inst.request, root) {
        Err(e) => return (false, format!("request records unreadable: {e}")),
        Ok(None) => return (false, "no record of this request".into()),
        Ok(Some(a)) => a,
    };
    if let Some(why) = mismatch(&ask, &task.template, &task.values) {
        return (false, why);
    }
    match task.template.cap() {
        "mail.reply" => {
            let ok = task.values.get("reply_to") == Some(&ask.msgid);
            (ok, if ok { "answers the request mail" } else { "the reply answers another mail" }.into())
        }
        "file.write" => {
            let paths = match write_paths(task) {
                Ok(p) => p,
                Err(e) => return (false, format!("write paths: {e}")),
            };
            let mut texts = Vec::new();
            for rel in paths {
                if !task.files_absent.as_ref().is_some_and(|a| a.contains(&rel)) {
                    return (false, format!("{rel}: not proven absent at task start"));
                }
                match guestfile::read(&task.owner, &rel) {
                    None => return (false, format!("{rel}: unreadable")),
                    Some(f) if !f.exists || f.written_ms < task.started_ms => return (false, format!("{rel}: not written in the task")),
                    Some(f) => texts.push(text(&rel, &f.data)),
                }
            }
            let missing = lacks(&ask.include, &texts.join(" "));
            if missing.is_empty() { (true, "matches the request".into()) } else { (false, format!("file lacks {missing:?}")) }
        }
        _ => (true, "matches the request".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn records_roundtrip() {
        let root = std::env::temp_dir().join(format!("ludius-req-{}", std::process::id()));
        let d = dir(&root);
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("2026-10-30-boss.yaml"), "manager: boss\ndate: '2026-10-30'\nrequests:\n  - {to: a, task: t, subject: s, body: b, manager: boss, msgid: '<m1>', status: sent, extra: ignored}\n").unwrap();
        assert_eq!(find("<m1>", &root).unwrap().unwrap().manager, "boss");
        assert!(find("<m2>", &root).unwrap().is_none());
        remember(&root, "aberg", "<m1>", WRITTEN).unwrap();
        assert_eq!(seen(&root, "aberg").unwrap()["<m1>"], WRITTEN);
        assert!(text("a.rtf", br"{\rtf1\ansi Hello\par}").contains("Hello"));
    }
}
