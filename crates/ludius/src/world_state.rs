//! What the world remembers between days (port of world_state.py): documents written, hand-off messages and
//! their follow-ups, calendar events, built from done items in earlier plans; the brief the planner reads.
use std::path::PathBuf;

use anyhow::Result;
use chrono::{Datelike, Duration, NaiveDate, Weekday};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

use crate::planner::{Event, World, done_slots, dump, load_plan, sorted_glob, stem_of, targets, templates};
use crate::tasks::Persona;
use crate::template::format_map;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub path: String,
    pub owner: String,
    pub date: String,
    pub template: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Message {
    pub id: String,
    pub date: String,
    pub sender: String,
    pub to: String,
    pub subject: String,
    pub body: String,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Followup {
    pub id: String,
    pub user: String,
    pub sender: String,
    pub due: String,
    pub about: String,
    #[serde(default)]
    pub document: String,
    #[serde(default)]
    pub subject: String,
    #[serde(default)]
    pub done_on: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CalendarEntry {
    pub date: String,
    pub what: String,
    pub attendees: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct State {
    #[serde(default)]
    pub documents: Vec<Document>,
    #[serde(default)]
    pub messages: Vec<Message>,
    #[serde(default)]
    pub followups: Vec<Followup>,
    #[serde(default)]
    pub calendar: Vec<CalendarEntry>,
}

pub fn next_workday(day: NaiveDate) -> NaiveDate {
    let mut d = day + Duration::days(1);
    while matches!(d.weekday(), Weekday::Sat | Weekday::Sun) {
        d += Duration::days(1);
    }
    d
}

/// Python's repr of a plain string, as the brief quotes mail subjects.
fn repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') { format!("\"{s}\"") } else { format!("'{}'", s.replace('\'', "\\'")) }
}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

pub fn build(world: &World, events: &[Event], personas: &[Persona], before: NaiveDate) -> Result<State> {
    let (mut state, templates, before) = (State::default(), templates()?, iso(before));
    let mut finished: IndexMap<String, String> = IndexMap::new();
    let mut handoffs: Vec<Followup> = vec![];
    for path in sorted_glob(&world.root.join("plans"), ".yaml")? {
        let stem = stem_of(&path);
        let (Some(date), Some(user)) = (stem.get(..10), stem.get(11..)) else { continue };
        if date >= before.as_str() {
            continue;
        }
        let done = done_slots(&world.day_log(user, date))?;
        for it in load_plan(&path, true)?.items {
            if !done.contains(&(it.at.clone(), it.template.clone())) {
                continue;
            }
            let t = templates.get(&it.template);
            let values = it.values();
            let mut doc = String::new();
            if let Some(w) = t.and_then(|t| t.writes.first())
                && let Ok(p) = format_map(w.strip_suffix(".*").unwrap_or(w), |k| values.get(k).cloned())
            {
                state.documents.push(Document { path: p.clone(), owner: user.into(), date: date.into(), template: it.template.clone() });
                doc = p;
            }
            if !it.followup.is_empty() {
                finished.entry(it.followup.clone()).or_insert_with(|| date.into());
            }
            let cap = t.map_or("", |t| t.cap());
            if !it.handoff.is_empty() && cap != "chat.dm" {
                let fid = format!("{date}-{user}-{}", it.at.replace(':', ""));
                let mut about = if it.context.is_empty() { it.template.clone() } else { it.context.clone() };
                let subject = if cap == "mail.send" { it.params.get("subject").cloned().unwrap_or_default() } else { String::new() };
                if !subject.is_empty() {
                    about = format!("mail {}: {about}", repr(&subject));
                }
                let see = if doc.is_empty() { String::new() } else { format!(" See {doc}.") };
                state.messages.push(Message {
                    id: fid.clone(),
                    date: date.into(),
                    sender: user.into(),
                    to: it.handoff.clone(),
                    subject: if subject.is_empty() { about.chars().take(80).collect() } else { subject.clone() },
                    body: format!("{about}{see}"),
                });
                let due = iso(next_workday(NaiveDate::parse_from_str(date, "%Y-%m-%d")?));
                handoffs.push(Followup { id: fid, user: it.handoff.clone(), sender: user.into(), due, about, document: doc.clone(), subject, done_on: String::new() });
            }
        }
    }
    state.followups = handoffs.into_iter().map(|f| Followup { done_on: finished.get(&f.id).cloned().unwrap_or_default(), ..f }).collect();
    state.calendar = calendar(events, personas);
    Ok(state)
}

/// Files this persona's plans targeted, plus the guest inventory (world/guest-files-<user>.txt).
pub fn existing_files(world: &World, user: &str) -> Result<Vec<String>> {
    let templates = templates()?;
    let mut out = vec![];
    for path in sorted_glob(&world.root.join("plans"), ".yaml")? {
        if stem_of(&path).get(11..) == Some(user) {
            out.extend(load_plan(&path, true)?.items.iter().flat_map(|it| targets(it, &templates)).map(|(_, v)| v));
        }
    }
    let inventory = world.root.join("world").join(format!("guest-files-{user}.txt"));
    if inventory.exists() {
        let text = std::fs::read_to_string(&inventory)?;
        out.extend(text.trim_start_matches('\u{feff}').lines().filter(|x| !x.trim().is_empty()).map(String::from));
    }
    Ok(out)
}

pub fn calendar(events: &[Event], personas: &[Persona]) -> Vec<CalendarEntry> {
    events
        .iter()
        .filter_map(|e| {
            let text = e.what.to_lowercase();
            let first = |n: &str| n.split_whitespace().next().unwrap_or_default().to_lowercase();
            let who: Vec<String> = personas.iter().filter(|p| text.contains(&p.name.to_lowercase()) || text.contains(&first(&p.name))).map(|p| p.user.clone()).collect();
            (!who.is_empty()).then(|| CalendarEntry { date: e.date.clone(), what: e.what.clone(), attendees: who })
        })
        .collect()
}

