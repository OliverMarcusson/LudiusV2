//! Weekly narrative revision (port of revision.py): the story moves on from what the personas really did.
use std::collections::BTreeSet;

use anyhow::{Result, bail};
use chrono::{Datelike, Duration, NaiveDate};
use serde::{Deserialize, Serialize};

use crate::llm::Check;
use crate::planner::{Event, Narrative, Person, World, near_duplicate};
use crate::tasks::Persona;

pub const HORIZON_DAYS: i64 = 28;
pub const MAX_PROJECTS: usize = 8;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Revision {
    pub summary: String,
    #[serde(default)]
    pub finished_projects: Vec<String>,
    #[serde(default)]
    pub new_projects: Vec<String>,
    #[serde(default)]
    pub new_people: Vec<Person>,
    #[serde(default)]
    pub new_events: Vec<Event>,
}

impl Check for Revision {}

fn iso(d: NaiveDate) -> String {
    d.format("%Y-%m-%d").to_string()
}

pub fn week_start(day: NaiveDate) -> NaiveDate {
    day - Duration::days(day.weekday().num_days_from_monday() as i64)
}

/// Not yet revised in `day`'s week; an unreadable `revised` date counts as due.
pub fn due(n: &Narrative, day: NaiveDate) -> bool {
    NaiveDate::parse_from_str(&n.revised, "%Y-%m-%d").map_or(true, |r| week_start(r) < week_start(day))
}

pub fn with_personas(n: &Narrative, personas: &[Persona]) -> Narrative {
    let have: BTreeSet<&str> = n.people.iter().filter(|p| !p.user.is_empty()).map(|p| p.user.as_str()).collect();
    let extra = personas.iter().filter(|p| !have.contains(p.user.as_str())).map(|p| Person {
        name: p.name.clone(),
        role: p.role.clone(),
        department: p.department.clone(),
        user: p.user.clone(),
        manager: String::new(),
    });
    let mut out = n.clone();
    out.people.extend(extra.collect::<Vec<_>>());
    out.accounts = personas.iter().map(|p| p.user.clone()).collect();
    out
}

fn short(s: &str) -> String {
    s.chars().take(40).collect()
}

pub fn check(r: &Revision, n: &Narrative, day: NaiveDate) -> Vec<String> {
    let mut problems = vec![];
    if r.summary.trim().is_empty() {
        problems.push("summary is empty".to_string());
    }
    let bad: Vec<&String> = r.finished_projects.iter().filter(|p| !n.projects.contains(p)).collect();
    if !bad.is_empty() {
        problems.push(format!("finished_projects not in the current projects: {bad:?}"));
    }
    let dup: Vec<&String> = r.new_projects.iter().filter(|p| n.projects.contains(p)).collect();
    if !dup.is_empty() {
        problems.push(format!("new_projects already exist: {dup:?}"));
    }
    let finished: BTreeSet<&String> = r.finished_projects.iter().collect();
    let left = (n.projects.len() + r.new_projects.len()) as i64 - finished.len() as i64;
    if !(1..=MAX_PROJECTS as i64).contains(&left) {
        problems.push(format!("would leave {left} projects; keep 1-{MAX_PROJECTS}"));
    }
    let names: BTreeSet<String> = n.people.iter().map(|p| p.name.to_lowercase()).collect();
    let users: BTreeSet<&str> = n.people.iter().filter(|p| !p.user.is_empty()).map(|p| p.user.as_str()).collect();
    for p in &r.new_people {
        if names.contains(&p.name.to_lowercase()) {
            problems.push(format!("new person {:?} already exists", p.name));
        }
        if !p.user.is_empty() {
            problems.push(format!("new person {:?}: user must be empty, only personas have accounts", p.name));
            if users.contains(p.user.as_str()) {
                problems.push(format!("new person {:?} reuses account {:?}", p.name, p.user));
            }
        }
    }
    let end = day + Duration::days(HORIZON_DAYS);
    let earlier: Vec<String> = n.events.iter().map(|e| e.what.clone()).collect();
    for e in &r.new_events {
        let Ok(when) = NaiveDate::parse_from_str(&e.date, "%Y-%m-%d") else {
            problems.push(format!("event {:?}: date {:?} is not YYYY-MM-DD", short(&e.what), e.date));
            continue;
        };
        if !(day <= when && when <= end) {
            problems.push(format!("event {:?} on {}: must be between {} and {}", short(&e.what), e.date, iso(day), iso(end)));
        }
        if near_duplicate(&e.what, &earlier).is_some() {
            problems.push(format!("event {:?} repeats an existing event", short(&e.what)));
        }
    }
    problems
}

