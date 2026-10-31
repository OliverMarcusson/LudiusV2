//! Day plans (port of planner.py): the plan and story types, plan-load validation, the world's history and
//! day logs. Writing plans (slots, LLM content) is in `make`, their checks in `validate`.
use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use anyhow::{Result, bail};
use indexmap::IndexMap;
use regex::Regex;
use serde::{Deserialize, Deserializer, Serialize};

use crate::tasks::{Template, TaskInstance, load_template};

mod check;
mod load;
pub(crate) mod make;
pub(crate) mod validate;
mod world;

pub use check::{asked_files, content_values, item_problem, owned_problem, taken_names, targets};
pub use load::load_plan;
pub use make::{Day, catalog, fill_recipient_names, installed, make_narrative, plan_day, role_templates};
pub use validate::{Context, near_duplicate, validate};
pub use world::{World, day_records, done_slots, dump, owned_files, started_slots};
pub(crate) use world::{sorted_glob, stem_of};

pub const BREAK: &str = "break";
pub const VALUE_MAX: usize = 300;
pub const RECYCLE_AGE_DAYS: i64 = 3;
pub const OWN_FILES: [&str; 7] = ["file.edit", "file.reuse", "file.export", "file.recycle", "file.share", "file.organize", "file.move"];
pub const MULTILINE: [&str; 5] = ["body", "notes", "rows", "bullets", "bullets2"];
pub const WORK_DAY: (&str, &str) = ("08:00", "17:00");
pub const MEETING_MINUTES: (i64, i64) = (15, 120);
pub const MEETING_DAYS_AHEAD: i64 = 14;
pub const REACTION_ONLY: [&str; 1] = ["chat.read_channel"];
pub(crate) fn re(pattern: &str) -> &'static Regex {
    static CACHE: OnceLock<std::sync::Mutex<BTreeMap<String, &'static Regex>>> = OnceLock::new();
    let mut m = CACHE.get_or_init(Default::default).lock().unwrap();
    m.entry(pattern.to_string()).or_insert_with(|| Box::leak(Box::new(Regex::new(pattern).expect("static pattern"))))
}

pub(crate) fn fullmatch(pattern: &str, s: &str) -> bool {
    re(&format!("^(?:{pattern})$")).is_match(s)
}

/// Every chat channel the chat server has (data/chat.yaml); team channels come from the narrative.
pub fn chat_channels() -> Result<Vec<String>> {
    #[derive(Deserialize)]
    struct Chat {
        channels: Vec<String>,
    }
    Ok(crate::tasks::load_yaml::<Chat>(&crate::paths::data().join("chat.yaml"))?.channels)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Person {
    pub name: String,
    pub role: String,
    pub department: String,
    #[serde(default)]
    pub user: String,
    #[serde(default)]
    pub manager: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub date: String,
    pub what: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Narrative {
    pub company: String,
    pub city: String,
    pub summary: String,
    pub people: Vec<Person>,
    #[serde(default)]
    pub channels: IndexMap<String, Vec<String>>,
    #[serde(default)]
    pub shares: IndexMap<String, Vec<String>>,
    pub projects: Vec<String>,
    pub events: Vec<Event>,
    #[serde(default)]
    pub revised: String,
    #[serde(default)]
    pub accounts: Vec<String>,
    #[serde(default)]
    pub history: Vec<Event>,
}

impl Narrative {
    /// Narrative._members: team channels and shares name plain, unique handles and only narrative accounts.
    pub fn check(&self) -> Result<()> {
        let users: BTreeSet<&str> = self.people.iter().filter(|p| !p.user.is_empty()).map(|p| p.user.as_str()).collect();
        let handle = |c: &str| c.to_lowercase().replace(' ', "-");
        let handles: Vec<String> = chat_channels()?.iter().chain(self.channels.keys()).map(|c| handle(c)).collect();
        for (name, members) in &self.channels {
            if !fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,40}", name) || !members.iter().all(|m| users.contains(m.as_str())) {
                bail!("team channel {name:?}: a plain name, every member a narrative person's account");
            }
            if handles.iter().filter(|h| **h == handle(name)).count() > 1 {
                bail!("team channel {name:?}: its handle is another channel's");
            }
        }
        for (name, members) in &self.shares {
            if !fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}", name) || ["NETLOGON", "SYSVOL"].contains(&name.to_uppercase().as_str()) {
                bail!("team share {name:?}: a plain name without spaces, not a system share");
            }
            if members.is_empty() || !members.iter().all(|m| users.contains(m.as_str())) {
                bail!("team share {name:?}: every member a narrative person's account");
            }
        }
        Ok(())
    }

    pub fn colleagues(&self, user: &str) -> BTreeSet<String> {
        self.people.iter().filter(|p| !p.user.is_empty() && p.user != user && self.accounts.contains(&p.user)).map(|p| p.user.clone()).collect()
    }

    /// The user's manager's account, if any: a requested file is mailed back to them.
    pub fn boss(&self, user: &str) -> BTreeSet<String> {
        let me = self.people.iter().find(|p| p.user == user);
        let known = |u: &str| self.people.iter().any(|p| !p.user.is_empty() && p.user == u);
        me.filter(|m| known(&m.manager) && m.manager != user).map(|m| m.manager.clone()).into_iter().collect()
    }

    pub fn people_names(&self) -> BTreeMap<String, String> {
        self.people.iter().filter(|p| !p.user.is_empty()).map(|p| (p.user.clone(), p.name.clone())).collect()
    }

    pub fn teams(&self, user: &str) -> Vec<String> {
        self.channels.iter().filter(|(_, m)| m.iter().any(|x| x == user)).map(|(c, _)| c.clone()).collect()
    }

    pub fn team_shares(&self, user: &str) -> Vec<String> {
        self.shares.iter().filter(|(_, m)| m.iter().any(|x| x == user)).map(|(c, _)| c.clone()).collect()
    }

    pub fn load(path: &std::path::Path) -> Result<Self> {
        let n: Self = crate::tasks::load_yaml(path)?;
        n.check()?;
        Ok(n)
    }
}

