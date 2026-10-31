//! Acting on one chosen option, with the checks that run right before the input lands.
use anyhow::{Result, anyhow};
use ludius_proto::v1::{Element, Snapshot};
use serde_json::json;

use super::*;
use super::wrapup::window_id;
use crate::calls::CapReached;
use crate::keys::chord;
use crate::options::{Opt, REASON_OTHER_FIELD, commits_unguarded, describe, field_of_another_step, launch_target, search_target, view};
use crate::tasks::{LAUNCH_TEXT, guard_matches, preconditions};
use crate::template::search;

pub(crate) fn replace_field(e: &Element, task: &Task, sg: &Subgoal) -> Result<bool> {
    let field = if !sg.field.is_empty() {
        sg.field.clone()
    } else {
        sg.done.as_ref().and_then(|d| d.element.as_ref()).and_then(|c| c.name.clone()).unwrap_or_default()
    };
    Ok(search(&task.fmt_re(&field)?, &e.name, false))
}

pub(crate) fn launch_text(sg: &Subgoal) -> bool {
    sg.text.as_deref() == Some(LAUNCH_TEXT)
}

pub(crate) fn no_text(sg: &Subgoal) -> bool {
    sg.text.as_deref().unwrap_or("").is_empty()
}

pub(crate) fn typed_text(o: &Opt, task: &Task) -> Result<String> {
    if !o.literal.is_empty() {
        return Ok(o.literal.clone());
    }
    task.values.get(&o.content).cloned().ok_or_else(|| anyhow!("no value {:?}", o.content))
}

