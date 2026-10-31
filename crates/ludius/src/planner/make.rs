//! Writing a day plan (planner.plan_day): seeded slots decide what happens when and to whom, the planner LLM
//! writes the content, `validate` rejects what doesn't hold, one fix round, the rest is dropped and reported.
use std::collections::{BTreeMap, BTreeSet};

use anyhow::{Result, bail};
use chrono::NaiveDate;
use indexmap::IndexMap;
use rand::distr::weighted::WeightedIndex;
use rand::rngs::StdRng;
use rand::seq::{IndexedRandom, SliceRandom};
use rand::{RngExt as _, SeedableRng};
use serde::Deserialize;
use sha2::Digest;

use super::check::{owned_problem, uses_own_file};
use super::validate::{Context, validate};
use super::{BREAK, DayPlan, Narrative, PlanItem, REACTION_ONLY, Scenario, chat_channels, leaf, minutes};
use crate::calls::CapReached;
use crate::decisions::DecisionsClient;
use crate::llm::{Check, LlmClient};
use crate::policy::Policy;
use crate::tasks::{ATTACHABLE, Persona, Template, sends};
use crate::template::casefold;

pub(crate) const NARRATIVE_SYSTEM: &str = "You write the backstory of a fictional, ordinary small company for a cyber-defence training range. \
It must read as mundane and plausible: real-sounding departments, projects and calendar events. \
No security incidents, no hacking, no real companies or real people.";
const PLAN_SYSTEM: &str = "You write the content of one ordinary working day for one employee of a fictional company in a training range. \
The day's tasks, times and recipients are fixed; you give each one a concrete reason that follows from the \
company story, the persona's role and what really happened before, and write all content (file names, note \
text, search queries, messages) in the persona's own style. Use plain ASCII only. Never mention the range, \
training, Ludius, security tools or anything outside the fiction.";
pub(crate) const GOSSIP: &str = "harmless office gossip about colleagues or the company (who is back from holiday, the new coffee machine, a \
project running late, a rumour about the office move), never about anyone's misconduct, health, pay, relationships or job";
const DM_PURPOSES: [&str; 2] = [
    "one line of small talk",
    "ask the colleague for something concrete the persona needs (a note, a file, a mail with a figure), saying what",
];
const WORK_ITEMS: (usize, usize) = (5, 8);
const GOSSIP_DAYS: f64 = 0.7;
const INTERRUPT: f64 = 0.4;
const SLOT_GAP: i64 = 15;
const SHOWN_EXISTING: usize = 80;
/// Capabilities that can settle a follow-up ("" is a template without one).
const SETTLES: [&str; 5] = ["", "file.write", "mail.send", "chat.dm", "calendar.invite"];
const TO_SOMEONE: [&str; 5] = ["mail.send", "chat.dm", "chat.file", "chat.read", "calendar.invite"];
const NEEDS_INBOX: [&str; 4] = ["mail.read", "mail.file", "mail.save", "mail.reply"];
const DERIVED: [&str; 1] = ["to_name"];

/// Everything a day plan is written from.
pub struct Day<'a> {
    pub narrative: &'a Narrative,
    pub persona: &'a Persona,
    pub date: NaiveDate,
    /// What happened so far (World::summary).
    pub world: String,
    pub history: Vec<String>,
    pub policy: Policy,
    pub scenario: Option<Scenario>,
    /// Open follow-up id -> what it is about, in the order they came in.
    pub followups: IndexMap<String, String>,
    pub existing: Vec<String>,
    pub mail_threads: BTreeMap<String, (String, String)>,
    pub inbox: BTreeSet<(String, String)>,
    pub owned: BTreeMap<String, String>,
    pub invites: BTreeSet<(String, String)>,
    pub unread: BTreeSet<(String, String)>,
    /// Times already taken today (HH:MM).
    pub taken: Vec<String>,
    /// Inbox mail -> its attachment names.
    pub attached: BTreeMap<(String, String), Vec<String>>,
}

