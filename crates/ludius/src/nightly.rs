//! Nightly planning (port of nightly.py, the part outside Temporal): plan each persona's next day once. An existing
//! plan is kept as it is (checked, never regenerated or overwritten); the Temporal schedule, workflow and activity
//! come with the workers (step 4c).
use std::collections::{BTreeMap, BTreeSet};
use std::fs::File;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use anyhow::{Context as _, Result};
use chrono::{DateTime, Duration, NaiveDate, Utc};
use serde::{Deserialize, Serialize};

use crate::decisions::DecisionsClient;
use crate::llm::LlmClient;
use crate::mail::{self, Message};
use crate::planner::{self, Context, DayPlan, PlanItem, World, load_plan, owned_files, role_templates};
use crate::policy::Policy;
use crate::requests::{self, Asks, Item, MAP_SYSTEM, SYSTEM};
use crate::revision::{Revision, revise_if_due};
use crate::tasks::{Persona, Template};
use crate::{reply, world_state};

pub const PARALLEL: usize = 8;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Planned {
    pub persona: String,
    pub date: String,
    pub path: String,
    pub generated: bool,
    pub items: usize,
    pub rejects: Vec<String>,
    pub note: String,
}

/// The plan file exists but cannot be kept: never retried, never overwritten.
#[derive(Debug)]
pub struct PlanConflict(pub String);

impl std::fmt::Display for PlanConflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for PlanConflict {}

pub struct Sources<'a> {
    pub personas: Vec<Persona>,
    pub templates: BTreeMap<String, Template>,
    pub policy: Policy,
    /// The persona's newest Inbox mail; None when the mail server cannot be read.
    pub inbox: &'a (dyn Fn(&str) -> Option<Vec<Message>> + Sync),
    pub decider: Option<&'a (dyn Fn() -> Result<DecisionsClient> + Sync)>,
}

pub struct Generated {
    pub plan: DayPlan,
    pub rejects: Vec<String>,
    pub note: String,
}

/// Generates one persona's day: `generate_day` with a fresh client in production, a fake in tests.
pub type Generate<'a> = &'a (dyn Fn(&Persona, NaiveDate, &Shared) -> Result<Generated> + Sync);

/// The world files every persona's planning touches ("" = the narrative and world state, else a manager's
/// request day): threads wait on each other, another process holding the file lock is an error to retry.
pub struct Shared {
    root: PathBuf,
    keys: Mutex<BTreeMap<String, Arc<Mutex<()>>>>,
}

impl Shared {
    pub fn new(world: &World) -> Self {
        Self { root: world.root.join("locks"), keys: Mutex::default() }
    }

    pub fn hold<T>(&self, key: &str, f: impl FnOnce() -> Result<T>) -> Result<T> {
        let m = self.keys.lock().unwrap_or_else(|e| e.into_inner()).entry(key.to_string()).or_default().clone();
        let _thread = m.lock().unwrap_or_else(|e| e.into_inner());
        std::fs::create_dir_all(&self.root)?;
        let path = self.root.join(if key.is_empty() { "nightly.lock".into() } else { format!("nightly-{key}.lock") });
        let file = File::options().read(true).write(true).create(true).truncate(false).open(&path)?;
        file.try_lock().map_err(|_| anyhow::anyhow!("another planner holds {}; retrying later", path.display()))?;
        f()
    }
}

/// The day a schedule firing at `occurrence` plans: the day after its local date in `tz`.
pub fn planning_date(occurrence: DateTime<Utc>, tz: &str) -> Result<NaiveDate> {
    let tz: chrono_tz::Tz = tz.parse().map_err(|_| anyhow::anyhow!("unknown time zone {tz:?}"))?;
    Ok(occurrence.with_timezone(&tz).date_naive() + Duration::days(1))
}

fn day_ms(date: NaiveDate) -> i64 {
    date.and_hms_opt(0, 0, 0).unwrap().and_utc().timestamp_millis()
}

fn account(sender: &str) -> String {
    sender.split('@').next().unwrap_or("").to_string()
}

