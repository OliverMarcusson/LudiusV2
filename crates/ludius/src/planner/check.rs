//! Per-item plan checks (planner.py item_problem, _owned, _recipient, _meeting and friends).
use std::collections::{BTreeMap, BTreeSet};

use anyhow::Result;
use chrono::{Datelike, Duration, NaiveDate};
use indexmap::IndexMap;
use rand::SeedableRng;

use super::{BREAK, MEETING_DAYS_AHEAD, MEETING_MINUTES, MULTILINE, OWN_FILES, PlanItem, REACTION_ONLY, RECYCLE_AGE_DAYS, VALUE_MAX, WORK_DAY, chat_channels, fullmatch, leaf, re};
use crate::policy::Policy;
use crate::tasks::{ATTACHABLE, NAME_PARAMS, Task, Template, file_reserved, refers_to_existing, write_paths};
use crate::template::{casefold, format_map};

const MIN_COMPARE: usize = 12;
const HHMM: &str = r"([01]\d|2[0-3]):[0-5]\d";
const LOCATION: &str = r"[A-Za-z0-9][A-Za-z0-9 .-]{1,39}";
const MAIL_FOLDER: &str = r"[A-Za-z][A-Za-z0-9 -]{1,29}";
const DEFAULT_FOLDERS: [&str; 15] = [
    "inbox", "outbox", "drafts", "sent items", "deleted items", "junk email", "archive", "calendar", "contacts", "tasks", "notes", "journal",
    "conversation history", "rss feeds", "sync issues",
];
const DEVICE: &str = r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?";
const NOT_A_NAME: &str = r"[;,<>@]";
const GRID_ROWS: usize = 8;
const GRID_COLS: usize = 5;
const CELL: &str = r"[A-Za-z][A-Za-z0-9 .,'&/-]{0,29}|0|[1-9][0-9]{0,8}";
const OFFICE_LINE_MAX: usize = 120;
const OFFICE_LINE: &str = r"[A-Za-z0-9][A-Za-z0-9 ,.;:!?&%#+/-]{0,119}";
const AUTOFORMAT: &str = r"--| -|- |\b[13]/[24]\b|^\w{1,3}[.)] ";
const COMPLETES: [&str; 18] = [
    "january", "february", "march", "april", "june", "july", "august", "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
];

fn office_lines(key: &str) -> usize {
    match key {
        "body" | "notes" => 10,
        "bullets" | "bullets2" => 6,
        _ => 1,
    }
}

fn fill(s: &str, values: &IndexMap<String, String>) -> Result<String> {
    format_map(s, |k| values.get(k).cloned())
}

fn account(addr: &str) -> (String, String) {
    let a = addr.trim().to_lowercase();
    match a.split_once('@') {
        Some((u, d)) => (u.to_string(), d.to_string()),
        None => (a, String::new()),
    }
}

fn keys(s: &str) -> Vec<String> {
    re(r"\{(\w+)\}").captures_iter(s).map(|c| c[1].to_string()).collect()
}