pub fn mail_threads(state: &State, user: &str) -> std::collections::BTreeMap<String, (String, String)> {
    state.followups.iter().filter(|f| f.user == user && !f.subject.is_empty()).map(|f| (f.id.clone(), (f.sender.clone(), f.subject.clone()))).collect()
}

pub fn due<'s>(state: &'s State, user: &str, day: NaiveDate) -> Vec<&'s Followup> {
    let day = iso(day);
    state.followups.iter().filter(|f| f.user == user && f.done_on.is_empty() && f.due <= day).collect()
}

/// What the persona knows going into `day`: inbox, calendar, own recent files, open follow-ups.
pub fn brief(state: &State, user: &str, day: NaiveDate) -> String {
    let (today, soon) = (iso(day), iso(day + Duration::days(3)));
    let last = |v: Vec<String>, n: usize| v[v.len().saturating_sub(n)..].to_vec();
    let mut lines: Vec<String> = vec![];
    let inbox = last(state.messages.iter().filter(|m| m.to == user && m.date < today).map(|m| format!("- {} from {}: {}", m.date, m.sender, m.body)).collect(), 10);
    if !inbox.is_empty() {
        lines.push("Inbox:".into());
        lines.extend(inbox);
    }
    let cal: Vec<String> = state.calendar.iter().filter(|c| c.attendees.iter().any(|a| a == user) && today <= c.date && c.date <= soon).map(|c| format!("- {} {}", c.date, c.what)).collect();
    if !cal.is_empty() {
        lines.push("Calendar (next days):".into());
        lines.extend(cal);
    }
    // as a Python dict keyed by casefolded path: first position, last value
    let mut docs: IndexMap<String, &Document> = IndexMap::new();
    for d in state.documents.iter().filter(|d| d.owner == user) {
        docs.insert(crate::template::casefold(&d.path), d);
    }
    let docs = last(docs.values().map(|d| format!("- {} {}", d.date, d.path)).collect(), 8);
    if !docs.is_empty() {
        lines.push("Own recent files:".into());
        lines.extend(docs);
    }
    let open = due(state, user, day);
    if !open.is_empty() {
        lines.push("Open follow-ups (must be scheduled today):".into());
        lines.extend(open.iter().map(|f| format!("- [{}] from {}: {}{}", f.id, f.sender, f.about, if f.document.is_empty() { String::new() } else { format!(" (file {})", f.document) })));
    }
    lines.join("\n")
}

/// world/state.yaml (JSON, which both YAML loaders read alike).
pub fn save(state: &State, world: &World) -> Result<PathBuf> {
    let path = world.root.join("world").join("state.yaml");
    dump(&path, state, false)?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::planner::{DayPlan, PlanItem};

    #[test]
    fn handoffs_become_messages_and_followups_until_done() {
        let root = crate::day::tests::log("state").path.parent().unwrap().join(format!("ludius-state-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let world = World::new(&root);
        let it = |at: &str, params: &[(&str, &str)], handoff: &str, followup: &str| PlanItem {
            template: "notepad_write_note".into(),
            at: at.into(),
            params: params.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
            handoff: handoff.into(),
            followup: followup.into(),
            context: "weekly numbers".into(),
            ..Default::default()
        };
        let fri = DayPlan { persona: "aberg".into(), date: "2026-10-30".into(), items: vec![it("09:00", &[("file_name", "Numbers"), ("body", "x")], "lnord", ""), it("10:00", &[("file_name", "never"), ("body", "x")], "lnord", "")] };
        let mon = DayPlan { persona: "lnord".into(), date: "2026-11-02".into(), items: vec![it("09:00", &[("file_name", "reply"), ("body", "y")], "", "2026-10-30-aberg-0900")] };
        for (p, at) in [(&fri, "09:00"), (&mon, "09:00")] {
            dump(&world.plan_path(&p.persona, &p.date), p, false).unwrap();
            let log = world.day_log(&p.persona, &p.date);
            std::fs::create_dir_all(log.parent().unwrap()).unwrap();
            std::fs::write(&log, format!("{{\"event\":\"item_end\",\"at\":\"{at}\",\"template\":\"notepad_write_note\",\"status\":\"done\"}}\n")).unwrap();
        }
        let tue = NaiveDate::from_ymd_opt(2026, 11, 3).unwrap();
        let state = build(&world, &[], &[], NaiveDate::from_ymd_opt(2026, 11, 2).unwrap()).unwrap();
        assert_eq!(state.followups.len(), 1, "only done items hand off");
        let f = &state.followups[0];
        assert_eq!((f.id.as_str(), f.due.as_str(), f.done_on.as_str()), ("2026-10-30-aberg-0900", "2026-11-02", ""));
        assert!(f.document.to_lowercase().contains("numbers"));
        assert!(brief(&state, "lnord", tue).contains("Open follow-ups") && brief(&state, "lnord", tue).contains("Inbox:"));
        let state = build(&world, &[], &[], tue).unwrap();
        assert_eq!(state.followups[0].done_on, "2026-11-02");
        assert!(due(&state, "lnord", tue).is_empty());
        assert_eq!(next_workday(NaiveDate::from_ymd_opt(2026, 10, 30).unwrap()), NaiveDate::from_ymd_opt(2026, 11, 2).unwrap());
        assert_eq!(repr("it's"), "\"it's\"");
    }
}