fn member(groups: &indexmap::IndexMap<String, Vec<String>>, user: &str) -> Vec<String> {
    groups.iter().filter(|(_, m)| m.iter().any(|x| x == user)).map(|(c, _)| c.clone()).collect()
}

/// Why validate rejects item `i` of `items`, if it does.
fn rejected(items: &[PlanItem], i: usize, c: &Context) -> Result<Option<String>> {
    Ok(planner::validate(items, c)?.into_iter().find(|(k, _)| *k == i).map(|(_, why)| why))
}

/// Plan `persona`'s `date`: revise the story if due, have the manager's requests sent, map the ones waiting in
/// the persona's Inbox, write the day with the LLM and merge the mapped requests in.
pub fn generate_day(llm: &LlmClient, world: &World, persona: &Persona, src: &Sources, date: NaiveDate, shared: &Shared) -> Result<Generated> {
    let user = persona.user.as_str();
    let (auto, state, summary) = shared.hold("", || {
        let auto = revise_if_due(&mut |u| llm.json::<Revision>(planner::make::NARRATIVE_SYSTEM, u, 0.9, 2), world, &src.personas, date)?;
        let state = world_state::build(world, &auto.narrative.events, &src.personas, date)?;
        world_state::save(&state, world)?;
        let summary = world.summary(user, date, 3)?;
        Ok((auto, state, summary))
    })?;
    let n = &auto.narrative;
    let people = n.people_names();
    let mut notes = vec![];
    let mut mapped: Vec<PlanItem> = vec![];
    let boss = requests::manager_of(n, user);
    match &boss {
        Err(why) => notes.push(format!("requests: {why}")),
        Ok(boss) => {
            let reports: Vec<&str> = src.personas.iter().map(|p| p.user.as_str()).filter(|u| *u == user || requests::manager_of(n, u).as_ref() == Ok(boss)).collect();
            let briefs = reports.iter().map(|u| (u.to_string(), world_state::brief(&state, u, date))).collect();
            let inboxes = reports.iter().map(|u| (u.to_string(), (src.inbox)(u))).collect();
            let existing = reports.iter().map(|u| Ok((u.to_string(), world_state::existing_files(world, u)?))).collect::<Result<_>>()?;
            let send = |a: &str, b: &str, c: &str, d: &str, e: &str, f: &str, g: &str| mail::deliver(a, b, c, d, e, f, g);
            let day = shared.hold(&boss.user, || {
                requests::ensure(&mut |u| llm.json::<Asks>(SYSTEM, u, 0.8, 1), &send, world, n, &briefs, &src.personas, &src.templates, &src.policy, date, boss, &inboxes, &existing)
            })?;
            notes.extend(day.rejects.iter().map(|r| format!("requests by {}: {r}", boss.user)));
            notes.extend(day.requests.iter().map(|r| format!("requests by {}: {} to {} {}", boss.user, r.status.as_deref().unwrap_or("None"), r.to, reply::py_repr(&r.subject))));
        }
    }
    let inbox_mails = (src.inbox)(user);
    let decider = src.decider.map(|d| d()).transpose()?;
    let role = role_templates(&src.templates, persona);
    if let (Ok(boss), Some(decider)) = (&boss, &decider) {
        let mails = shared.hold("", || requests::pending(world, user, boss, inbox_mails.as_deref(), &src.policy, date))?;
        let (items, more) = requests::candidates(&mut |u| llm.json::<Item>(MAP_SYSTEM, u, 0.7, 1), &mut |s, o| decider.decide(s, o, false, None), world, persona, boss, &mails, &role, date, &people)?;
        mapped = items;
        notes.extend(more);
    }
    let mails = inbox_mails.as_deref().unwrap_or_default();
    let ids: BTreeSet<&str> = mapped.iter().map(|it| it.request.as_str()).collect();
    let asked: BTreeSet<&str> = mails.iter().filter(|m| ids.contains(m.msgid.as_str())).map(|m| m.subject.as_str()).collect();
    let inbox = reply::hints(inbox_mails.as_deref(), user, &people, &src.policy, "IPM.Note", Some(day_ms(date)));
    let unread: BTreeSet<(String, String)> = mails.iter().filter(|m| !m.read).map(|m| (account(&m.sender), m.subject.clone())).collect();
    let files: BTreeMap<(String, String), Vec<String>> = mails.iter().filter(|m| !m.files.is_empty()).map(|m| ((account(&m.sender), m.subject.clone()), m.files.clone())).collect();
    let domain = mail::domain();
    let mut listed: String = inbox
        .iter()
        .filter(|(_, s)| !asked.contains(s.as_str()))
        .map(|k| {
            let unread = if unread.contains(k) { " (unread)" } else { "" };
            let attached = files.get(k).map(|f| format!(" (attached: {})", f.join(", "))).unwrap_or_default();
            format!("- from {}@{domain}: {}{unread}{attached}", k.0, k.1)
        })
        .collect::<Vec<_>>()
        .join("\n");
    if !listed.is_empty() {
        listed = format!("Inbox on the mail server (newest first; a reply answers one of these):\n{listed}");
    }
    let invites = reply::hints(inbox_mails.as_deref(), user, &people, &src.policy, mail::MEETING_REQUEST, None);
    if !invites.is_empty() {
        listed += "\nMeeting invitations waiting for an answer (an answer accepts or declines one):\n";
        listed += &invites.iter().map(|(u, s)| format!("- from {u}@{domain}: {s}")).collect::<Vec<_>>().join("\n");
    }
    let planned = if mapped.is_empty() {
        String::new()
    } else {
        let lines: Vec<String> = mapped.iter().map(|it| format!("- {} {}: {}", it.at, it.template, it.context)).collect();
        format!("Already planned from the manager's requests (keep these times free, do not plan them again):\n{}", lines.join("\n"))
    };
    let world_text = [summary, world_state::brief(&state, user, date), listed, planned].into_iter().filter(|x| !x.is_empty()).collect::<Vec<_>>().join("\n");
    let followups: indexmap::IndexMap<String, String> = world_state::due(&state, user, date).into_iter().map(|f| (f.id.clone(), f.about.clone())).collect();
    let threads = world_state::mail_threads(&state, user);
    let existing = world_state::existing_files(world, user)?;
    let owned = owned_files(&world.root, user, &date.format("%Y-%m-%d").to_string())?;
    let inbox: BTreeSet<(String, String)> = inbox.into_iter().collect();
    let invites: BTreeSet<(String, String)> = invites.into_iter().collect();
    let d = planner::Day {
        narrative: n,
        persona,
        date,
        world: world_text,
        history: world.history(user)?,
        policy: src.policy.clone(),
        scenario: None,
        followups: followups.clone(),
        existing: existing.clone(),
        mail_threads: threads.clone(),
        inbox: inbox.clone(),
        owned: owned.clone(),
        invites: invites.clone(),
        unread: unread.clone(),
        taken: mapped.iter().map(|it| it.at.clone()).collect(),
        attached: files,
    };
    let (mut plan, mut rejects) = planner::plan_day(llm, &d, &src.templates, decider.as_ref())?;

    // a planned read of a mail that is not unread (any more) would fail: drop it
    let (stale, keep): (Vec<PlanItem>, Vec<PlanItem>) = plan.items.into_iter().partition(|x| {
        role.get(&x.template).is_some_and(|t| t.cap() == "mail.read")
            && !unread.contains(&(account(x.params.get("sender").map(String::as_str).unwrap_or("").trim()).to_lowercase(), x.params.get("subject").cloned().unwrap_or_default()))
    });
    plan.items = keep;
    rejects.extend(stale.iter().map(|x| format!("{} at {}: dropped, the mail is not unread", x.template, x.at)));

    let ctx = |colleagues: BTreeSet<String>, history: Vec<String>, full: bool| Context {
        templates: role.clone(),
        history,
        policy: src.policy.clone(),
        colleagues,
        scenario: None,
        followups: followups.keys().cloned().collect(),
        existing: existing.clone(),
        mail_threads: threads.clone(),
        people: people.clone(),
        user: user.to_string(),
        day: date.format("%Y-%m-%d").to_string(),
        inbox: inbox.clone(),
        teams: if full { member(&n.channels, user) } else { vec![] },
        owned: if full { owned.clone() } else { BTreeMap::new() },
        shares: if full { member(&n.shares, user) } else { vec![] },
        invites: if full { invites.clone() } else { BTreeSet::new() },
    };
    let colleagues = n.colleagues(user);
    for mut it in mapped {
        let dups = requests::duplicates(&plan.items, &it, &world.root)?;
        let rest: Vec<PlanItem> = plan.items.iter().filter(|x| !dups.contains(x)).cloned().collect();
        while rest.iter().any(|x| x.at == it.at) {
            let m = (it.at[..2].parse::<u32>()? * 60 + it.at[3..].parse::<u32>()? + 1) % (24 * 60);
            it.at = format!("{:02}:{:02}", m / 60, m % 60);
        }
        planner::fill_recipient_names(std::slice::from_mut(&mut it), &people);
        let all = [rest.clone(), vec![it.clone()]].concat();
        let why = match rejected(&all, rest.len(), &ctx(colleagues.clone(), world.history(user)?, true))? {
            Some(w) => Some(w),
            None => requests::planned(&it, &src.templates, &world.root),
        };
        if let Some(why) = why {
            requests::remember(&world.root, user, &it.request, &format!("rejected: {why}"))?;
            rejects.push(format!("request {} at {}: {why}", it.template, it.at));
            continue;
        }
        plan.items = all;
        plan.items.sort_by(|a, b| a.at.cmp(&b.at));
        rejects.extend(dups.iter().map(|x| format!("{} at {}: dropped, it repeats the request {}", x.template, x.at, it.request)));
        if let Some(read) = requests::read_first(&it, inbox_mails.as_deref(), &role, &plan.items) {
            let others: Vec<PlanItem> = plan.items.iter().filter(|x| !(x.template == read.template && x.params.get("subject") == read.params.get("subject"))).cloned().collect();
            let c = ctx(colleagues.clone(), vec![], false);
            let all = [others.clone(), vec![read]].concat();
            match rejected(&all, others.len(), &c)? {
                Some(why) => rejects.push(format!("read before request {} at {}: {why}", it.template, it.at)),
                None => {
                    plan.items = all;
                    plan.items.sort_by(|a, b| a.at.cmp(&b.at));
                }
            }
        }
        let Ok(boss) = &boss else { continue };
        if let Some(back) = requests::deliver(&it, boss, inbox_mails.as_deref(), &role, &plan.items) {
            let mut c = ctx(colleagues.iter().cloned().chain([boss.user.clone()]).collect(), vec![], true);
            (c.shares, c.invites) = (vec![], BTreeSet::new());
            let all = [plan.items.clone(), vec![back.clone()]].concat();
            let why = match rejected(&all, plan.items.len(), &c)? {
                Some(w) => Some(w),
                None => requests::planned(&back, &role, &world.root),
            };
            match why {
                Some(why) => rejects.push(format!("mail back request {} at {}: {why}", it.template, it.at)),
                None => {
                    plan.items = all;
                    plan.items.sort_by(|a, b| a.at.cmp(&b.at));
                }
            }
        }
    }
    rejects.extend(notes);
    Ok(Generated { plan, rejects, note: auto.note })
}