/// What is wrong with one plan item on its own, if anything (planner.item_problem).
pub fn item_problem(it: &PlanItem, templates: &BTreeMap<String, Template>, policy: &Policy, user: &str, teams: &[String], shares: &[String]) -> Result<Option<String>> {
    if it.template == BREAK {
        return Ok((!(1..=120).contains(&it.minutes)).then(|| "break needs 1-120 minutes".into()));
    }
    let Some(t) = templates.get(&it.template) else { return Ok(Some(format!("unknown template {:?}", it.template))) };
    let values = it.values();
    if values.contains_key("date")
        && let Some(why) = meeting(&values)
    {
        return Ok(Some(why));
    }
    let task = match Task::new(t.clone(), it.instance(), &mut rand::rngs::StdRng::seed_from_u64(0)) {
        Ok(task) => task,
        Err(e) => return Ok(Some(e.to_string())),
    };
    let cap = t.cap();
    let get = |k: &str| task.values.get(k).cloned().unwrap_or_default();
    if REACTION_ONLY.contains(&cap) {
        return Ok(Some(format!("{} is read by chat reactions only (their outcome check is the server's read marker)", it.template)));
    }
    let channels: Vec<String> = chat_channels()?.into_iter().chain(teams.iter().cloned()).collect();
    if cap == "chat.post" && !task.values.get("channel").is_some_and(|c| channels.contains(c)) {
        return Ok(Some(format!("channel {:?} is not one of {channels:?}", get("channel"))));
    }
    let folder = get("folder");
    if cap == "mail.file" && (!fullmatch(MAIL_FOLDER, &folder) || DEFAULT_FOLDERS.contains(&casefold(&folder).as_str())) {
        return Ok(Some("folder: 2-30 letters, digits, spaces or '-', not one of Outlook's own folders".into()));
    }
    if cap == "file.share" && !task.values.get("share").is_some_and(|s| shares.contains(s)) {
        let mut sorted = shares.to_vec();
        sorted.sort();
        return Ok(Some(format!("share {:?} is not one of this persona's team shares {sorted:?}", get("share"))));
    }
    let message = get("message").trim().chars().count();
    if ["chat.post", "chat.dm", "chat.file"].contains(&cap) && !(2..=200).contains(&message) {
        return Ok(Some("message: one line of 2-200 characters".into()));
    }
    if cap == "file.download" && !download_listed(&get("file_name"), &get("page"), policy) {
        return Ok(Some("file_name and page: one download and its page from the intranet downloads list, exactly".into()));
    }
    for (name, sg) in &t.subgoals {
        if let Some(check) = sg.done.as_ref().and_then(|d| d.element.as_ref()).and_then(|e| e.value.as_ref())
            && task.fmt(check)?.chars().count() > VALUE_MAX
        {
            return Ok(Some(format!("{name}: text too long, keep it under {} characters in total", VALUE_MAX - 20)));
        }
    }
    let office = office_keys(t);
    for slide in t.dones().filter_map(|d| d.file.as_ref()).filter_map(|f| f.slides.as_ref()).flatten() {
        let filled: BTreeSet<bool> = keys(slide).iter().map(|k| task.values.get(k).is_some_and(|v| !v.is_empty())).collect();
        if filled.len() > 1 {
            return Ok(Some(format!("a slide is planned whole or not at all: {slide:?}")));
        }
    }
    for (key, value) in &values {
        let typed = if key == "rows" { value.replace('\t', " ") } else { value.clone() };
        if !fullmatch(r"[\x20-\x7e\n]*", &typed) {
            return Ok(Some(format!("{key}: non-ASCII text cannot be typed")));
        }
        if value.contains('\n') && !MULTILINE.contains(&key.as_str()) {
            return Ok(Some(format!("{key}: must be one line")));
        }
        if key == "rows"
            && let Some(why) = grid(value)
        {
            return Ok(Some(format!("rows: {why}")));
        }
        if office.contains(key)
            && !value.is_empty()
            && let Some(why) = office_text(value, office_lines(key))
        {
            return Ok(Some(format!("{key}: {why}")));
        }
        if key.ends_with("_name") && (re(r#"[<>:"/\\|?*]"#).is_match(value) || value != value.trim() || value.ends_with('.') || fullmatch(DEVICE, value)) {
            return Ok(Some(format!("{key}: not a valid file name")));
        }
        if let Some(r) = policy.text_allowed(value, None) {
            return Ok(Some(format!("{key}: {r}")));
        }
    }
    for (name, sg) in &t.subgoals {
        if !sg.search.is_empty()
            && let Some(r) = policy.text_allowed(&sg.search, None)
        {
            return Ok(Some(format!("{name}: search text: {r}")));
        }
    }
    for rel in write_paths(&task)? {
        if file_reserved(user, &rel, policy) {
            return Ok(Some(format!("{rel}: scenario-reserved file, choose a different name")));
        }
    }
    Ok(None)
}

#[cfg(feature = "intranet")]
fn download_listed(file: &str, page: &str, policy: &Policy) -> bool {
    crate::intranet::downloads(&crate::intranet::dir(), |t| policy.page_reserved(t)).is_ok_and(|d| d.iter().any(|(f, p)| f == file && p == page))
}

#[cfg(not(feature = "intranet"))]
fn download_listed(_file: &str, _page: &str, _policy: &Policy) -> bool {
    false
}

/// A meeting's location, weekday date and work-hours time span (planner._meeting).
pub(crate) fn meeting(values: &IndexMap<String, String>) -> Option<String> {
    let get = |k: &str| values.get(k).map(String::as_str).unwrap_or("");
    if !fullmatch(LOCATION, get("location")) {
        return Some("location: 2-40 letters, digits, spaces, '-' or '.' (no @, comma or semicolon)".into());
    }
    let Ok(date) = NaiveDate::parse_from_str(get("date"), "%Y-%m-%d") else { return Some("date: must be YYYY-MM-DD".into()) };
    if date.weekday().num_days_from_monday() >= 5 || get("date").len() != 10 {
        return Some("date: must be a weekday (Monday-Friday) as YYYY-MM-DD".into());
    }
    let (start, end) = (get("start"), get("end"));
    if !(fullmatch(HHMM, start) && fullmatch(HHMM, end)) {
        return Some("start and end: must be HH:MM".into());
    }
    let minutes = super::minutes(end) - super::minutes(start);
    if !(WORK_DAY.0 <= start && end <= WORK_DAY.1) || !(MEETING_MINUTES.0..=MEETING_MINUTES.1).contains(&minutes) {
        return Some(format!("a meeting runs {}-{} minutes between {} and {}", MEETING_MINUTES.0, MEETING_MINUTES.1, WORK_DAY.0, WORK_DAY.1));
    }
    None
}

/// A meeting is set after the plan's day and within MEETING_DAYS_AHEAD of it (planner._meeting_day).
pub(crate) fn meeting_day(it: &PlanItem, day: &str) -> Option<String> {
    let date = it.value("date")?;
    let last = NaiveDate::parse_from_str(day, "%Y-%m-%d").ok()? + Duration::days(MEETING_DAYS_AHEAD);
    let ok = day < date.as_str() && date.as_str() <= last.format("%Y-%m-%d").to_string().as_str();
    (!ok).then(|| format!("date: a meeting is set after {day} and within {MEETING_DAYS_AHEAD} days of it"))
}

/// `to` is a colleague's address and `to_name` that colleague's story name (planner._recipient).
pub(crate) fn recipient(it: &PlanItem, colleagues: &BTreeSet<String>, people: &BTreeMap<String, String>) -> Option<String> {
    let to = it.value("to")?;
    let (user, domain) = account(to);
    if domain != crate::mail::domain() || !colleagues.contains(&user) {
        return Some(format!("to {to:?} is not a colleague's address (<account>@{})", crate::mail::domain()));
    }
    let name = it.value("to_name").map(String::as_str).unwrap_or("");
    if re(NOT_A_NAME).is_match(name) || Some(name) != people.get(&user).map(String::as_str) {
        return Some(format!("to_name {name:?} is not {user:?}'s name from the company story"));
    }
    if !it.handoff.is_empty() && it.handoff != user {
        return Some(format!("handoff {:?} must be the mail's recipient {user:?}", it.handoff));
    }
    None
}

pub(crate) fn sender(it: &PlanItem, colleagues: &BTreeSet<String>) -> Option<String> {
    let sender = it.value("sender")?;
    let (user, domain) = account(sender);
    (domain != crate::mail::domain() || !colleagues.contains(&user)).then(|| format!("sender {sender:?} is not a colleague's address"))
}

/// An edit, move, share or attachment names one of the persona's own files (planner._owned).
pub fn owned_problem(it: &PlanItem, t: &Template, owned: &BTreeMap<String, String>, day: &str, items: &[PlanItem]) -> Option<String> {
    let cap = t.cap();
    let recycle = cap == "file.recycle";
    let files: Vec<_> = t.dones().filter_map(|d| d.file.as_ref()).collect();
    let shared: Vec<String> = files.iter().filter(|f| f.share.is_some()).map(|f| f.path.clone()).collect();
    let sources: Vec<String> =
        files.iter().filter_map(|f| f.source.clone().or(f.export_of.clone()).or(f.extract_of.clone()).or(f.moved_from.clone())).filter(|s| !s.is_empty()).collect();
    let attaches = t.attaches();
    let named = if recycle || cap == "file.organize" {
        &t.removes
    } else if !shared.is_empty() {
        &shared
    } else if !sources.is_empty() {
        &sources
    } else if !attaches.is_empty() {
        &attaches
    } else {
        &t.writes
    };
    let values = it.values();
    let Ok(paths) = named.iter().map(|w| fill(w, &values)).collect::<Result<Vec<_>>>() else { return Some("missing parameters".into()) };
    if paths.is_empty() || paths.iter().any(|p| !owned.contains_key(&casefold(p))) {
        let verb = if recycle {
            "recycle"
        } else if !t.removes.is_empty() {
            "move"
        } else if !shared.is_empty() {
            "copy"
        } else if !sources.is_empty() {
            "reuse"
        } else {
            "edit"
        };
        let verb = if attaches.is_empty() { verb } else { "attach" };
        return Some(format!("file_name: {verb} only one of this persona's own files (Own recent files), named exactly"));
    }
    if cap == "file.export" && t.writes.iter().any(|w| fill(w, &values).is_ok_and(|p| owned.contains_key(&casefold(&p)))) {
        return Some("file_name: this file already has that copy, choose a different one".into());
    }
    if !attaches.is_empty() && !paths.iter().all(|p| ATTACHABLE.iter().any(|x| p.to_lowercase().ends_with(x))) {
        return Some(format!("file_name: attach only {} files", ATTACHABLE.join(", ")));
    }
    if recycle {
        let cutoff = NaiveDate::parse_from_str(day, "%Y-%m-%d").map(|d| (d - Duration::days(RECYCLE_AGE_DAYS)).format("%Y-%m-%d").to_string()).unwrap_or_default();
        if paths.iter().any(|p| owned[&casefold(p)] > cutoff) {
            return Some(format!("file_name: recycle only an own file untouched for {RECYCLE_AGE_DAYS} days or more"));
        }
        let name = casefold(it.params.get("file_name").map(String::as_str).unwrap_or(""));
        if items.iter().any(|x| !std::ptr::eq(x, it) && x.values().values().any(|v| casefold(v) == name)) {
            return Some("file_name: another item today uses this file, recycle a different one".into());
        }
    }
    None
}

/// Does this item edit, move, share or attach one of the persona's files (and so need `owned_problem`)?
pub(crate) fn uses_own_file(t: &Template) -> bool {
    OWN_FILES.contains(&t.cap()) || !t.attaches().is_empty()
}

/// Files an earlier item of the same request writes today: its delivery may attach them (planner.asked_files).
pub fn asked_files(it: &PlanItem, items: &[PlanItem], templates: &BTreeMap<String, Template>, day: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for x in items {
        let Some(t) = templates.get(&x.template) else { continue };
        if it.request.is_empty() || x.request != it.request || x.at >= it.at || t.cap() != "file.write" {
            continue;
        }
        let mut values = t.defaults.clone();
        values.extend(x.values());
        if let Ok(ws) = t.writes.iter().map(|w| fill(w, &values)).collect::<Result<Vec<_>>>() {
            out.extend(ws.iter().map(|w| (casefold(w), day.to_string())));
        }
    }
    out
}

/// The names an item creates (file, folder, new name), not those that refer to existing things.
pub fn targets(it: &PlanItem, templates: &BTreeMap<String, Template>) -> Vec<(String, String)> {
    let skip = templates.get(&it.template).map(|t| refers_to_existing(t.cap())).unwrap_or(&[]);
    it.values().into_iter().filter(|(k, _)| NAME_PARAMS.contains(&k.as_str()) && !skip.contains(&k.as_str())).collect()
}

pub fn taken_names<'a>(paths: impl IntoIterator<Item = &'a str>, with_stem: bool) -> BTreeSet<String> {
    let mut out = BTreeSet::new();
    for p in paths {
        let l = casefold(&leaf(p));
        let s = if with_stem { re(r"\.[^.]+$").replace(&l, "").into_owned() } else { l.clone() };
        out.extend([l, s].into_iter().filter(|n| !n.is_empty()));
    }
    out
}

