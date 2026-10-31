//! One subgoal: observe, guard, decide, act, until done, stuck or out of budget.
use std::collections::{HashMap, HashSet};

use anyhow::{Result, anyhow};
use ludius_proto::v1::{Element, Snapshot};
use serde_json::json;

use super::act::{launch_text, no_text, typed_text};
use super::wrapup::{equivalent, ident, typed_landed};
use super::*;
use crate::decisions::{Decision, DecisionError};
use crate::keys::chord;
use crate::options::{Opt, StepView, build_options, build_state, describe, launch_target, search_target, view};
use crate::policy::{OVERWRITE_REASON, overwrite_prompt};
use crate::tasks::{RESERVED_RECIPIENT, added_baseline, check_done, declared_commits, preconditions, recipient_reserved, window_matches};

type Res = (bool, i64, String);

fn failed(steps: i64, why: impl Into<String>) -> Result<Res> {
    Ok((false, steps, why.into()))
}

fn opt(key: usize, kind: &str, text: String) -> Opt {
    Opt { key: format!("o{key}"), kind: kind.into(), text, ..Default::default() }
}

impl Runner<'_> {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn subgoal_steps(&mut self, task: &mut Task, name: &str, sg: &Subgoal, budget: i64, t0: Instant, may_detour: bool, mut carry: Option<&mut Option<Carry>>) -> Result<Res> {
        let t = task.template.task.clone();
        let mut history: Vec<String> = Vec::new();
        let mut verifications = 0;
        let mut last_result = "start of subgoal".to_string();
        let mut no_change = 0;
        let (mut typed_ok, mut pressed_ok, mut clicked_ok) = (false, false, false);
        let mut last_click: Option<Element> = None;
        let mut recovering = false;
        let mut dead: HashMap<Fingerprint, HashSet<(String, String)>> = HashMap::new();
        let mut snap = self.observe(5000)?;
        match carry.as_deref_mut() {
            Some(Some(c)) => {
                (typed_ok, pressed_ok, clicked_ok, last_click) = (c.0, c.1, c.2, c.3.clone());
                task.added_base = c.4;
            }
            c => {
                added_baseline(sg.done.as_ref(), &snap, task)?;
                if let Some(c) = c {
                    *c = Some((typed_ok, pressed_ok, clicked_ok, last_click.clone(), task.added_base));
                }
            }
        }
        let mut seen: HashSet<Fingerprint> = HashSet::from([fingerprint(&snap)]);
        let (mut escs, mut ladders, mut blocks) = (0, 0, 0);
        let mut sat: Vec<String> = Vec::new();
        let mut shot_why: Option<&'static str> = None;
        let mut step: i64 = 0;
        let (mut waited, mut wait_why) = (0.0f64, String::new());
        let ask = sg.done.as_ref().and_then(|d| d.ask.clone());
        while step < budget {
            step += 1;
            self.live = step;
            if t0.elapsed().as_secs_f64() > task.template.budget_minutes * 60.0 {
                return failed(step - 1, "time budget");
            }
            if waited > WAIT_CAP_S {
                return failed(step - 1, format!("waited too long: {wait_why}"));
            }
            if snap.locked {
                return failed(step - 1, "session locked");
            }
            if !self.mapping_ok {
                return failed(step - 1, "pointer mapping drift after a display change");
            }
            if snap.secure_desktop {
                self.agent.chord(chord("Esc")?)?;
                self.log.write(json!({"event": "blocked", "host_ms": now_ms(), "task": t, "subgoal": name, "reason": "elevation required"}))?;
                return failed(step - 1, "blocked: elevation required");
            }
            let code_done = check_done(sg.done.as_ref(), &snap, task, typed_ok, true, pressed_ok, clicked_ok, last_click.as_ref())?;
            if code_done == Some(true) {
                self.log.write(json!({"event": "subgoal_done", "host_ms": now_ms(), "guest_ms": snap.guest_time_ms, "task": t, "subgoal": name, "by": "code", "steps": step - 1}))?;
                return Ok((true, step - 1, String::new()));
            }
            if code_done == Some(false) && check_done(sg.done.as_ref(), &snap, task, typed_ok, false, pressed_ok, clicked_ok, last_click.as_ref())? == Some(true) {
                self.log.write(json!({"event": "wait", "host_ms": now_ms(), "task": t, "subgoal": name, "step": step, "reason": "done on screen, waiting for the outside check"}))?;
                let w0 = Instant::now();
                std::thread::sleep(BLOCKED_WAIT);
                snap = self.observe(10000)?;
                (step, waited, wait_why) = (step - 1, waited + w0.elapsed().as_secs_f64(), "the outside check".into());
                continue;
            }
            if let Some(fg) = foreground(&snap)
                && fg.unresponsive
            {
                // A hung app buffers keys and replays them later; wait for it like a person would.
                // The task's time budget bounds the wait, not WAIT_CAP_S: a busy Outlook can take minutes.
                self.log.write(json!({"event": "wait", "host_ms": now_ms(), "task": t, "subgoal": name, "step": step, "reason": format!("'{}' is not responding", fg.title)}))?;
                std::thread::sleep(HUNG_WAIT);
                snap = self.observe(10000)?;
                step -= 1;
                continue;
            }
            if let Some(q) = &ask
                && self.decider_says_done(task, name, q, &snap)?
            {
                self.log.write(json!({"event": "subgoal_done", "host_ms": now_ms(), "guest_ms": snap.guest_time_ms, "task": t, "subgoal": name, "by": "decider", "steps": step - 1}))?;
                return Ok((true, step - 1, String::new()));
            }
            if recipient_reserved(sg, task, &self.policy)? {
                self.log.write(json!({"event": "blocked", "host_ms": now_ms(), "task": t, "subgoal": name, "reason": RESERVED_RECIPIENT}))?;
                return failed(step - 1, RESERVED_RECIPIENT);
            }
            if overwrite_prompt(&snap) {
                self.log.write(json!({"event": "blocked", "host_ms": now_ms(), "task": t, "subgoal": name, "reason": OVERWRITE_REASON}))?;
                return failed(step - 1, OVERWRITE_REASON);
            }
            let blocked = preconditions(sg, &snap, task, None)?;
            let popup = if blocked.is_some() { popup_over(sg, &snap, task)? } else { None };
            match popup {
                None => sat.clear(),
                Some(p) => {
                    if sat.last() != Some(&p) {
                        sat.clear();
                    }
                    sat.push(p.clone());
                    if sat.len() % 3 == 0 && may_detour {
                        return failed(step - 1, format!("{STUCK}: '{p}' is in front of the task window"));
                    }
                    if sat.len() % 3 == 0 && escs < 2 {
                        escs += 1;
                        self.agent.chord(chord("Esc")?)?;
                        self.log.write(json!({"event": "dismiss", "host_ms": now_ms(), "task": t, "subgoal": name, "step": step, "window": p}))?;
                    }
                }
            }
            blocks = if blocked.is_some() { blocks + 1 } else { 0 };
            if let Some(b) = blocked {
                if blocks >= STUCK_WAITS && may_detour {
                    return failed(step - 1, format!("{STUCK}: {b}"));
                }
                self.log.write(json!({"event": "wait", "host_ms": now_ms(), "task": t, "subgoal": name, "step": step, "reason": b}))?;
                let w0 = Instant::now();
                self.refocus(task, name, sg, &snap)?;
                std::thread::sleep(BLOCKED_WAIT);
                snap = self.observe(10000)?;
                (step, waited, wait_why) = (step - 1, waited + w0.elapsed().as_secs_f64(), b);
                continue;
            }
            let v = view(&snap, task, sg, &self.policy);
            let all = if recovering { recovery_options(&v) } else { build_options(&v, task, sg, &self.policy)? };
            let here = fingerprint(&snap);
            let mut options: Vec<Opt> = all.iter().filter(|o| !dead.get(&here).is_some_and(|d| d.contains(&ident(o)))).cloned().collect();
            if options.is_empty() {
                options = all;
            }
            let state = build_state(&v, task, sg, &history, &last_result)?;
            let choices: Vec<(String, String)> = options.iter().map(|o| (o.key.clone(), o.text.clone())).collect();
            let mut image = image_reason(shot_why, &snap, self.image);
            shot_why = None;
            let shot = if image.is_some() { self.shot(&here) } else { None };
            if shot.is_none() {
                image = None;
            }
            let decision = match self.decider.decide(&state, &choices, !recovering, shot.as_deref()) {
                Err(e) if e.is::<DecisionError>() => {
                    self.log.write(json!({"event": "decision_error", "host_ms": now_ms(), "task": t, "subgoal": name, "step": step, "error": short(&e.to_string(), 300)}))?;
                    return failed(step - 1, "decision error");
                }
                r => r?,
            };
            let chosen = self.choose(&decision, &options)?;

            let mut decider_done = code_done.is_none() && ask.is_none() && decision.subgoal_done >= DONE_THRESHOLD && !recovering;
            if decider_done {
                if let Some(a) = &self.assist
                    && verifications < 2
                {
                    verifications += 1;
                    let verdict = a.verify(&state)?;
                    self.log.write(json!({"event": "llm_verify", "host_ms": now_ms(), "task": t, "subgoal": name, "done": verdict.done, "reason": verdict.reason}))?;
                    // Python also sets last_result to "not done yet: ..." here; every path below overwrites it.
                    decider_done = verdict.done;
                }
                if decider_done {
                    self.log.write(json!({"event": "subgoal_done", "host_ms": now_ms(), "guest_ms": snap.guest_time_ms, "task": t, "subgoal": name, "by": "decision", "p": decision.subgoal_done, "steps": step - 1}))?;
                    return Ok((true, step - 1, String::new()));
                }
            }
            if !recovering && decision.off_track >= OFF_TRACK_THRESHOLD {
                recovering = true;
                last_result = "screen looked unrelated to the goal".into();
                self.log_step(task, name, &snap, &chosen, &decision, "off_track -> recovery", step, 0, &options, None, code_done, image)?;
                continue;
            }
            if let Some(reason) = self.post_check(&chosen, sg, task)? {
                self.log_step(task, name, &snap, &chosen, &decision, &format!("blocked: {reason}"), step, 0, &options, None, code_done, image)?;
                last_result = format!("that action is not allowed ({reason})");
                shot_why = Some("refused");
                continue;
            }

            self.think();
            let (host_ms, w0) = (now_ms(), Instant::now());
            let refused = self.act(&chosen, task, Some(sg))?;
            let rescan = chosen.kind == "rescan";
            let guarded = !sg.require.is_empty() && refused.is_none() && !rescan;
            snap = self.observe(if rescan { 10000 } else { 5000 })?;
            if rescan {
                (step, waited, wait_why) = (step - 1, waited + w0.elapsed().as_secs_f64(), "the screen to load".into());
            }
            let slow_window = sg.done.as_ref().is_some_and(|d| d.window.is_some() || d.foreground.is_some());
            if chosen.kind == "key" && refused.is_none() && sg.keys.contains(&chosen.keys) {
                let slow = fingerprint(&snap) == here
                    || slow_window && check_done(sg.done.as_ref(), &snap, task, typed_ok, false, pressed_ok, clicked_ok, last_click.as_ref())? != Some(true);
                if slow {
                    std::thread::sleep(SLOW_KEY_WAIT);
                    snap = self.observe(10000)?;
                }
            }
            let after = fingerprint(&snap);
            let (changed, revisit) = (after != here, seen.contains(&after));
            seen.insert(after);
            if chosen.kind == "type" {
                typed_ok = refused.is_none() && typed_landed(&snap, &chosen, sg);
            }
            pressed_ok = pressed_ok || (guarded && chosen.kind == "key" && sg.keys.contains(&chosen.keys));
            clicked_ok = clicked_ok || (sg.in_window.is_some() && refused.is_none() && chosen.kind == "click");
            if !rescan {
                last_click = if chosen.kind == "click" && refused.is_none() { chosen.element.clone() } else { None };
            }
            if let Some(c) = carry.as_deref_mut() {
                *c = Some((typed_ok, pressed_ok, clicked_ok, last_click.clone(), task.added_base));
            }
            let mut outcome = if changed && revisit { "back to an earlier screen" } else if changed { "changed" } else { "no change" }.to_string();
            last_result = if changed { outcome.clone() } else { "no visible change".into() };
            if let Some(r) = &refused {
                outcome = format!("not typed: {r}");
                last_result = outcome.clone();
            }
            if !snap.settled {
                last_result += " (screen still busy)";
            }
            history.push(format!("{} -> {outcome}", wrapup::short_opt(&chosen)));
            self.log_step(task, name, &snap, &chosen, &decision, &last_result, step, host_ms, &options, Some((sg, typed_ok)), code_done, image)?;
            shot_why = if refused.is_some() { Some("refused") } else if !changed { Some("no_change") } else { None };
            recovering = false;

            let progress = changed && !revisit;
            no_change = if progress { 0 } else { no_change + 1 };
            if !progress && !rescan && refused.as_deref() != Some(FRONT_MOVED) {
                dead.entry(here).or_default().insert(ident(&chosen));
            }
            if no_change == 2 {
                snap = self.observe(10000)?;
                self.log.write(json!({"event": "ladder", "step": 1, "action": "rescan", "host_ms": now_ms(), "task": t}))?;
            } else if no_change >= 3 {
                ladders += 1;
                if may_detour && ladders >= 2 {
                    return failed(step, format!("{STUCK}: no progress, even after in-app recovery"));
                }
                recovering = true;
                no_change = 0;
                self.log.write(json!({"event": "ladder", "step": 2, "action": "in-app recovery", "host_ms": now_ms(), "task": t}))?;
            }
        }
        failed(budget, "step budget")
    }

    pub(super) fn choose(&mut self, d: &Decision, options: &[Opt]) -> Result<Opt> {
        let top = options.iter().find(|o| o.key == d.choice).ok_or_else(|| anyhow!("choice {:?} is not an option", d.choice))?;
        let p_top = d.probabilities.get(&d.choice).copied().unwrap_or(0.0);
        let ties: Vec<&Opt> = d
            .probabilities
            .iter()
            .filter(|(_, p)| **p >= p_top - NEAR_TIE)
            .filter_map(|(k, _)| options.iter().find(|o| &o.key == k))
            .filter(|o| equivalent(o, top))
            .collect();
        Ok(if ties.len() > 1 { ties[self.rng.random_range(0..ties.len())].clone() } else { top.clone() })
    }

    fn post_check(&self, o: &Opt, sg: &Subgoal, task: &Task) -> Result<Option<String>> {
        if o.kind != "rescan" {
            if recipient_reserved(sg, task, &self.policy)? {
                return Ok(Some(RESERVED_RECIPIENT.into()));
            }
            if let Some(r) = preconditions(sg, &self.snap, task, o.element.as_ref())? {
                return Ok(Some(r));
            }
        }
        if let Some(e) = &o.element
            && let Some(r) = self.policy.element_allowed(e, &sg.allow_destructive, Some(&self.snap))
        {
            return Ok(Some(r));
        }
        match o.kind.as_str() {
            "type" => {
                if overwrite_prompt(&self.snap) {
                    return Ok(Some(OVERWRITE_REASON.into()));
                }
                if no_text(sg) && !sg.copy_text && !search_target(o.element.as_ref(), &self.snap, task) {
                    return Ok(Some("template text goes into an edit field of the task's foreground app only".into()));
                }
                let focus = self.snap.elements.iter().find(|e| e.focused);
                if launch_text(sg) && !launch_target(o.element.as_ref().or(focus), &self.snap) {
                    return Ok(Some(LAUNCH_REASON.into()));
                }
                Ok(self.policy.text_allowed(&typed_text(o, task)?, o.element.as_ref()))
            }
            "key" => Ok(self.policy.key_allowed(&o.keys, &self.snap, &declared_commits(task, sg))),
            _ => Ok(None),
        }
    }
}

fn popup_over(sg: &Subgoal, snap: &Snapshot, task: &Task) -> Result<Option<String>> {
    let (Some(want), Some(fg)) = (&sg.in_window, foreground(snap)) else { return Ok(None) };
    if window_matches(fg, want, task)? {
        return Ok(None);
    }
    let p = fg.process_name.to_lowercase();
    Ok(task.template.processes.iter().any(|x| x.to_lowercase() == p).then(|| fg.title.clone()))
}

fn recovery_options(v: &StepView) -> Vec<Opt> {
    let mut opts = vec![
        Opt { keys: "Esc".into(), ..opt(1, "key", "press Esc".into()) },
        Opt { keys: "Alt+Left".into(), ..opt(2, "key", "press Alt+Left (back)".into()) },
    ];
    for e in &v.allowed {
        if e.hints.iter().any(|h| h == "closes dialog") || ["close", "cancel", "back"].contains(&e.name.to_lowercase().as_str()) {
            opts.push(Opt { element: Some(e.clone()), ..opt(opts.len() + 1, "click", format!("click {}", describe(e))) });
        }
    }
    opts.push(opt(opts.len() + 1, "rescan", "rescan: wait and look again".into()));
    opts
}