impl Day<'_> {
    fn day(&self) -> String {
        self.date.format("%Y-%m-%d").to_string()
    }

    fn window(&self) -> Option<&Scenario> {
        self.scenario.as_ref().filter(|s| s.date == self.day())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Draft {
    items: Vec<PlanItem>,
}

impl Check for Draft {
    fn check(&self) -> Result<()> {
        self.items.iter().try_for_each(|it| it.clone().normalize())
    }
}

impl Check for Narrative {
    fn check(&self) -> Result<()> {
        Narrative::check(self)
    }
}

pub(crate) fn seeded(seed: &str) -> StdRng {
    StdRng::from_seed(sha2::Sha256::digest(seed.as_bytes()).into())
}

fn hhmm(m: i64) -> String {
    format!("{:02}:{:02}", m.div_euclid(60).rem_euclid(24), m.rem_euclid(60))
}

/// The task catalog the planner LLM picks content for (shuffled by `seed` when given).
pub fn catalog(templates: &BTreeMap<String, Template>, seed: &str) -> String {
    let mut lines: Vec<String> = templates
        .values()
        .map(|t| {
            let params: Vec<&str> = t.params.iter().map(String::as_str).filter(|p| !DERIVED.contains(p)).collect();
            let params = if params.is_empty() { "none".to_string() } else { params.join(", ") };
            let rare = if t.plan_weight < 1.0 { format!(" Plan it rarely, about {} times as often as other tasks.", t.plan_weight) } else { String::new() };
            format!("- {}: {} Parameters: {params}.{rare}", t.task, t.about)
        })
        .collect();
    if !seed.is_empty() {
        lines.shuffle(&mut seeded(seed));
    }
    lines.push(format!("- {BREAK}: away from the desk (lunch, coffee, meeting), screen locked. Set minutes."));
    lines.join("\n")
}

pub fn make_narrative(llm: &LlmClient, personas: &[Persona], start: NaiveDate) -> Result<Narrative> {
    let who: Vec<String> = personas.iter().map(|p| format!("- {} (account {}): {}, {}", p.name, p.user, p.role, p.department)).collect();
    let user = format!(
        "Range start date: {}.\nThese employees are personas and must appear in people with their account as user:\n{}\n\n\
         Return JSON with keys company, city, summary (3-5 sentences), people (8-15 entries: name, role, department, user (a short \
         lower-case account for everyone), manager (the user of the person they report to; empty only for the head of the company; \
         every employee listed above reports to someone who is not one of them)), projects (3-6 short strings) and events (6-12 \
         entries: date YYYY-MM-DD within four weeks of the start, what). Include the routine (month-end close, team meetings) and a \
         few one-offs (an office move, a supplier change, a system migration).",
        start.format("%Y-%m-%d"),
        who.join("\n")
    );
    let mut n: Narrative = llm.json(NARRATIVE_SYSTEM, &user, 0.9, 2)?;
    n.accounts = personas.iter().map(|p| p.user.clone()).collect();
    Ok(n)
}

pub fn installed(app: &str) -> bool {
    std::env::var("LUDIUS_APPS").map_or(true, |apps| apps.split(',').any(|a| a.trim() == app))
}

/// The templates this persona may be planned: lookalikes only from their risk profile, installed apps,
/// fallbacks only when the main app is missing, and every `$VAR` default set.
pub fn role_templates(templates: &BTreeMap<String, Template>, persona: &Persona) -> BTreeMap<String, Template> {
    templates
        .iter()
        .filter(|(k, t)| {
            (t.resembles.is_empty() || persona.risk_profile.contains(k))
                && !REACTION_ONLY.contains(&t.cap())
                && installed(&t.app)
                && !(!t.fallback_for.is_empty() && installed(&t.fallback_for))
                && t.defaults.values().filter_map(|v| v.strip_prefix('$')).all(|v| std::env::var(v).is_ok_and(|x| !x.is_empty()))
        })
        .map(|(k, t)| (k.clone(), t.clone()))
        .collect()
}

pub fn fill_recipient_names(items: &mut [PlanItem], people: &BTreeMap<String, String>) {
    for it in items {
        if let Some(to) = it.params.get("to") {
            let user = to.trim().to_lowercase().split('@').next().unwrap_or("").to_string();
            it.content.shift_remove("to_name");
            it.params.insert("to_name".into(), people.get(&user).cloned().unwrap_or_default());
        }
    }
}

/// LUDIUS_SOCIAL: how much chat and gossip a day has (1 = normal).
fn social() -> Result<f64> {
    let raw = std::env::var("LUDIUS_SOCIAL").unwrap_or_else(|_| "1".into());
    match raw.trim().parse::<f64>() {
        Ok(v) if v.is_finite() && v > 0.0 => Ok(v),
        _ => bail!("LUDIUS_SOCIAL must be a positive number, got {raw:?}"),
    }
}

/// Per-template plausibility weights and the task that settles each follow-up, from the Decider.
fn fit(decider: Option<&DecisionsClient>, d: &Day, eligible: &BTreeMap<String, Template>, followups: &IndexMap<String, String>, open: &[String]) -> Result<(BTreeMap<String, f64>, BTreeMap<String, String>)> {
    let Some(decider) = decider else { return Ok((eligible.keys().map(|k| (k.clone(), 1.0)).collect(), BTreeMap::new())) };
    let p = d.persona;
    let who = format!("{}, {} ({}). Style: {}", p.name, p.role, p.department, if p.style.is_empty() { "ordinary" } else { &p.style });
    let weekday = d.date.format("%A");
    let mut questions = serde_json::Map::new();
    for (k, t) in eligible {
        questions.insert(
            format!("t:{k}"),
            serde_json::json!({
                "type": "noul",
                "instructions": format!("Would {} plausibly do this kind of task on {weekday} as part of their work, given their role and what is going on? Task: {}", p.name, t.about),
                "criteria": {"true": "A plausible task for this person today.", "false": "Out of character or role."},
            }),
        );
    }
    if !open.is_empty() {
        let criteria: serde_json::Map<String, serde_json::Value> = open.iter().map(|k| (k.clone(), eligible[k].about.clone().into())).collect();
        for (f, about) in followups {
            questions.insert(format!("f:{f}"), serde_json::json!({"type": "choice", "instructions": format!("Which task settles this follow-up best? {about}"), "criteria": criteria}));
        }
    }
    let tail: String = d.world.chars().rev().take(6000).collect::<Vec<_>>().into_iter().rev().collect();
    let answers = decider.ask(&serde_json::json!({"persona": who, "what happened so far": tail}), &serde_json::Value::Object(questions))?;
    let weights = eligible.keys().map(|k| Ok((k.clone(), answers.noul(&format!("t:{k}"))?.max(0.02)))).collect::<Result<_>>()?;
    let settle = if open.is_empty() {
        BTreeMap::new()
    } else {
        followups.keys().filter_map(|f| answers.answers[format!("f:{f}")]["choice"].as_str().map(|c| (f.clone(), c.to_string()))).collect()
    };
    Ok((weights, settle))
}

/// The seeded slot planner's state (planner.day_slots' closures).
struct Slots<'a> {
    d: &'a Day<'a>,
    templates: &'a BTreeMap<String, Template>,
    rng: StdRng,
    colleagues: Vec<String>,
    people: BTreeMap<String, String>,
    channels: Vec<String>,
    shares: Vec<String>,
    mails: Vec<(String, String)>,
    saves: Vec<((String, String), String)>,
    fresh: Vec<(String, String)>,
    downloads: bool,
}