/// The item's new content (not names of existing things) long enough to compare for near-duplicates.
pub fn content_values(it: &PlanItem, templates: &BTreeMap<String, Template>) -> Vec<String> {
    let Some(t) = templates.get(&it.template) else { return vec![] };
    let skip = refers_to_existing(t.cap());
    it.values().into_iter().filter(|(k, v)| t.params.contains(k) && !skip.contains(&k.as_str()) && v.chars().count() >= MIN_COMPARE).map(|(_, v)| v).collect()
}

pub(crate) fn grid(rows: &str) -> Option<String> {
    let grid: Vec<Vec<&str>> = rows.trim_matches('\n').split('\n').map(|l| l.split('\t').collect()).collect();
    if !(1..=GRID_ROWS).contains(&grid.len()) || grid.iter().any(|r| !(1..=GRID_COLS).contains(&r.len())) {
        return Some(format!("1-{GRID_ROWS} lines of 1-{GRID_COLS} tab-separated cells"));
    }
    for (r, row) in grid.iter().enumerate() {
        for (c, cell) in row.iter().enumerate() {
            if !fullmatch(CELL, cell) || *cell != cell.trim() {
                return Some(format!("cell {cell:?}: a short text starting with a letter, or a whole number"));
            }
            let cf = casefold(cell);
            let above = grid[..r].iter().filter_map(|g| g.get(c)).map(|a| casefold(a));
            if cell.starts_with(|ch: char| ch.is_ascii_alphabetic()) && above.into_iter().any(|a| a != cf && a.starts_with(&cf)) {
                return Some(format!("cell {cell:?}: Excel would complete it to a longer cell above it"));
            }
        }
    }
    None
}