impl Runner<'_> {
    pub(super) fn refocus(&mut self, task: &Task, name: &str, sg: &Subgoal, snap: &Snapshot) -> Result<()> {
        let mut loose_sg = sg.clone();
        for c in &mut loose_sg.require {
            c.focused = None;
        }
        if preconditions(&loose_sg, snap, task, None)?.is_some() {
            return Ok(());
        }
        let fg = crate::tasks::foreground(snap).map(|i| i as u32);
        for c in sg.require.iter().filter(|c| c.focused == Some(true)) {
            let mut loose = c.clone();
            loose.focused = None;
            let mut hits = Vec::new();
            for e in &snap.elements {
                if Some(e.window) == fg && !e.focused && guard_matches(e, &loose, task)? && self.policy.element_allowed(e, &sg.allow_destructive, Some(snap)).is_none() {
                    hits.push(e.clone());
                }
            }
            if hits.len() == 1 {
                self.move_to(&hits[0])?;
                self.agent.click(1)?;
                self.caret_to_end(&hits[0], Some(sg))?;
                self.log.write(json!({"event": "refocus", "host_ms": now_ms(), "task": task.template.task, "subgoal": name, "element": describe(&hits[0])}))?;
                return Ok(());
            }
        }
        Ok(())
    }

    /// A screenshot of the screen `here` describes; None when it moved meanwhile or can't be taken.
    pub(super) fn shot(&self, here: &Fingerprint) -> Option<Vec<u8>> {
        let shot = self.agent.screenshot().ok()?;
        let now = self.agent.observe(false, 300, 5000).ok()?;
        (fingerprint(&now) == *here).then_some(shot)
    }

    pub(super) fn decider_says_done(&self, task: &Task, name: &str, question: &str, snap: &Snapshot) -> Result<bool> {
        let Some(shot) = self.shot(&fingerprint(snap)) else { return Ok(false) };
        let q = task.fmt(question)?;
        let options = vec![("yes".to_string(), format!("Yes: {q}")), ("no".to_string(), "No, or it can't be seen on the screen.".to_string())];
        let d = match self.decider.decide(&json!({"question": q}), &options, false, Some(&shot)) {
            Err(e) if e.is::<CapReached>() => return Err(e),
            Err(e) => {
                // Fail closed: no answer is no.
                self.log.write(json!({"event": "decider_check", "host_ms": now_ms(), "task": task.template.task, "subgoal": name, "error": short(&e.to_string(), 300)}))?;
                return Ok(false);
            }
            Ok(d) => d,
        };
        let yes = d.probabilities.get("yes").copied().unwrap_or(0.0);
        self.log.write(json!({"event": "decider_check", "host_ms": now_ms(), "task": task.template.task, "subgoal": name, "yes": round3(yes), "cost": d.cost}))?;
        Ok(d.choice == "yes" && yes >= DONE_THRESHOLD)
    }

    pub(super) fn act(&mut self, o: &Opt, task: &Task, sg: Option<&Subgoal>) -> Result<Option<String>> {
        let acts = ["click", "double_click", "type", "key"].contains(&o.kind.as_str());
        if sg.is_some_and(|s| s.confirm.is_some() && acts && !s.keys.contains(&o.keys)) {
            return Ok(Some("only this step's confirmed keys may act here (a Paste button would skip the confirm question)".into()));
        }
        match (o.kind.as_str(), &o.element) {
            ("click" | "double_click", Some(e)) => {
                self.move_to(e)?;
                // Checked last, right before the input: a popup since the decision (Outlook Reminders) would get it.
                if self.front_moved()? {
                    return Ok(Some(FRONT_MOVED.into()));
                }
                self.agent.click(if o.kind == "double_click" { 2 } else { 1 })?;
                if o.kind == "click" {
                    self.caret_to_end(e, sg)?;
                }
            }
            ("type", _) => {
                let text = typed_text(o, task)?;
                let Some(sg) = sg else { return Ok(Some("typing needs its subgoal".into())) };
                let (why, mut e) = self.type_target(o, task, sg, &text)?;
                if why.is_some() {
                    return Ok(why);
                }
                if let Some(t) = e.clone()
                    && (!t.focused || self.focus_shared)
                {
                    self.move_to(&t)?;
                    self.agent.click(1)?;
                    std::thread::sleep(Duration::from_millis(150));
                    let (why, again) = self.type_target(o, task, sg, &text)?;
                    if why.is_some() {
                        return Ok(why);
                    }
                    match &again {
                        Some(a) if a.focused => self.caret_to_end(a, Some(sg))?,
                        _ => return Ok(Some("the focusing click did not put the keyboard focus in the target field".into())),
                    }
                    e = again;
                }
                if sg.caret == "append" && e.as_ref().is_some_and(|e| e.role == "document") {
                    self.agent.chord(chord("Ctrl+End")?)?;
                }
                let replace = match &e {
                    Some(e) if sg.replace => replace_field(e, task, sg)?,
                    _ => false,
                };
                if replace || launch_text(sg) {
                    self.agent.chord(chord("Ctrl+A")?)?;
                }
                self.type_text(&text)?;
            }
            ("key", _) => {
                if let Some(s) = sg
                    && task.template.key_commits.iter().any(|c| c.keys.to_lowercase() == o.keys.to_lowercase())
                {
                    let fresh = self.agent.observe(false, 300, 5000)?;
                    if commits_unguarded(&o.keys, fresh.elements.iter().find(|x| x.focused), task, s)? {
                        return Ok(Some(format!("{} commits in the focused field: only the guarded send step may, in its field", o.keys)));
                    }
                }
                if let Some(s) = sg
                    && let Some(confirm) = &s.confirm
                    && s.keys.contains(&o.keys)
                {
                    let fresh = self.agent.observe(true, 300, 5000)?;
                    if !self.decider_says_done(task, "confirm", confirm, &fresh)? {
                        return Ok(Some(format!("the Decider did not confirm: {}", task.fmt(confirm)?)));
                    }
                }
                // After the confirm question too: Alt+S in Reminders snoozes.
                if self.front_moved()? {
                    return Ok(Some(FRONT_MOVED.into()));
                }
                self.agent.chord(chord(&o.keys)?)?;
            }
            ("scroll", _) => {
                let target = self
                    .snap
                    .elements
                    .iter()
                    .find(|x| x.hints.join(" ").contains("list continues"))
                    .or_else(|| self.snap.elements.iter().find(|x| x.role == "document"))
                    .cloned();
                if let Some(t) = target {
                    self.move_to(&t)?;
                }
                self.agent.scroll(o.notches)?;
            }
            _ => {}
        }
        Ok(None)
    }

    fn front_moved(&self) -> Result<bool> {
        let Some(was) = foreground(&self.snap) else { return Ok(false) };
        let fresh = self.agent.observe(false, 300, 5000)?;
        Ok(foreground(&fresh).is_none_or(|now| window_id(now) != window_id(was)))
    }

    fn type_target(&mut self, o: &Opt, task: &Task, sg: &Subgoal, text: &str) -> Result<(Option<String>, Option<Element>)> {
        let no = |why: &str| Ok((Some(why.to_string()), None));
        let fresh = self.agent.observe(false, 300, 5000)?;
        self.focus_shared = fresh.elements.iter().filter(|e| e.focused).count() > 1;
        let v = view(&fresh, task, sg, &self.policy);
        let fg = crate::tasks::foreground(&fresh);
        let moved = match (fg, foreground(&self.snap)) {
            (Some(i), Some(was)) => window_id(&fresh.windows[i]) != window_id(was) || !v.windows.contains_key(&(i as u32)),
            _ => true,
        };
        if moved {
            return no(FRONT_MOVED);
        }
        let allowed = |key: &str| v.allowed.iter().find(|e| e.key == key);
        let Some(want) = &o.element else {
            let focus = fresh.elements.iter().find(|e| e.focused);
            if let Some(f) = focus
                && (allowed(&f.key).is_none() || self.policy.text_allowed(text, Some(f)).is_some())
            {
                return no("the keyboard focus is not a field this task may type into");
            }
            if field_of_another_step(focus, task, sg)? {
                return no(REASON_OTHER_FIELD);
            }
            if launch_text(sg) && !launch_target(focus, &fresh) {
                return no(LAUNCH_REASON);
            }
            if sg.replace
                && let Some(f) = focus
            {
                if self.focus_shared || !replace_field(f, task, sg)? {
                    return no(REPLACE_REASON);
                }
                return Ok((None, Some(f.clone())));
            }
            if sg.caret == "append" {
                return match focus {
                    Some(f) if f.role == "document" && !self.focus_shared => Ok((None, Some(f.clone()))),
                    _ => no("text is appended only at the end of the focused document"),
                };
            }
            return Ok((None, None));
        };
        let target = match allowed(&want.key) {
            Some(t) if t.role == want.role && t.name == want.name => t,
            _ => return no("the target field changed before typing"),
        };
        if let Some(r) = self.policy.text_allowed(text, Some(target)) {
            return Ok((Some(r), None));
        }
        if field_of_another_step(Some(target), task, sg)? {
            return no(REASON_OTHER_FIELD);
        }
        if no_text(sg) && !sg.copy_text && !search_target(Some(target), &fresh, task) {
            return no("template text goes into an edit field of the task's foreground app only");
        }
        if launch_text(sg) && !launch_target(Some(target), &fresh) {
            return no(LAUNCH_REASON);
        }
        Ok((None, Some(target.clone())))
    }
}
