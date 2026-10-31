//! Template growth (port of growth.py): the planner LLM proposes a new task template, checked here and saved
//! to templates/proposed; it is admitted only through validation on the VM.
use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use anyhow::{Result, bail};
use serde_json::Value;

use crate::llm::Check;
use crate::tasks::{Done, Template};
use crate::template::{format_map, re};

pub const GROWTH_SYSTEM: &str = "You design task templates for an agent that operates a Windows 10 desktop like an office worker, one UI \
action at a time, choosing among the controls visible on screen. A template is a skeleton of subgoals. \
Each subgoal goal is one short, concrete instruction the agent can finish in a few clicks or keystrokes. \
Completion checks read UI Automation state and must be true only once the subgoal really is done. \
Variants are different human ways to do the same task (keyboard shortcut vs menu, Start search vs \
taskbar). Every variant ends by closing the app so the desktop is left clean. Never delete files, change \
system settings, install software, or touch anything outside the persona's own documents. The agent can \
click, double-click, type prepared text, press keys and scroll; it cannot drag or use the clipboard, and \
copy_text only types text visible in the same window as the target field. A done check must be false \
when its subgoal starts.";

pub const COOKIE_CONSENT: &str = "(?i)accept.*|reject.*";
const TASK_CONTENT_KEYS: [&str; 2] = ["body", "reply_to"];

/// The proposal arrives as raw JSON; it is parsed and validated as a template in `check`.
impl Check for Value {}

fn yaml(v: &impl serde::Serialize) -> Result<String> {
    Ok(serde_yaml::to_string(v)?)
}

/// Propose one template (`ask` gets the user prompt, e.g. `llm.json(GROWTH_SYSTEM, u, 0.7, 2)`), with one
/// correction round. Returns the template and the JSON as proposed, to save.
pub fn propose(ask: &mut dyn FnMut(&str) -> Result<Value>, capabilities: &serde_yaml::Value, templates: &BTreeMap<String, Template>, idea: &str) -> Result<(Template, Value)> {
    // ponytail: the template files as examples (Python dumped the models without defaults, about the same text)
    let mut examples = vec![];
    for e in std::fs::read_dir(crate::paths::data().join("templates"))? {
        let p = e?.path();
        if p.extension().is_some_and(|x| x == "yaml") {
            examples.push(std::fs::read_to_string(&p)?);
        }
    }
    examples.sort();
    let mut user = format!(
        "Installed apps and what people do with them:\n{}\nCompletion checks available:\n{}\n\
         Existing templates (do not duplicate their task; copy their style and check patterns):\n{}\n\
         Propose one new template{}. \
         Return it as one JSON object with the same keys as the existing templates (task, app, processes, \
         params, defaults, about, example, variants, subgoals, budget_steps, budget_minutes). 2-4 variants. \
         params are what a planner fills per instance; defaults are fixed values such as the Start search \
         query; example gives a value for every param. Placeholders like {{file_name}} may appear in goals and \
         check regexes. Every subgoal that is not open-ended needs a done check.",
        yaml(&capabilities["apps"])?,
        yaml(&capabilities["checks"])?,
        examples.join("\n---\n"),
        if idea.is_empty() { " for a common office task not covered yet".to_string() } else { format!(": {idea}") },
    );
    for attempt in 0..2 {
        let raw = ask(&user)?;
        let (t, problems) = match Template::from_yaml(&serde_json::to_string(&raw)?) {
            Ok(t) => {
                let p = check(&t, capabilities, templates);
                (Some(t), p)
            }
            Err(e) => (None, vec![format!("{e:#}")]),
        };
        if let (Some(t), true) = (&t, problems.is_empty()) {
            return Ok((t.clone(), raw));
        }
        if attempt == 1 {
            bail!("proposal {:?} rejected: {}", raw.get("task").and_then(Value::as_str).unwrap_or_default(), problems.join("; "));
        }
        user += &format!("\n\nYour proposal was rejected:\n{}\nSend a corrected one.", problems.iter().map(|p| format!("- {p}")).collect::<Vec<_>>().join("\n"));
    }
    unreachable!()
}