fn office_keys(t: &Template) -> BTreeSet<String> {
    let mut text = String::new();
    for f in t.dones().filter_map(|d| d.file.as_ref()) {
        for s in [f.paragraphs.as_deref(), f.comment.as_deref(), f.append.as_deref().filter(|_| f.path.to_lowercase().ends_with(".docx"))].into_iter().flatten() {
            text += s;
            text += " ";
        }
        for s in f.slides.iter().flatten() {
            text += s;
            text += " ";
        }
    }
    keys(&text).into_iter().collect()
}

pub(crate) fn office_text(value: &str, max_lines: usize) -> Option<String> {
    let lines: Vec<&str> = value.trim_matches('\n').split('\n').collect();
    if !(1..=max_lines).contains(&lines.len()) {
        return Some(format!("1-{max_lines} lines"));
    }
    for line in lines {
        if !fullmatch(OFFICE_LINE, line) || line != line.trim() || re(AUTOFORMAT).is_match(line) {
            return Some(format!(
                "line {line:?}: up to {OFFICE_LINE_MAX} chars from a letter or digit: letters, digits, spaces and , . ; : ! ? & % # + / -; no ' - ', '--' or 1/2; not like a list ('a) ', '1. ')"
            ));
        }
        let last = casefold(line.split_whitespace().last().unwrap_or(""));
        if last.len() >= 4 && COMPLETES.iter().any(|w| *w != last && w.starts_with(&last)) {
            return Some(format!("line {line:?}: Word would complete {last:?} to a month or day name"));
        }
    }
    None
}