pub fn apply(r: &Revision, n: &Narrative, day: NaiveDate) -> Narrative {
    let mut out = n.clone();
    out.projects.retain(|p| !r.finished_projects.contains(p));
    out.projects.extend(r.new_projects.iter().cloned());
    out.people.extend(r.new_people.iter().cloned());
    out.events.extend(r.new_events.iter().cloned());
    out.events.sort_by(|a, b| a.date.cmp(&b.date)); // stable, as Python's sorted
    out.history.push(Event { date: iso(day), what: r.summary.trim().into() });
    out.revised = iso(day);
    out
}

fn pretty(n: &Narrative) -> Result<String> {
    let mut buf = vec![];
    let mut ser = serde_json::Serializer::with_formatter(&mut buf, serde_json::ser::PrettyFormatter::with_indent(b" "));
    n.serialize(&mut ser)?;
    Ok(String::from_utf8(buf)?)
}

/// One revision of `n`: the LLM proposes (`ask` gets the user prompt), one correction round, then the
/// unusable parts are dropped. Returns the revised narrative and the problems of the last proposal.
pub fn revise(ask: &mut dyn FnMut(&str) -> Result<Revision>, n: &Narrative, personas: &[Persona], day: NaiveDate, happened: &str) -> Result<(Narrative, Vec<String>)> {
    if !due(n, day) {
        bail!("narrative already revised this week ({})", n.revised);
    }
    let n = with_personas(n, personas);
    let user = format!(
        "Company story so far:\n{}\n\nToday is {} {}. What really happened since the last revision ({}):\n{}\
         \n\nRevise the story for the coming weeks without contradicting it. Return JSON with keys summary (1-3 \
         sentences: what changed this week, grounded in what happened), finished_projects (exact names from \
         projects that are now done, may be empty), new_projects (short strings, may be empty), new_people (rarely; \
         name, role, department, user empty) and new_events (2-5 entries: date YYYY-MM-DD between today and four \
         weeks ahead, what). Keep it mundane: follow-ups of what happened, routine meetings, deadlines.",
        pretty(&n)?,
        day.format("%A"),
        iso(day),
        if n.revised.is_empty() { "the start" } else { &n.revised },
        if happened.is_empty() { "- nothing logged" } else { happened },
    );
    let mut r = ask(&user)?;
    let mut problems = check(&r, &n, day);
    if !problems.is_empty() {
        r = ask(&format!("{user}\n\nYour proposal was rejected:\n- {}\nSend the corrected JSON object.", problems.join("\n- ")))?;
        problems = check(&r, &n, day);
    }
    if !problems.is_empty() {
        let room = MAX_PROJECTS.saturating_sub(n.projects.len());
        let alone = |r: Revision| check(&Revision { summary: "x".into(), ..r }, &n, day).is_empty();
        r.finished_projects.retain(|p| n.projects.contains(p));
        r.new_projects.retain(|p| !n.projects.contains(p));
        r.new_projects.truncate(room);
        r.new_people.retain(|p| alone(Revision { new_people: vec![p.clone()], ..Default::default() }));
        r.new_events.retain(|e| alone(Revision { new_events: vec![e.clone()], ..Default::default() }));
        let left = check(&r, &n, day);
        if !left.is_empty() {
            bail!("revision unusable: {left:?}");
        }
    }
    Ok((apply(&r, &n, day), problems))
}

/// What each persona did in the 7 days before `day`, from the day logs.
pub fn happened(world: &World, personas: &[Persona], day: NaiveDate) -> Result<String> {
    let start = iso(day - Duration::days(7));
    let mut parts = vec![];
    for p in personas {
        let summary = world.summary(&p.user, day, 7)?;
        let lines: Vec<&str> = summary.lines().filter(|x| !x.starts_with("- ") || x.get(2..12).unwrap_or_default() >= start.as_str()).collect();
        if lines.iter().any(|x| x.starts_with("- ")) {
            parts.push(format!("{} ({}):\n{}", p.name, p.user, lines.join("\n")));
        }
    }
    Ok(parts.join("\n\n"))
}