/// What a proposal may not do beyond a valid template (the model checks ran in `Template::from_yaml`).
pub fn check(t: &Template, capabilities: &serde_yaml::Value, templates: &BTreeMap<String, Template>) -> Vec<String> {
    let mut problems = vec![];
    if !regex::Regex::new("^[a-z][a-z0-9_]{2,40}$").unwrap().is_match(&t.task) {
        problems.push("task must be snake_case".to_string());
    }
    if templates.contains_key(&t.task) {
        problems.push(format!("task {:?} already exists", t.task));
    }
    let drawn: Vec<&Vec<String>> = t.variants.iter().enumerate().filter(|(i, _)| t.weights.as_ref().is_none_or(|w| w.get(*i).is_some_and(|x| *x != 0.0))).map(|(_, v)| v).collect();
    if !(2..=4).contains(&drawn.len()) {
        problems.push("needs 2-4 variants".into());
    }
    let allowed: BTreeSet<String> = capabilities["apps"].as_sequence().into_iter().flatten().filter_map(|a| a["process"].as_str()).map(str::to_lowercase).collect();
    let bad: Vec<&String> = t.processes.iter().filter(|p| !allowed.contains(&p.to_lowercase())).collect();
    if !bad.is_empty() {
        problems.push(format!("processes outside the catalog: {bad:?}"));
    }
    let base: BTreeSet<String> = t.params.iter().chain(t.defaults.keys()).cloned().collect();
    let mut keys: BTreeSet<String> = base.iter().flat_map(|k| ["typo", "enter", "ui", "line", "stem"].map(|d| format!("{k}_{d}"))).chain(base.iter().cloned()).collect();
    if t.task_content().is_some() {
        problems.push("task_content not permitted for proposals".into());
        keys.extend(TASK_CONTENT_KEYS.map(String::from));
    }
    let missing: Vec<&String> = t.params.iter().filter(|p| t.example.get(*p).is_none_or(String::is_empty) && !t.defaults.contains_key(*p)).collect();
    if !missing.is_empty() {
        problems.push(format!("example lacks {missing:?}"));
    }
    let mut open_ended = 0;
    for (name, sg) in &t.subgoals {
        if let Some(text) = sg.text.as_ref().filter(|x| !x.is_empty() && !keys.contains(*x)) {
            problems.push(format!("{name}: text {text:?} is not a param or default"));
        }
        let unknown: Vec<String> = placeholders(&sg.goal, sg.done.as_ref()).into_iter().filter(|p| !keys.contains(p)).collect();
        if !unknown.is_empty() {
            problems.push(format!("{name}: unknown placeholders {unknown:?}"));
        }
        let extra: Vec<&String> = sg.allow_destructive.iter().filter(|x| *x != COOKIE_CONSENT).collect();
        if !extra.is_empty() {
            problems.push(format!("{name}: allow_destructive not permitted for proposals: {extra:?}"));
        }
        if !sg.search.is_empty() {
            problems.push(format!("{name}: search not permitted for proposals"));
        }
        if sg.unreadable_ok {
            problems.push(format!("{name}: unreadable_ok not permitted for proposals"));
        }
        if sg.done.is_none() {
            open_ended += 1;
        }
        for rx in regexes(sg.done.as_ref()) {
            let filled = format_map(rx, |k| keys.contains(k).then(|| "x".to_string())).and_then(|s| re(&s, false));
            if let Err(e) = filled {
                problems.push(format!("{name}: bad check regex {rx:?}: {e:#}"));
            }
        }
    }
    if open_ended > 1 {
        problems.push("at most one open-ended subgoal (without done)".into());
    }
    for v in drawn {
        let closed = v.last().and_then(|s| t.subgoals.get(s)).and_then(|s| s.done.as_ref()).and_then(|d| d.window.as_ref()).is_some_and(|w| w.absent);
        if !closed {
            problems.push(format!("variant {v:?} must end with a check that the app window is closed (window absent)"));
        }
    }
    problems
}