impl Slots<'_> {
    fn to(&mut self, mut it: PlanItem, user: Option<&str>) -> PlanItem {
        let Some(user) = user.map(String::from).or_else(|| self.colleagues.choose(&mut self.rng).cloned()) else { return it };
        it.params.insert("to".into(), format!("{user}@{}", crate::mail::domain()));
        it.params.insert("to_name".into(), self.people.get(&user).cloned().unwrap_or_else(|| user.clone()));
        if sends(self.templates[&it.template].cap()).is_some() {
            it.handoff = user;
        }
        it
    }

    fn of(&self, cap: &str) -> Vec<String> {
        self.templates.iter().filter(|(_, t)| t.cap() == cap && t.resembles.is_empty()).map(|(k, _)| k.clone()).collect()
    }

    /// The persona's own files this template can act on; None when nothing tells (no files, or no file parameter).
    fn own(&self, k: &str) -> Option<Vec<IndexMap<String, String>>> {
        let (t, owned, day) = (&self.templates[k], &self.d.owned, self.d.day());
        let (mut fits, mut known) = (Vec::new(), owned.is_empty());
        for path in owned.keys() {
            let name = path.rsplit('\\').next().unwrap_or(path);
            let (stem, ext) = name.rsplit_once('.').unwrap_or(("", name));
            for c in [vec![("file_name", name)], vec![("file_name", stem)], vec![("file_name", stem), ("file_ext", ext)]] {
                let params: IndexMap<String, String> = c.into_iter().map(|(a, b)| (a.to_string(), b.to_string())).collect();
                let it = PlanItem { at: "00:00".into(), template: k.into(), params: params.clone(), ..Default::default() };
                let why = owned_problem(&it, t, owned, &day, &[]);
                known |= why.as_deref() != Some("missing parameters");
                if why.is_none() {
                    fits.push(params);
                    break;
                }
            }
        }
        known.then_some(fits)
    }

    fn slot(&mut self, k: &str, why: &str) -> PlanItem {
        let mut it = PlanItem { at: "00:00".into(), template: k.into(), context: why.into(), ..Default::default() };
        let t = &self.templates[k];
        let cap = t.cap().to_string();
        if uses_own_file(t)
            && let Some(fits) = self.own(k)
            && let Some(c) = fits.choose(&mut self.rng)
        {
            it.params.extend(c.clone());
        }
        let domain = crate::mail::domain();
        match cap.as_str() {
            c if TO_SOMEONE.contains(&c) => it = self.to(it, None),
            "mail.reply" => {
                if let Some((u, subject)) = self.mails.choose(&mut self.rng).cloned() {
                    it = self.to(it, Some(&u));
                    it.params.insert("subject".into(), subject);
                }
            }
            "mail.read" => {
                if let Some((u, subject)) = self.fresh.pop() {
                    it.params.extend([("sender".into(), format!("{u}@{domain}")), ("subject".into(), subject)]);
                }
            }
            "mail.save" => {
                if let Some(((u, subject), name)) = self.saves.choose(&mut self.rng).cloned() {
                    it.params.extend([("sender".into(), format!("{u}@{domain}")), ("subject".into(), subject), ("file_name".into(), name)]);
                }
            }
            "mail.file" => {
                if let Some((u, subject)) = self.mails.choose(&mut self.rng).cloned() {
                    it.params.extend([("sender".into(), format!("{u}@{domain}")), ("subject".into(), subject)]);
                }
            }
            "chat.post" => {
                if let Some(c) = self.channels.choose(&mut self.rng) {
                    it.params.insert("channel".into(), c.clone());
                }
            }
            "file.share" => {
                if let Some(s) = self.shares.choose(&mut self.rng) {
                    it.params.insert("share".into(), s.clone());
                }
            }
            _ => {}
        }
        it
    }

    fn eligible(&self, k: &str, t: &Template) -> bool {
        let cap = t.cap();
        t.resembles.is_empty()
            && cap != "calendar.respond"
            && (!TO_SOMEONE.contains(&cap) || !self.colleagues.is_empty())
            && (!NEEDS_INBOX.contains(&cap) || !self.mails.is_empty())
            && (cap != "mail.read" || !self.fresh.is_empty())
            && (cap != "mail.save" || !self.saves.is_empty())
            && (cap != "chat.post" || !self.channels.is_empty())
            && (cap != "file.share" || !self.shares.is_empty())
            && (cap != "file.download" || self.downloads)
            && (!uses_own_file(t) || self.own(k).is_none_or(|f| !f.is_empty()))
    }
}

