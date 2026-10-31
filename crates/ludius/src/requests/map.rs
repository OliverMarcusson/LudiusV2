//! The report's side of manager requests (port of requests.py): the nightly planner maps each unanswered request
//! to a task, reads the mail first and mails a requested file back.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::Result;
use chrono::{Duration, NaiveDate};
use serde_json::{Value, json};

use super::plan::{DECISION_MIN, DERIVED, Item, MAPPED_MAX, READ_LEAD, SHOWN, addr, hhmm, iso, minutes, requestable};
use super::{days, find, mismatch, remember, seen};
use crate::decisions::{Decision, Options};
use crate::mail::{self, Message};
use crate::planner::{Person, PlanItem, World, load_plan};
use crate::policy::Policy;
use crate::reply::{RECENT_DAYS, py_repr};
use crate::tasks::{Persona, Template};

/// The boss's sent requests in `user`'s Inbox not yet mapped or answered, newest first, at most MAPPED_MAX.
pub fn pending(world: &World, user: &str, boss: &Person, inbox: Option<&[Message]>, policy: &Policy, date: NaiveDate) -> Result<Vec<Message>> {
    let mut done: BTreeSet<String> = seen(&world.root, user)?.into_keys().collect();
    let suffix = format!("-{user}.yaml");
    let mut plans: Vec<_> = std::fs::read_dir(world.root.join("plans")).map(|rd| rd.filter_map(|e| e.ok().map(|e| e.path())).collect()).unwrap_or_default();
    plans.retain(|p: &std::path::PathBuf| p.file_name().and_then(|n| n.to_str()).is_some_and(|n| n.ends_with(&suffix)));
    plans.sort();
    for p in plans {
        // an unreadable plan is skipped, as Python: its requests are mapped again, and planned() rejects repeats
        if let Ok(plan) = load_plan(&p, true) {
            done.extend(plan.items.into_iter().map(|it| it.request).filter(|r| !r.is_empty()));
        }
    }
    let sent: BTreeSet<String> = days(&world.root)?.into_iter().flat_map(|d| d.requests).filter(|r| r.to == user && r.status.as_deref() == Some("sent")).map(|r| r.msgid).collect();
    let since = (date - Duration::days(RECENT_DAYS)).and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp_millis();
    let from = addr(&boss.user);
    let mut out: Vec<Message> = inbox
        .unwrap_or_default()
        .iter()
        .filter(|m| m.sender == from && m.cls == "IPM.Note" && sent.contains(&m.msgid) && !m.subject.is_empty() && !done.contains(&m.msgid) && m.delivery_ms >= since && !policy.mail_reserved(&m.sender, &m.subject, &m.body))
        .cloned()
        .collect();
    out.sort_by_key(|m| -m.delivery_ms);
    out.truncate(MAPPED_MAX);
    Ok(out)
}

/// Each pending mail mapped to one plan item: the Decider picks the task (fail closed), the LLM fills it in.
#[allow(clippy::too_many_arguments)]
pub fn candidates(llm: &mut dyn FnMut(&str) -> Result<Item>, decide: &mut dyn FnMut(&Value, &Options) -> Result<Decision>, world: &World, persona: &Persona, boss: &Person, mails: &[Message], templates: &BTreeMap<String, Template>, date: NaiveDate, people: &BTreeMap<String, String>) -> Result<(Vec<PlanItem>, Vec<String>)> {
    let kinds = requestable(templates);
    let book: Vec<String> = people.iter().filter(|(u, _)| **u != persona.user).map(|(u, name)| format!("{name} <{}>", addr(u))).collect();
    let (mut items, mut notes) = (vec![], vec![]);
    for m in mails {
        let shown: String = mail::above_quote(&m.body).trim().chars().take(SHOWN).collect();
        let state = json!({"goal": "Your manager sent you this mail. Which one task does it ask you to do?", "you": format!("{}, {}", persona.name, persona.role), "mail": {"from": boss.name, "subject": m.subject, "body": shown}});
        let mut options: Options = kinds.iter().map(|(k, t)| (k.clone(), format!("{k}: {}", t.about))).collect();
        options.push(("none".into(), "none: it asks for none of these tasks, or for nothing to do".into()));
        let d = match decide(&state, &options) {
            Ok(d) => d,
            Err(e) => {
                // fail closed: no answer maps nothing (asked again next night)
                notes.push(format!("request {}: Decisions failed: {e:#}", py_repr(&m.subject)));
                continue;
            }
        };
        let p = d.probabilities.get(&d.choice).copied().unwrap_or(0.0);
        let Some(t) = kinds.get(&d.choice).filter(|_| p >= DECISION_MIN) else {
            remember(&world.root, &persona.user, &m.msgid, &format!("decision {} {p:.2}", d.choice))?;
            notes.push(format!("request {}: Decisions maps it to {} ({p:.2}), not planned", py_repr(&m.subject), d.choice));
            continue;
        };
        let params: Vec<&str> = t.params.iter().map(String::as_str).filter(|x| !DERIVED.contains(x)).collect();
        let reply = if t.cap() == "mail.reply" { format!(" For this reply: to is {} and subject is exactly {}.", m.sender, py_repr(&m.subject)) } else { String::new() };
        let user = format!(
            "Employee: {}, {} ({}). Usual hours {}-{}. Style: {}\nDay: {} {}.\n\nMail from {} <{}>, subject {}:\n<<<\n{shown}\n>>>\n\n\
             Task: {}: {}\nColleagues' addresses: {}\n\n\
             Return JSON {{\"at\": \"HH:MM\", \"params\": {{{}}}, \"context\": \"...\"}}: at within the usual hours; \
             params as strings, every one listed; use exactly the file name, recipient and subject the mail gives; \
             write the content in the employee's style, and repeat verbatim the words the mail uses for each thing \
             it asks to be covered; context: one sentence, why now.{reply}",
            persona.name,
            persona.role,
            persona.department,
            persona.hours.0,
            persona.hours.1,
            if persona.style.is_empty() { "ordinary" } else { &persona.style },
            date.format("%A"),
            iso(date),
            boss.name,
            m.sender,
            py_repr(&m.subject),
            t.task,
            t.about,
            book.join(", "),
            params.join(", "),
        );
        let item = llm(&user).and_then(|x| {
            let mut it = PlanItem { template: t.task.clone(), at: x.at, params: x.params, context: x.context, request: m.msgid.clone(), ..Default::default() };
            it.normalize()?;
            Ok(it)
        });
        match item {
            Ok(it) => items.push(it),
            Err(e) => notes.push(format!("request {}: parameters: {e:#}", py_repr(&m.subject))),
        }
    }
    Ok((items, notes))
}