fn regexes(done: Option<&Done>) -> Vec<&String> {
    let Some(d) = done else { return vec![] };
    let mut out: Vec<&String> = [&d.window, &d.foreground].into_iter().flatten().filter_map(|w| w.title.as_ref()).filter(|x| !x.is_empty()).collect();
    if let Some(e) = &d.element {
        out.extend([&e.name, &e.value].into_iter().flatten().filter(|x| !x.is_empty()));
    }
    out
}

fn placeholders(goal: &str, done: Option<&Done>) -> BTreeSet<String> {
    let names = RefCell::new(BTreeSet::new());
    for s in std::iter::once(goal).chain(regexes(done).into_iter().map(String::as_str)) {
        let seen = |k: &str| {
            names.borrow_mut().insert(k.to_string());
            Some(String::new())
        };
        if format_map(s, seen).is_err() {
            names.borrow_mut().insert("<unbalanced braces>".into());
        }
    }
    names.into_inner()
}

/// templates/proposed/<task>.yaml, marked as a proposal.
pub fn save(raw: &Value, proposed: &Path) -> Result<PathBuf> {
    let task = raw.get("task").and_then(Value::as_str).unwrap_or_default();
    anyhow::ensure!(regex::Regex::new("^[a-z][a-z0-9_]{2,40}$").unwrap().is_match(task), "proposal task {task:?} is not snake_case");
    std::fs::create_dir_all(proposed)?;
    let path = proposed.join(format!("{task}.yaml"));
    std::fs::write(&path, format!("# Proposed by the planner LLM; admitted only via `ludius validate`.\n{}", yaml(raw)?))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn proposals_get_one_correction_round() {
        crate::day::tests::log("growth");
        let caps: serde_yaml::Value = serde_yaml::from_str(&std::fs::read_to_string(crate::paths::data().join("capabilities.yaml")).unwrap()).unwrap();
        let templates = crate::planner::templates().unwrap();
        let good = serde_json::json!({
            "task": "notepad_word_wrap", "app": "notepad", "processes": ["notepad.exe"], "params": ["doc_title"], "example": {"doc_title": "notes"},
            "variants": [["open", "wrap", "close"], ["open", "wrap_menu", "close"]],
            "subgoals": {
                "open": {"goal": "Open {doc_title} in Notepad", "done": {"window": {"title": "(?i){doc_title}.*Notepad"}}},
                "wrap": {"goal": "Turn on word wrap with the Format menu", "allow_destructive": [COOKIE_CONSENT]},
                "wrap_menu": {"goal": "Turn on word wrap", "done": {"element": {"name": "Word Wrap"}}},
                "close": {"goal": "Close Notepad", "done": {"window": {"process": "notepad.exe", "absent": true}}}
            }
        });
        let mut bad = good.clone();
        bad["task"] = "notepad_write_note".into();
        bad["subgoals"]["wrap"]["goal"] = "Wrap {nope".into();
        bad["subgoals"]["wrap"]["done"] = serde_json::json!({"element": {"name": "{other}("}});
        let t: Template = Template::from_yaml(&bad.to_string()).unwrap();
        let p = check(&t, &caps, &templates);
        assert!(p.iter().any(|x| x.contains("already exists")) && p.iter().any(|x| x.contains("<unbalanced braces>")), "{p:?}");
        assert!(p.iter().any(|x| x.contains("bad check regex")), "{p:?}");
        let mut answers = vec![bad, good.clone()].into_iter();
        let mut prompts = vec![];
        let (t, raw) = propose(&mut |u| { prompts.push(u.to_string()); Ok(answers.next().unwrap()) }, &caps, &templates, "").unwrap();
        assert_eq!((t.task.as_str(), prompts.len()), ("notepad_word_wrap", 2));
        assert!(prompts[1].contains("Your proposal was rejected"));
        let dir = std::env::temp_dir().join(format!("ludius-growth-{}", std::process::id()));
        let saved = std::fs::read_to_string(save(&raw, &dir).unwrap()).unwrap();
        assert!(saved.starts_with("# Proposed") && Template::from_yaml(&saved).is_ok());
        assert!(propose(&mut |_| Ok(serde_json::json!({"task": "x"})), &caps, &templates, "").is_err());
    }
}