#[derive(Debug)]
pub struct RevisionBusy(pub String);

impl std::fmt::Display for RevisionBusy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for RevisionBusy {}

#[derive(Debug)]
pub struct AutoRevision {
    pub narrative: Narrative,
    pub revised: bool,
    pub note: String,
    pub dropped: Vec<String>,
}

/// Revise the world's narrative when due and there is logged work; one planner at a time (world/revision.lock,
/// the same file Python locks).
pub fn revise_if_due(ask: &mut dyn FnMut(&str) -> Result<Revision>, world: &World, personas: &[Persona], day: NaiveDate) -> Result<AutoRevision> {
    let path = world.root.join("world").join("revision.lock");
    std::fs::create_dir_all(path.parent().unwrap())?;
    let lock = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(&path)?;
    if lock.try_lock().is_err() {
        return Err(RevisionBusy(format!("another planner is revising the narrative (lock {}); try again shortly", path.display())).into());
    }
    let stored = world.narrative()?;
    let n = with_personas(&stored, personas);
    if n.accounts != stored.accounts {
        world.save_narrative(&n)?;
    }
    if !due(&n, day) {
        let when = if n.revised.as_str() > iso(day).as_str() { "after" } else { "this week, on" };
        let note = format!("not due: narrative last revised {when} {}", n.revised);
        return Ok(AutoRevision { narrative: n, revised: false, note, dropped: vec![] });
    }
    let evidence = happened(world, personas, day)?;
    if evidence.is_empty() {
        let since = if n.revised.is_empty() { "narrative has no revision yet".to_string() } else { format!("since the revision of {}", n.revised) };
        let note = format!("due but no logged work in the 7 days before ({since}); kept as is");
        return Ok(AutoRevision { narrative: n, revised: false, note, dropped: vec![] });
    }
    let (revised, dropped) = revise(ask, &n, personas, day, &evidence)?;
    world.save_narrative(&revised)?;
    let note = format!("revised {}: {}", revised.revised, revised.history.last().map_or("", |e| e.what.as_str()));
    Ok(AutoRevision { narrative: revised, revised: true, note, dropped })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn narrative() -> Narrative {
        serde_json::from_value(serde_json::json!({
            "company": "Acme", "city": "Lund", "summary": "s", "projects": ["Old", "Keep"],
            "people": [{"name": "Anna Berg", "role": "r", "department": "d", "user": "aberg"}],
            "events": [{"date": "2026-11-03", "what": "Quarterly budget review with finance"}]
        }))
        .unwrap()
    }

    #[test]
    fn revise_corrects_once_then_drops_the_unusable_parts() {
        let day = NaiveDate::from_ymd_opt(2026, 11, 2).unwrap();
        let bad = Revision {
            summary: "Old wrapped up.".into(),
            finished_projects: vec!["Old".into(), "Ghost".into()],
            new_projects: vec!["Keep".into(), "New".into()],
            new_events: vec![
                Event { date: "2026-11-05".into(), what: "Quarterly budget review with finance".into() },
                Event { date: "2027-01-01".into(), what: "Far away".into() },
                Event { date: "2026-11-06".into(), what: "Kickoff for New".into() },
            ],
            ..Default::default()
        };
        let mut prompts = vec![];
        let (n, problems) = revise(&mut |u: &str| { prompts.push(u.to_string()); Ok(bad.clone()) }, &narrative(), &[], day, "").unwrap();
        assert_eq!(prompts.len(), 2);
        assert!(prompts[1].contains("Your proposal was rejected") && prompts[0].contains("- nothing logged"));
        assert_eq!(problems.len(), 4, "{problems:?}");
        assert_eq!(n.projects, ["Keep", "New"]);
        assert_eq!(n.events.iter().map(|e| e.date.as_str()).collect::<Vec<_>>(), ["2026-11-03", "2026-11-06"]);
        assert_eq!((n.revised.as_str(), n.history[0].what.as_str()), ("2026-11-02", "Old wrapped up."));
        assert!(!due(&n, NaiveDate::from_ymd_opt(2026, 11, 8).unwrap()) && due(&n, NaiveDate::from_ymd_opt(2026, 11, 9).unwrap()));
        assert!(revise(&mut |_: &str| Ok(bad.clone()), &n, &[], day, "").is_err(), "twice in one week");
    }
}
