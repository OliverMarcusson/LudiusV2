//! planner.load_plan: a day plan is validated whole when loaded, before any of it runs.
use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use anyhow::{Result, bail};

use super::check::{asked_files, item_problem, meeting_day, owned_problem, recipient, sender, uses_own_file};
use super::{DayPlan, Narrative, fullmatch, owned_files, templates};
use crate::policy::Policy;

const MESSAGE_ID: &str = r"<[!-;=?-~]{1,200}@[!-;=?-~]{1,100}>";

/// Load a day plan. `history`: an old plan read for its record (shape only, duplicate slots dropped);
/// otherwise every item is validated as the controller would run it.
pub fn load_plan(path: &Path, history: bool) -> Result<DayPlan> {
    let mut plan: DayPlan = crate::tasks::load_yaml(path)?;
    for it in &mut plan.items {
        it.normalize().map_err(|e| anyhow::anyhow!("{}: {e}", path.display()))?;
    }
    let mut count: BTreeMap<(String, String), usize> = BTreeMap::new();
    for it in &plan.items {
        *count.entry((it.at.clone(), it.template.clone())).or_default() += 1;
    }
    let dup: BTreeSet<_> = count.into_iter().filter(|(_, n)| *n > 1).map(|(k, _)| k).collect();
    if !dup.is_empty() {
        if !history {
            bail!("{}: items share a time and template: {dup:?}", path.display());
        }
        plan.items.retain(|it| !dup.contains(&(it.at.clone(), it.template.clone())));
    }
    if history {
        return Ok(plan);
    }
    let (policy, templates) = (Policy::from_env()?, templates()?);
    let root = path.parent().and_then(Path::parent).unwrap_or(Path::new("."));
    let narrative = root.join("world").join("narrative.yaml");
    let world = if narrative.exists() { Some(Narrative::load(&narrative)?) } else { None };
    let (teams, shares) = world.as_ref().map(|w| (w.teams(&plan.persona), w.team_shares(&plan.persona))).unwrap_or_default();
    let at = |it: &super::PlanItem| format!("{}: {} at {}", path.display(), it.template, it.at);
    let edits: Vec<_> = plan.items.iter().filter(|it| templates.get(&it.template).is_some_and(uses_own_file)).collect();
    let owned = if edits.is_empty() { BTreeMap::new() } else { owned_files(root, &plan.persona, &plan.date)? };
    for it in edits {
        let mut mine = owned.clone();
        mine.extend(asked_files(it, &plan.items, &templates, &plan.date));
        if let Some(why) = owned_problem(it, &templates[&it.template], &mine, &plan.date, &plan.items) {
            bail!("{}: {why}", at(it));
        }
    }
    for it in &plan.items {
        if templates.contains_key(&it.template)
            && let Some(why) = item_problem(it, &templates, &policy, &plan.persona, &teams, &shares)?.or_else(|| meeting_day(it, &plan.date))
        {
            bail!("{}: {why}", at(it));
        }
        if !it.request.is_empty() && !fullmatch(MESSAGE_ID, &it.request) {
            bail!("{}: request {:?} is not a message id", at(it), it.request);
        }
    }
    let people = world.as_ref().map(Narrative::people_names).unwrap_or_default();
    let mates: BTreeSet<String> = world.as_ref().map(|w| w.colleagues(&plan.persona).into_iter().chain(w.boss(&plan.persona)).collect()).unwrap_or_default();
    let others: BTreeSet<String> = people.keys().filter(|u| **u != plan.persona).cloned().collect();
    for it in &plan.items {
        if let Some(why) = recipient(it, &mates, &people).or_else(|| sender(it, &others)) {
            bail!("{}: {why}", at(it));
        }
    }
    Ok(plan)
}