/// The plan already on disk, if it is this persona's day and still valid; else a PlanConflict.
pub fn check_existing(world: &World, persona: &Persona, date: NaiveDate, src: &Sources) -> Result<DayPlan> {
    let (user, day) = (persona.user.as_str(), date.format("%Y-%m-%d").to_string());
    let path = world.plan_path(user, &day);
    let conflict = |why: String| anyhow::Error::new(PlanConflict(why));
    let plan = load_plan(&path, false).map_err(|e| conflict(format!("{} exists but is not a readable plan: {e:#}", path.display())))?;
    if (plan.persona.as_str(), plan.date.as_str()) != (user, day.as_str()) {
        return Err(conflict(format!("{} is the plan of {} on {}, not {user} on {day}", path.display(), plan.persona, plan.date)));
    }
    let n = world.narrative()?;
    let state = world_state::build(world, &n.events, &src.personas, date)?;
    let people = n.people_names();
    let inbox_mails = (src.inbox)(user);
    let inbox: BTreeSet<(String, String)> = reply::hints(inbox_mails.as_deref(), user, &people, &src.policy, "IPM.Note", Some(day_ms(date))).into_iter().collect();
    let invites = reply::hints(inbox_mails.as_deref(), user, &people, &src.policy, mail::MEETING_REQUEST, None).into_iter().collect();
    // a thread whose mail left the recent Inbox can no longer be answered; an unreadable Inbox keeps them all
    let mail_threads = world_state::mail_threads(&state, user).into_iter().filter(|(_, t)| inbox_mails.is_none() || inbox.contains(t)).collect();
    let c = Context {
        templates: role_templates(&src.templates, persona),
        history: vec![], // this plan's own content is already in the history
        policy: src.policy.clone(),
        colleagues: n.colleagues(user).into_iter().chain(n.boss(user)).collect(),
        scenario: None,
        followups: plan.items.iter().filter(|i| !i.followup.is_empty()).map(|i| i.followup.clone()).collect(),
        existing: vec![],
        mail_threads,
        people,
        user: user.to_string(),
        day: day.clone(),
        inbox,
        teams: member(&n.channels, user),
        owned: owned_files(&world.root, user, &day)?,
        shares: member(&n.shares, user),
        invites,
    };
    let rejects = planner::validate(&plan.items, &c)?;
    if !rejects.is_empty() {
        let why: Vec<String> = rejects.iter().map(|(i, w)| format!("item {i}: {w}")).collect();
        return Err(conflict(format!("{} fails validation: {}", path.display(), why.join("; "))));
    }
    Ok(plan)
}

