//! Persona-to-persona interactions for the range monitor (port of interactions.py): only verified effects
//! (a done send to another persona, a sent manager request, a fulfilled request) count, appended once each.
use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::{Read as _, Seek as _, SeekFrom, Write as _};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use anyhow::Result;
use chrono::DateTime;
use serde::{Deserialize, Serialize};
use serde_json::Value;

const KINDS: [(&str, &str); 5] = [("mail.send", "mail"), ("mail.reply", "reply"), ("calendar.invite", "invite"), ("chat.dm", "chat"), ("chat.file", "file")];
const MAX_READ: u64 = 8 << 20;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Interaction {
    pub id: String,
    pub personas: Vec<String>,
    pub kind: String,
    pub label: String,
    pub host: String,
    pub logical: Option<String>,
}

/// Template name -> interaction kind, for templates whose capability reaches another persona.
pub fn kinds() -> Result<BTreeMap<String, String>> {
    let kind = |cap: &str| KINDS.iter().find(|(c, _)| *c == cap).map(|(_, k)| k.to_string());
    Ok(crate::planner::templates()?.into_iter().filter_map(|(name, t)| kind(t.cap()).map(|k| (name, k))).collect())
}

fn persona(address: &Value, personas: &HashSet<String>) -> Option<String> {
    let a = crate::timeline::text(Some(address)).trim().to_lowercase();
    let (local, domain) = a.split_once('@')?;
    (domain == ludius_mail::domain().to_lowercase() && personas.contains(local)).then(|| local.to_string())
}

fn iso(ms: i64) -> String {
    DateTime::from_timestamp_millis(ms).unwrap_or_default().format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
}

fn str_of(v: &Value) -> String {
    crate::timeline::text(Some(v).filter(|v| v.as_str() != Some("")))
}

/// The interactions in the last MAX_READ bytes of `sender`'s day log; None when it can't be read.
pub fn from_day_log(log: &Path, sender: &str, personas: &HashSet<String>, sends: &BTreeMap<String, String>, requests: &HashMap<String, Value>) -> Option<Vec<Interaction>> {
    let mut f = std::fs::File::open(log).ok()?;
    let end = f.seek(SeekFrom::End(0)).ok()?;
    f.seek(SeekFrom::Start(end.saturating_sub(MAX_READ))).ok()?;
    let mut buf = vec![];
    f.read_to_end(&mut buf).ok()?;
    let name = log.file_name().unwrap_or_default().to_string_lossy();
    let mut out = vec![];
    for e in String::from_utf8_lossy(&buf).lines().filter_map(|l| serde_json::from_str::<Value>(l).ok()) {
        let Some(ms) = e["host_ms"].as_i64().filter(|_| e["host_ms"].is_i64() || e["host_ms"].is_u64()) else { continue };
        match e["event"].as_str() {
            Some("item_end") if e["status"] == "done" => {
                let Some(kind) = e["template"].as_str().and_then(|t| sends.get(t)) else { continue };
                let p = &e["params"];
                if let Some(to) = persona(&p["to"], personas).filter(|to| to != sender) {
                    let label = [&p["subject"], &p["message"]].into_iter().map(str_of).find(|s| !s.is_empty()).unwrap_or_default();
                    out.push(Interaction { id: format!("{name}:{ms}"), personas: vec![sender.into(), to], kind: kind.clone(), label, host: iso(ms), logical: e["logical"].as_str().map(String::from) });
                }
            }
            Some("request_check") if e["fulfilled"] == true && e["capability"] != "file.write" => {
                let id = crate::timeline::text(e.get("request"));
                let Some(r) = requests.get(&id) else { continue };
                let boss = str_of(&r["manager"]);
                if personas.contains(&boss) && boss != sender {
                    out.push(Interaction { id: format!("done:{id}"), personas: vec![sender.into(), boss], kind: "request_done".into(), label: str_of(&r["subject"]), host: iso(ms), logical: None });
                }
            }
            _ => {}
        }
    }
    Some(out)
}