fn shuffled<T: Clone>(v: &[T], rng: &mut StdRng) -> Vec<T> {
    let mut v = v.to_vec();
    v.shuffle(rng);
    v
}

/// What happens when, to whom, about which mail or file: everything but the written content (planner.day_slots).
pub(crate) fn day_slots(d: &Day, templates: &BTreeMap<String, Template>, channels: Vec<String>, weights: &BTreeMap<String, f64>, settle: &BTreeMap<String, String>, lookalikes: &[String]) -> Result<Vec<PlanItem>> {
    let mut rng = seeded(&format!("{}:{}", d.persona.user, d.day()));
    let mails: Vec<(String, String)> = d.inbox.iter().cloned().collect();
    let saved: BTreeSet<String> = d.owned.keys().filter_map(|p| p.rsplit_once('\\').filter(|(dir, _)| dir.ends_with("documents")).map(|(_, n)| casefold(n))).collect();
    let mut saves = Vec::new();
    for k in &mails {
        let names = d.attached.get(k).map(Vec::as_slice).unwrap_or(&[]);
        for n in names {
            let once = names.iter().filter(|x| casefold(x) == casefold(n)).count() == 1;
            if once && ATTACHABLE.iter().any(|x| n.to_lowercase().ends_with(x)) && !saved.contains(&casefold(n)) {
                saves.push((k.clone(), n.clone()));
            }
        }
    }
    let fresh = shuffled(&d.unread.intersection(&d.inbox).cloned().collect::<Vec<_>>(), &mut rng);
    let people = d.narrative.people_names();
    let mut s = Slots {
        d,
        templates,
        rng,
        colleagues: d.narrative.colleagues(&d.persona.user).into_iter().collect(),
        people,
        channels,
        shares: d.narrative.team_shares(&d.persona.user),
        mails,
        saves,
        fresh,
        downloads: !super::downloads(&d.policy).is_empty(),
    };
    let social = social()?;
    let mut first = Vec::new();
    if !s.fresh.is_empty()
        && let Some(read) = s.of("mail.read").first()
    {
        first.push(s.slot(read, "the morning look at new mail"));
    }
    let open: Vec<String> = templates.iter().filter(|(k, t)| SETTLES.contains(&t.cap()) && s.eligible(k, t)).map(|(k, _)| k.clone()).collect();
    for (f, about) in &d.followups {
        let mut it = if let Some((u, subject)) = d.mail_threads.get(f) {
            let Some(reply) = s.of("mail.reply").first().cloned() else { continue };
            if !d.inbox.contains(&(u.clone(), subject.clone())) {
                continue;
            }
            let mut it = s.to(PlanItem { at: "00:00".into(), template: reply, context: about.clone(), ..Default::default() }, Some(u));
            it.params.insert("subject".into(), subject.clone());
            it
        } else if !open.is_empty() {
            let k = settle.get(f).filter(|k| open.contains(k)).cloned().unwrap_or_else(|| open.choose(&mut s.rng).unwrap().clone());
            s.slot(&k, about)
        } else {
            continue;
        };
        it.followup = f.clone();
        first.push(it);
    }
    for (u, subject) in d.invites.iter().take(2) {
        if let Some(answer) = s.of("calendar.respond").first().cloned() {
            let mut it = s.to(PlanItem { at: "00:00".into(), template: answer, context: format!("answers the invitation {subject:?}"), ..Default::default() }, Some(u));
            it.params.insert("subject".into(), subject.clone());
            first.push(it);
        }
    }
    if !s.colleagues.is_empty()
        && let Some(dm) = s.of("chat.dm").first().cloned()
        && s.rng.random::<f64>() < GOSSIP_DAYS * social
    {
        first.push(s.slot(&dm, &format!("gossip with a colleague the persona is friendly with: {GOSSIP}")));
    }

    let (h0, h1) = (minutes(&d.persona.hours.0) + 10, minutes(&d.persona.hours.1) - 30);
    let lunch = 11 * 60 + 45 + 5 * s.rng.random_range(0..10);
    let lunch_end = lunch + [30, 40, 45].choose(&mut s.rng).unwrap();
    let mut busy: Vec<(i64, i64)> = vec![(lunch - SLOT_GAP, lunch_end + SLOT_GAP)];
    busy.extend(d.taken.iter().map(|t| (minutes(t) - SLOT_GAP, minutes(t) + SLOT_GAP)));
    let free_of = |busy: &[(i64, i64)], m: i64| !busy.iter().any(|(x, y)| *x <= m && m <= *y);
    let free: Vec<i64> = (h0 - h0 % 5..h1).step_by(5).filter(|m| free_of(&busy, *m)).collect();
    let (mut late, mut pinned) = (Vec::new(), Vec::<i64>::new());
    if let Some(w) = d.window()
        && !lookalikes.is_empty()
    {
        let (a, b) = (minutes(&w.start), minutes(&w.end));
        let picks = shuffled(lookalikes, &mut s.rng);
        for (k, aim) in picks.iter().take(2).zip([a - 60, b + 20]) {
            let near = free.iter().filter(|m| pinned.iter().all(|t| (**m - t).abs() >= SLOT_GAP) && !(a - 15 <= **m && **m <= b + 15)).min_by_key(|m| (**m - aim).abs());
            if let Some(m) = near {
                pinned.push(*m);
                late.push(PlanItem { at: hhmm(*m), template: k.clone(), context: "part of the normal job, due today".into(), ..Default::default() });
            }
        }
        busy.push((a - 90, b + 90));
    }
    let grid: Vec<i64> = free.iter().copied().filter(|m| free_of(&busy, *m)).collect();

    let want = s.rng.random_range(WORK_ITEMS.0..=WORK_ITEMS.1);
    first.truncate(want.saturating_sub(late.len()));
    let mut pool: Vec<(String, f64)> = templates
        .iter()
        .filter(|(k, t)| s.eligible(k, t))
        .map(|(k, t)| (k.clone(), weights.get(k).copied().unwrap_or(1.0) * t.plan_weight * if t.cap().starts_with("chat.") { social } else { 1.0 }))
        .collect();
    let mut rest = Vec::new();
    while !pool.is_empty() && first.len() + rest.len() + late.len() < want {
        let Ok(w) = WeightedIndex::new(pool.iter().map(|(_, w)| *w)) else { break };
        let (k, _) = pool.remove(s.rng.sample(&w));
        let why = match templates[&k].cap() {
            "chat.dm" => DM_PURPOSES.choose(&mut s.rng).unwrap().to_string(),
            "chat.post" => "small talk".into(),
            _ => String::new(),
        };
        rest.push(s.slot(&k, &why));
    }

    let mut times: Vec<i64> = Vec::new();
    for m in shuffled(&grid, &mut s.rng) {
        if times.len() == first.len() + rest.len() {
            break;
        }
        if times.iter().chain(&pinned).all(|t| (m - t).abs() >= SLOT_GAP) {
            times.push(m);
        }
    }
    times.sort();
    let mut items: Vec<PlanItem> = first.into_iter().chain(shuffled(&rest, &mut s.rng)).collect();
    items.truncate(times.len());
    for (it, m) in items.iter_mut().zip(&times) {
        it.at = hhmm(*m);
    }
    items.extend(late);
    for it in items.iter_mut().skip(1) {
        if it.template == "edge_web_lookup" && s.rng.random::<f64>() < INTERRUPT {
            it.interrupt = true;
            break;
        }
    }
    items.push(PlanItem { at: hhmm(lunch), template: BREAK.into(), minutes: (lunch_end - lunch) as u32, context: "Lunch.".into(), ..Default::default() });
    items.sort_by(|a, b| a.at.cmp(&b.at));
    Ok(items)
}