/// Plan `persona`'s `date` once: an existing plan is checked and kept (its history repaired), never regenerated.
pub fn ensure_day(generate: Generate, world: &World, persona: &Persona, src: &Sources, date: NaiveDate, shared: &Shared) -> Result<Planned> {
    let path = world.plan_path(&persona.user, &date.format("%Y-%m-%d").to_string());
    let planned = |date: &str, generated, items, rejects, note| Planned { persona: persona.user.clone(), date: date.to_string(), path: path.display().to_string(), generated, items, rejects, note };
    if path.exists() {
        let plan = check_existing(world, persona, date, src)?;
        world.repair_history(&plan, &src.templates).map_err(|e| PlanConflict(format!("cannot restore the content history of {}: {e:#}", path.display())))?;
        return Ok(planned(&plan.date, false, plan.items.len(), vec![], String::new()));
    }
    let g = generate(persona, date, shared)?;
    world.save_plan(&g.plan, &src.templates, true)?;
    Ok(planned(&g.plan.date, true, g.plan.items.len(), g.rejects, g.note))
}

/// Plan every persona's `date`, PARALLEL at a time; each persona's LLM calls count against it.
pub fn ensure_days(generate: Generate, world: &World, personas: &[Persona], src: &Sources, date: NaiveDate) -> BTreeMap<String, Result<Planned>> {
    let shared = Shared::new(world);
    let mut out = BTreeMap::new();
    for chunk in personas.chunks(PARALLEL) {
        std::thread::scope(|s| {
            let runs: Vec<_> = chunk.iter().map(|p| (p.user.clone(), s.spawn(|| crate::calls::for_persona(&p.user, || ensure_day(generate, world, p, src, date, &shared))))).collect();
            for (u, h) in runs {
                out.insert(u, h.join().unwrap_or_else(|_| Err(anyhow::anyhow!("planner thread panicked"))));
            }
        });
    }
    out
}

/// One process-wide entry for the CLI and the workers: a fresh client per persona.
pub fn generator<'a>(key: &'a str, model: Option<&'a str>, world: &'a World, src: &'a Sources<'a>) -> impl Fn(&Persona, NaiveDate, &Shared) -> Result<Generated> + Sync + 'a {
    move |p, d, s| generate_day(&LlmClient::new(key, model).context("planner LLM")?, world, p, src, d, s)
}

#[cfg(test)]
mod tests;

pub mod worker;