/// Sent manager requests by message id, with the day's date and the request file's mtime (ms).
pub fn sent_requests(dir: &Path) -> HashMap<String, (Value, i64)> {
    let mut out = HashMap::new();
    let Ok(entries) = std::fs::read_dir(dir) else { return out };
    for p in entries.filter_map(|e| e.ok().map(|e| e.path())) {
        let name = p.file_name().unwrap_or_default().to_string_lossy().into_owned();
        if !(name.starts_with('2') && name.ends_with(".yaml")) {
            continue;
        }
        let Some((day, mtime)) = (|| {
            let day: Value = serde_yaml::from_str(&std::fs::read_to_string(&p).ok()?).ok()?;
            let mtime = p.metadata().ok()?.modified().ok()?.duration_since(SystemTime::UNIX_EPOCH).ok()?.as_millis() as i64;
            Some((day, mtime))
        })() else {
            continue;
        };
        for r in day["requests"].as_array().into_iter().flatten() {
            if r["status"] == "sent" && !str_of(&r["msgid"]).is_empty() {
                let mut rec = r.clone();
                rec["date"] = day["date"].clone();
                out.insert(str_of(&r["msgid"]), (rec, mtime));
            }
        }
    }
    out
}

pub struct Tracker {
    pub days: PathBuf,
    pub personas: Vec<String>,
    pub out: PathBuf,
    pub requests_dir: PathBuf,
    pub sends: BTreeMap<String, String>,
    pub recent: Vec<Interaction>,
    seen: Option<HashSet<String>>,
    mtimes: HashMap<PathBuf, SystemTime>,
}

impl Tracker {
    pub fn new(days: &Path, personas: Vec<String>, out: &Path, sends: BTreeMap<String, String>) -> Self {
        let requests_dir = days.parent().unwrap_or(Path::new(".")).join("world").join("requests");
        Self { days: days.into(), personas, out: out.into(), requests_dir, sends, recent: vec![], seen: None, mtimes: HashMap::new() }
    }