/// A mail.read of the request mail READ_LEAD minutes before `it`, when it is still unread.
pub fn read_first(it: &PlanItem, inbox: Option<&[Message]>, templates: &BTreeMap<String, Template>, items: &[PlanItem]) -> Option<PlanItem> {
    let kind = templates.iter().find(|(_, t)| t.cap() == "mail.read")?.0;
    let m = inbox.unwrap_or_default().iter().find(|m| m.msgid == it.request && !m.read)?;
    let taken: BTreeSet<&str> = items.iter().map(|x| x.at.as_str()).collect();
    let mut t = minutes(&it.at)? - READ_LEAD;
    while t >= 0 && taken.contains(hhmm(t).as_str()) {
        t -= 1;
    }
    if t < 0 {
        return None;
    }
    Some(PlanItem {
        template: kind.clone(),
        at: hhmm(t),
        params: [("sender".to_string(), m.sender.clone()), ("subject".to_string(), m.subject.clone())].into_iter().collect(),
        context: format!("Reads the manager's mail {} before doing what it asks.", py_repr(&m.subject)),
        ..Default::default()
    })
}

/// Mail a requested file back to the manager once its write is done: saving it alone never reaches them.
pub fn deliver(it: &PlanItem, boss: &Person, inbox: Option<&[Message]>, templates: &BTreeMap<String, Template>, items: &[PlanItem]) -> Option<PlanItem> {
    let t = templates.get(&it.template).filter(|t| t.cap() == "file.write")?;
    let kind = templates.iter().find(|(_, x)| super::delivery(x))?.0;
    let m = inbox.unwrap_or_default().iter().find(|m| m.msgid == it.request)?;
    let mut values = t.defaults.clone();
    values.extend(it.values());
    let path = crate::template::format_map(t.writes.first()?, |k| values.get(k).cloned()).ok()?;
    let (folder, name) = path.rsplit_once('\\').unwrap_or(("", &path));
    if folder != "Documents" {
        return None;
    }
    let taken: BTreeSet<&str> = items.iter().map(|x| x.at.as_str()).collect();
    let mut at = minutes(&it.at)? + t.budget_minutes.ceil() as i64 + 1;
    while at < 24 * 60 && taken.contains(hhmm(at).as_str()) {
        at += 1;
    }
    if at >= 24 * 60 {
        return None;
    }
    let first = boss.name.split_whitespace().next().unwrap_or_default();
    let params = [
        ("to", addr(&boss.user)),
        ("to_name", boss.name.clone()),
        ("subject", format!("RE: {}", m.subject)),
        ("body", format!("hi {first}\nhere is {name} as you asked\n")),
        ("file_name", name.to_string()),
    ];
    Some(PlanItem {
        template: kind.clone(),
        at: hhmm(at),
        params: params.into_iter().map(|(k, v)| (k.to_string(), v)).collect(),
        context: format!("Sends {name} back to the manager, who asked for it in {}.", py_repr(&m.subject)),
        request: it.request.clone(),
        ..Default::default()
    })
}

/// Why `it` does not do what its request asked, if it does not.
pub fn planned(it: &PlanItem, templates: &BTreeMap<String, Template>, root: &Path) -> Option<String> {
    let ask = match find(&it.request, root) {
        Err(e) => return Some(format!("request records unreadable: {e:#}")),
        Ok(None) => return Some("no record of this request".into()),
        Ok(Some(a)) => a,
    };
    let Some(t) = templates.get(&it.template) else { return Some(format!("unknown template {}", py_repr(&it.template))) };
    let mut values: std::collections::HashMap<String, String> = t.defaults.clone().into_iter().collect();
    values.extend(it.values());
    mismatch(&ask, t, &values)
}

/// Unrequested items that already do what `it`'s request asks (same task, every include phrase).
pub fn duplicates(items: &[PlanItem], it: &PlanItem, root: &Path) -> Result<Vec<PlanItem>> {
    let Some(ask) = find(&it.request, root)? else { return Ok(vec![]) };
    let words: Vec<String> = ask.include.iter().map(|p| mail::letters(p)).collect();
    if words.is_empty() {
        return Ok(vec![]);
    }
    Ok(items
        .iter()
        .filter(|x| x.request.is_empty() && x.template == ask.task)
        .filter(|x| {
            let text = mail::letters(&x.values().into_values().collect::<Vec<_>>().join(" "));
            words.iter().all(|w| text.contains(w))
        })
        .cloned()
        .collect())
}