/// Write one day's plan; returns the plan and why any planned item or follow-up was dropped.
pub fn plan_day(llm: &LlmClient, d: &Day, templates: &BTreeMap<String, Template>, decider: Option<&DecisionsClient>) -> Result<(DayPlan, Vec<String>)> {
    let (p, n) = (d.persona, d.narrative);
    let templates = role_templates(templates, p);
    let window = d.window();
    let lookalikes: Vec<String> = templates.iter().filter(|(_, t)| window.is_some_and(|w| t.resembles == w.signal)).map(|(k, _)| k.clone()).collect();
    let teams = n.teams(&p.user);
    let mut names: Vec<String> = d.existing.iter().map(|e| leaf(e)).filter(|l| !l.is_empty()).collect::<BTreeSet<_>>().into_iter().collect();
    names.sort_by_key(|n| casefold(n));
    names.truncate(SHOWN_EXISTING);
    let open: Vec<String> = templates.iter().filter(|(_, t)| t.resembles.is_empty() && SETTLES.contains(&t.cap())).map(|(k, _)| k.clone()).collect();
    let choose: IndexMap<String, String> = d.followups.iter().filter(|(f, _)| !d.mail_threads.contains_key(*f)).map(|(f, a)| (f.clone(), a.clone())).collect();
    let (weights, settle) = match fit(decider, d, &templates, &choose, &open) {
        Ok(x) => x,
        Err(e) if e.is::<CapReached>() => return Err(e),
        Err(_) => Default::default(), // no answer: even weights, follow-ups by a fixed pick
    };
    let channels: Vec<String> = chat_channels()?.into_iter().chain(teams.iter().cloned()).collect();
    let slots = day_slots(d, &templates, channels, &weights, &settle, &lookalikes)?;
    let work: Vec<&PlanItem> = slots.iter().filter(|it| it.template != BREAK).collect();

    let mut user = format!(
        "Company story:\n{}\n\nPersona: {}, {} ({}). Usual hours {}-{}.\nStyle and habits: {}\n\nDay to plan: {} {}.\n\nWhat happened so far:\n{}\n\nTask catalog:\n{}\n\n",
        serde_json::to_string_pretty(n)?,
        p.name,
        p.role,
        p.department,
        p.hours.0,
        p.hours.1,
        if p.style.is_empty() { "ordinary" } else { &p.style },
        d.date.format("%A"),
        d.day(),
        if d.world.is_empty() { "- nothing yet" } else { &d.world },
        catalog(&work.iter().map(|it| (it.template.clone(), templates[&it.template].clone())).collect(), ""),
    );
    if templates.contains_key("edge_web_lookup") {
        user += &format!("Intranet pages (a lookup's search_query should find one of them):\n{}\n\n", super::intranet_index(&d.policy));
    }
    if templates.values().any(|t| t.cap() == "file.download") {
        let list: Vec<String> = super::downloads(&d.policy).into_iter().map(|(f, page)| format!("- {page}: {f}")).collect();
        user += &format!("Intranet downloads (edge_download_file: page is the exact page title, file_name the exact file name):\n{}\n\n", list.join("\n"));
    }
    let recent: Vec<String> = d.history.iter().rev().take(40).rev().map(|h| format!("- {}", h.chars().take(120).collect::<String>())).collect();
    user += &format!("Recent content the persona already wrote (do not repeat or paraphrase it):\n{}", if recent.is_empty() { "- none".into() } else { recent.join("\n") });
    if !names.is_empty() {
        user += "\n\nThese files and folders already exist for the persona. Never create, save or rename anything to one of these names, with or without an extension; only explorer_rename_move may name one of them as the existing file_name or folder_name it acts on:\n";
        user += &names.iter().map(|n| format!("- {n}")).collect::<Vec<_>>().join("\n");
    }
    user += "\n\nReturn JSON {\"items\": [...]} with exactly these items, one each, same at and template: params (object with every listed parameter as a string; keep every given value exactly) and context (one sentence: why the persona does this now, following the given purpose).\n";
    let lines: Vec<String> = work
        .iter()
        .map(|it| {
            let ctx = if it.context.is_empty() { String::new() } else { format!(": {}", it.context) };
            let given = if it.params.is_empty() { String::new() } else { format!("; given {}", serde_json::to_string(&it.params).unwrap_or_default()) };
            format!("- {} {}{ctx}{given}", it.at, it.template)
        })
        .collect();
    user += &lines.join("\n");
    let colleagues = n.colleagues(&p.user);
    if !colleagues.is_empty() {
        user += &format!(
            "\nMail: reply only with the given subject; a read or filed mail is one listed under Inbox on the mail server (sender is its sender's address, subject exactly as listed). Recipients are <account>@{}.",
            crate::mail::domain()
        );
    }

    let people = n.people_names();
    let merge = |draft: Draft| -> Result<Vec<PlanItem>> {
        let mut got: BTreeMap<(String, String), PlanItem> = BTreeMap::new();
        for mut x in draft.items {
            x.normalize()?;
            got.insert((x.at.clone(), x.template.clone()), x);
        }
        let mut out = Vec::new();
        for it in &work {
            if let Some(x) = got.remove(&(it.at.clone(), it.template.clone())) {
                let mut m = (*it).clone();
                m.content = x.content.into_iter().filter(|(k, _)| !it.params.contains_key(k)).collect();
                m.params = x.params;
                m.params.extend(it.params.clone());
                if !x.context.is_empty() {
                    m.context = x.context;
                }
                out.push(m);
            }
        }
        fill_recipient_names(&mut out, &people);
        Ok(out)
    };
    let ctx = Context {
        templates: templates.clone(),
        history: d.history.clone(),
        policy: d.policy.clone(),
        colleagues,
        scenario: window,
        followups: d.followups.keys().cloned().collect(),
        existing: d.existing.clone(),
        mail_threads: d.mail_threads.clone(),
        people: people.clone(),
        user: p.user.clone(),
        day: d.day(),
        inbox: d.inbox.clone(),
        teams,
        owned: d.owned.clone(),
        shares: n.team_shares(&p.user),
        invites: d.invites.clone(),
    };

    let mut items = merge(llm.json(PLAN_SYSTEM, &user, 0.9, 2)?)?;
    let mut rejects = validate(&items, &ctx)?;
    let have: BTreeSet<(&str, &str)> = items.iter().map(|it| (it.at.as_str(), it.template.as_str())).collect();
    let unwritten: Vec<String> = work.iter().filter(|it| !have.contains(&(it.at.as_str(), it.template.as_str()))).map(|it| format!("- {} {} is missing", it.at, it.template)).collect();
    if !rejects.is_empty() || !unwritten.is_empty() {
        let fix: Vec<String> = rejects.iter().map(|(i, why)| format!("- item {i} ({} at {}): {why}", items[*i].template, items[*i].at)).chain(unwritten).collect();
        items = merge(llm.json(PLAN_SYSTEM, &format!("{user}\n\nThese items were rejected; return the full corrected item list:\n{}", fix.join("\n")), 0.9, 2)?)?;
        rejects = validate(&items, &ctx)?;
    }
    let bad: BTreeSet<usize> = rejects.iter().map(|(i, _)| *i).collect();
    let mut kept: Vec<PlanItem> = items.iter().enumerate().filter(|(i, _)| !bad.contains(i)).map(|(_, it)| it.clone()).collect();
    let scheduled: BTreeSet<&str> = kept.iter().map(|it| it.followup.as_str()).collect();
    let mut missing: Vec<&String> = d.followups.keys().filter(|f| !scheduled.contains(f.as_str())).collect();
    missing.sort();
    let mut notes: Vec<String> = rejects.iter().map(|(i, why)| format!("{} at {}: {why}", items[*i].template, items[*i].at)).collect();
    notes.extend(missing.iter().map(|f| format!("follow-up {f}: not scheduled, stays open")));
    kept.extend(slots.iter().filter(|it| it.template == BREAK).cloned());
    kept.sort_by(|a, b| a.at.cmp(&b.at));
    Ok((DayPlan { persona: p.user.clone(), date: d.day(), items: kept }, notes))
}