    pub fn step(&mut self) -> Result<Vec<Interaction>> {
        let seen = self.seen.get_or_insert_with(|| {
            self.recent = std::fs::read_to_string(&self.out).unwrap_or_default().lines().filter_map(|l| serde_json::from_str(l).ok()).collect();
            let ids = self.recent.iter().map(|i| i.id.clone()).collect();
            self.recent = self.recent.split_off(self.recent.len().saturating_sub(20));
            ids
        });
        let users: HashSet<String> = self.personas.iter().cloned().collect();
        let sent = sent_requests(&self.requests_dir);
        let mut found = vec![];
        for (msgid, (r, mtime)) in &sent {
            let (boss, to) = (str_of(&r["manager"]), str_of(&r["to"]));
            if users.contains(&boss) && users.contains(&to) && boss != to {
                let date = str_of(&r["date"]);
                found.push(Interaction { id: msgid.clone(), personas: vec![boss, to], kind: "request".into(), label: str_of(&r["subject"]), host: iso(*mtime), logical: Some(date).filter(|d| !d.is_empty()) });
            }
        }
        let records: HashMap<String, Value> = sent.into_iter().map(|(k, (v, _))| (k, v)).collect();
        for log in std::fs::read_dir(&self.days).into_iter().flatten().filter_map(|e| e.ok().map(|e| e.path())) {
            let name = log.file_name().unwrap_or_default().to_string_lossy().into_owned();
            let Some(user) = name.strip_suffix(".jsonl").filter(|n| n.len() > 11 && chrono::NaiveDate::parse_from_str(&n[..10], "%Y-%m-%d").is_ok()).map(|n| &n[11..]) else { continue };
            let Ok(mtime) = log.metadata().and_then(|m| m.modified()) else { continue };
            if !users.contains(user) || self.mtimes.get(&log) == Some(&mtime) {
                continue;
            }
            // an unreadable log is retried next step, not marked as read
            if let Some(got) = from_day_log(&log, user, &users, &self.sends, &records) {
                self.mtimes.insert(log.clone(), mtime);
                found.extend(got);
            }
        }
        let mut new: Vec<Interaction> = found.into_iter().filter(|i| !seen.contains(&i.id)).collect();
        if !new.is_empty() {
            new.sort_by(|a, b| a.host.cmp(&b.host));
            std::fs::create_dir_all(self.out.parent().unwrap_or(Path::new(".")))?;
            let mut f = std::fs::OpenOptions::new().create(true).append(true).open(&self.out)?;
            for i in &new {
                writeln!(f, "{}", serde_json::to_string(i)?)?;
                seen.insert(i.id.clone());
            }
            self.recent.extend(new.iter().cloned());
            self.recent.sort_by(|a, b| a.host.cmp(&b.host));
            self.recent = self.recent.split_off(self.recent.len().saturating_sub(20));
        }
        Ok(new)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn interactions_only_from_verified_effects_between_personas() {
        let tmp = std::env::temp_dir().join(format!("ludius-interactions-{}", std::process::id()));
        let (days, reqs, out) = (tmp.join("days"), tmp.join("world").join("requests"), tmp.join("monitor-interactions.jsonl"));
        std::fs::create_dir_all(&days).unwrap();
        std::fs::create_dir_all(&reqs).unwrap();
        let d = ludius_mail::domain();
        let now = chrono::Utc::now().timestamp_millis();
        let users: Vec<String> = ["aberg", "sdekker", "lnord"].map(String::from).to_vec();
        let sends = BTreeMap::from([("outlook_send_mail".to_string(), "mail".to_string())]);
        let send = |to: String, status: &str, ms: i64| {
            let params = json!({"to": to, "subject": "Board delivery numbers", "body": "typed body text"});
            format!("{}\n", json!({"event": "item_end", "template": "outlook_send_mail", "logical": "2026-11-02T10:00", "host_ms": ms, "params": params, "status": status}))
        };
        let day = [
            send(format!("sdekker@{d}"), "done", now),
            send(format!("mvellinga@{d}"), "done", now + 1),
            send(format!("lnord@{d}"), "abandoned", now + 2),
            send(format!("aberg@{d}"), "done", now + 3),
            send("sdekker@other.example".into(), "done", now + 4),
        ];
        std::fs::write(days.join("2026-11-02-aberg.jsonl"), day.concat()).unwrap();
        let mut t = Tracker::new(&days, users.clone(), &out, sends.clone());
        let new = t.step().unwrap();
        let got: Vec<_> = new.iter().map(|i| (i.personas.clone(), i.kind.as_str(), i.label.as_str(), i.logical.as_deref())).collect();
        assert_eq!(got, [(vec!["aberg".to_string(), "sdekker".into()], "mail", "Board delivery numbers", Some("2026-11-02T10:00"))]);
        assert!(!std::fs::read_to_string(&out).unwrap().contains("typed body text"));
        assert!(t.step().unwrap().is_empty());
        assert!(Tracker::new(&days, users.clone(), &out, sends).step().unwrap().is_empty());

        let msgid = format!("<1.aberg@{d}>");
        let request = |status: &str| json!({"manager": "aberg", "date": "2026-11-02", "requests": [{"to": "sdekker", "task": "excel_new_workbook", "subject": "October accruals workbook", "manager": "aberg", "msgid": msgid, "status": status}]});
        std::fs::write(reqs.join("2026-11-02-aberg.yaml"), serde_yaml::to_string(&request("planned")).unwrap()).unwrap();
        assert!(t.step().unwrap().is_empty());
        std::fs::write(reqs.join("2026-11-02-aberg.yaml"), serde_yaml::to_string(&request("sent")).unwrap()).unwrap();
        let check = json!({"event": "request_check", "host_ms": now + 9, "request": msgid, "fulfilled": true});
        let mut write = check.clone();
        write["capability"] = "file.write".into(); // the file is written, not yet mailed back
        std::fs::write(days.join("2026-11-02-sdekker.jsonl"), format!("{write}\n")).unwrap();
        assert_eq!(t.step().unwrap().iter().map(|i| i.kind.as_str()).collect::<Vec<_>>(), ["request"]);
        std::thread::sleep(std::time::Duration::from_millis(20)); // a new mtime
        std::fs::write(days.join("2026-11-02-sdekker.jsonl"), format!("{check}\n")).unwrap();
        let got: Vec<_> = t.step().unwrap().into_iter().map(|i| (i.kind, i.personas)).collect();
        assert_eq!(got, [("request_done".to_string(), vec!["sdekker".to_string(), "aberg".into()])]);
        assert_eq!(std::fs::read_to_string(&out).unwrap().lines().count(), 3);
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