fn null_empty<'de, D: Deserializer<'de>>(d: D) -> Result<String, D::Error> {
    Ok(Option::<String>::deserialize(d)?.unwrap_or_default())
}

fn is_default<T: Default + PartialEq>(v: &T) -> bool {
    *v == T::default()
}

/// One planned item: a task instance at a time of day (or a break).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanItem {
    pub template: String,
    #[serde(default, skip_serializing_if = "is_default")]
    pub params: IndexMap<String, String>,
    #[serde(default, skip_serializing_if = "is_default")]
    pub content: IndexMap<String, String>,
    #[serde(default, skip_serializing_if = "is_default")]
    pub variant: Option<usize>,
    #[serde(default, skip_serializing_if = "is_default")]
    pub context: String,
    #[serde(default, skip_serializing_if = "is_default")]
    pub request: String,
    pub at: String,
    #[serde(default, skip_serializing_if = "is_default")]
    pub minutes: u32,
    #[serde(default, skip_serializing_if = "is_default")]
    pub interrupt: bool,
    #[serde(default, deserialize_with = "null_empty", skip_serializing_if = "is_default")]
    pub handoff: String,
    #[serde(default, deserialize_with = "null_empty", skip_serializing_if = "is_default")]
    pub followup: String,
}

impl PlanItem {
    pub fn instance(&self) -> TaskInstance {
        TaskInstance {
            template: self.template.clone(),
            params: self.params.clone(),
            content: self.content.clone(),
            variant: self.variant,
            context: self.context.clone(),
            request: self.request.clone(),
        }
    }

    /// `{**params, **content}`.
    pub fn values(&self) -> IndexMap<String, String> {
        let mut v = self.params.clone();
        v.extend(self.content.clone());
        v
    }

    pub fn value(&self, k: &str) -> Option<&String> {
        self.content.get(k).or_else(|| self.params.get(k))
    }

    /// PlanItem validators: HH:MM time, multi-line values with \n line ends.
    pub(crate) fn normalize(&mut self) -> Result<()> {
        if !fullmatch(r"([01]\d|2[0-3]):[0-5]\d", &self.at) {
            bail!("at must be HH:MM");
        }
        for m in [&mut self.params, &mut self.content] {
            for (k, v) in m.iter_mut().filter(|(k, _)| MULTILINE.contains(&k.as_str())) {
                let _ = k;
                *v = v.replace("\r\n", "\n").replace('\r', "\n");
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DayPlan {
    pub persona: String,
    pub date: String,
    pub items: Vec<PlanItem>,
}

/// The scenario window a day plan keeps lookalike work away from.
#[derive(Debug, Clone, PartialEq)]
pub struct Scenario {
    pub date: String,
    pub start: String,
    pub end: String,
    pub signal: String,
}

impl std::str::FromStr for Scenario {
    type Err = anyhow::Error;
    fn from_str(s: &str) -> Result<Self> {
        let Some(c) = re(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d)-(\d\d:\d\d):(\w+)$").captures(s) else {
            bail!("scenario must look like 2026-10-28T13:00-14:00:powershell")
        };
        Ok(Self { date: c[1].into(), start: c[2].into(), end: c[3].into(), signal: c[4].into() })
    }
}

/// Every template in data/templates, by task name.
pub fn templates() -> Result<BTreeMap<String, Template>> {
    let mut paths: Vec<_> = std::fs::read_dir(crate::paths::data().join("templates"))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "yaml"))
        .collect();
    paths.sort();
    paths.iter().map(|p| load_template(p).map(|t| (t.task.clone(), t))).collect()
}

/// The intranet downloads (file, page title) a plan may name; none without the intranet add-on or when unreadable.
pub(crate) fn downloads(policy: &crate::policy::Policy) -> Vec<(String, String)> {
    #[cfg(feature = "intranet")]
    return crate::intranet::downloads(&crate::intranet::dir(), |t| policy.page_reserved(t)).unwrap_or_default();
    #[cfg(not(feature = "intranet"))]
    {
        let _ = policy;
        vec![]
    }
}

pub(crate) fn intranet_index(policy: &crate::policy::Policy) -> String {
    #[cfg(feature = "intranet")]
    return crate::intranet::index(&crate::intranet::dir(), |t| policy.page_reserved(t)).unwrap_or_default();
    #[cfg(not(feature = "intranet"))]
    {
        let _ = policy;
        String::new()
    }
}

pub(crate) fn leaf(path: &str) -> String {
    re(r"[\\/]").split(path.trim()).last().unwrap_or("").trim().trim_end_matches([' ', '.']).to_string()
}

pub(crate) fn stem(path: &str) -> String {
    re(r"\.[^.\\/]*$").replace(path, "").into_owned()
}

pub(crate) fn minutes(at: &str) -> i64 {
    at[..2].parse::<i64>().unwrap_or(0) * 60 + at[3..].parse::<i64>().unwrap_or(0)
}

#[cfg(test)]
mod tests;
