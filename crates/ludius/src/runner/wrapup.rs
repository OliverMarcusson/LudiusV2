//! Abandoning a task, the step record, and the small helpers they share.
use anyhow::Result;
use ludius_proto::v1::{Element, Snapshot, Window};
use serde_json::{Map, Value, json};

use super::*;
use crate::decisions::Decision;
use crate::keys::chord;
use crate::options::Opt;
use crate::policy::alt_f4_safe;
use crate::tasks::{check_done, profile};
use crate::template::{fullmatch, search};

const FILENAME_ROWS: usize = 6;
const FILENAME_TEXT: usize = 80;

pub fn image_reason(why: Option<&'static str>, snap: &Snapshot, forced: bool) -> Option<&'static str> {
    let unreadable = foreground(snap).is_some_and(|w| w.no_tree || w.elevated_unreadable || w.unresponsive || pixels_window(w));
    why.or(if unreadable { Some("unreadable") } else if forced { Some("forced") } else { None })
}

pub(crate) fn window_id(w: &Window) -> (String, String, i32, String) {
    (w.process_name.to_lowercase(), w.class_name.clone(), w.kind, if pixels_window(w) { String::new() } else { w.title.clone() })
}

pub(crate) fn typed_landed(snap: &Snapshot, o: &Opt, sg: &Subgoal) -> bool {
    let Some(e) = &o.element else { return true };
    if sg.unreadable_ok && !e.actions.iter().any(|a| a == "value") && e.value.is_empty() {
        return true;
    }
    snap.elements.iter().find(|x| x.key == e.key).is_some_and(|t| t.value != e.value)
}

fn norm(name: &str) -> String {
    crate::template::re(r"[&.\s]+", false).expect("static pattern").replace_all(name, " ").trim().to_lowercase()
}

pub(crate) fn equivalent(a: &Opt, b: &Opt) -> bool {
    match (&a.element, &b.element) {
        (Some(x), Some(y)) if a.kind == b.kind && ["click", "double_click"].contains(&a.kind.as_str()) => {
            let n = norm(&x.name);
            !n.is_empty() && n == norm(&y.name)
        }
        _ => a.key == b.key,
    }
}

pub(crate) fn ident(o: &Opt) -> (String, String) {
    (o.kind.clone(), o.element.as_ref().map_or_else(|| o.text.clone(), |e| e.key.clone()))
}

pub(crate) fn short_opt(o: &Opt) -> String {
    if o.text.chars().count() <= 90 { o.text.clone() } else { format!("{}…", short(&o.text, 90)) }
}

fn shown(e: &Element) -> Value {
    let b = e.bounds.unwrap_or_default();
    json!({"name": short(&e.name, FILENAME_TEXT), "x": b.x, "y": b.y, "w": b.width, "h": b.height})
}

fn filename_diag(snap: &Snapshot, task: &mut Task, sg: &Subgoal, typed_ok: bool) -> Result<Option<Value>> {
    let pats: Vec<String> = sg.done.as_ref().and_then(|d| d.pixels.as_ref()).map(|p| p.present.clone()).unwrap_or_default();
    let Some(label_rx) = pats.iter().find(|p| p.starts_with("^Filename:")).map(|p| task.pixels_re(p)) else { return Ok(None) };
    if !pats.iter().any(|p| p.contains("{file_name")) || !snap.windows.iter().any(pixels_window) {
        return Ok(None);
    }
    let rows: Vec<&Element> = snap.elements.iter().filter(|e| snap.windows.get(e.window as usize).is_some_and(pixels_window)).collect();
    let ws = crate::template::re(r"\s+", false)?;
    let labels: Vec<&Element> = rows.iter().copied().filter(|e| search(&label_rx, &ws.replace_all(&e.name, ""), true)).collect();
    let mut out = Map::new();
    out.insert("verdict".into(), json!(check_done(sg.done.as_ref(), snap, task, typed_ok, true, false, false, None)?));
    out.insert("typed_ok".into(), json!(typed_ok));
    out.insert("rows".into(), json!(rows.len()));
    out.insert("labels".into(), json!(labels.len()));
    out.insert("label".into(), Value::Null);
    out.insert("geometry_only".into(), json!(true));
    if let Some(lab) = labels.first() {
        let b = lab.bounds.unwrap_or_default();
        let mid = f64::from(b.y) + f64::from(b.height) / 2.0;
        let mut near: Vec<&Element> = rows
            .iter()
            .copied()
            .filter(|e| {
                let eb = e.bounds.unwrap_or_default();
                !std::ptr::eq(*e, *lab)
                    && !e.name.trim().is_empty()
                    && !e.password
                    && !fullmatch(r"[\s*•●∙·]+", &e.name, false)
                    && (f64::from(eb.y) + f64::from(eb.height) / 2.0 - mid).abs() <= f64::from(b.height)
                    && eb.x + eb.width > b.x + b.width
            })
            .collect();
        near.sort_by_key(|e| e.bounds.map(|b| (b.x, b.y)).unwrap_or_default());
        out.insert("label".into(), if lab.password { Value::Null } else { shown(lab) });
        out.insert("candidates".into(), near.iter().take(FILENAME_ROWS).map(|e| shown(e)).collect());
        out.insert("candidates_dropped".into(), json!(near.len().saturating_sub(FILENAME_ROWS)));
    }
    Ok(Some(Value::Object(out)))
}

impl Runner<'_> {
    pub(super) fn abandon(&mut self, task: &Task, reason: &str) -> Result<()> {
        let t = &task.template.task;
        self.log.write(json!({"event": "ladder", "step": 3, "action": task.template.on_abandon, "reason": reason, "host_ms": now_ms(), "task": t}))?;
        let apps: Vec<String> = task.template.processes.iter().map(|p| p.to_lowercase()).collect();
        let prof = profile(&task.template.app)?;
        let words = &prof.dialogs;
        let close = prof.keys.get("close").cloned().unwrap_or_else(|| "Alt+F4".into());
        let discard_rx = words.discard.join("|");
        let leave_rx = words.leave.join("|");
        let mut keyed: Option<Fingerprint> = None;
        for _ in 0..6 {
            let snap = self.observe(5000)?;
            let fgi = crate::tasks::foreground(&snap).map(|i| i as u32);
            let fg = foreground(&snap);
            let mut discard = snap.elements.iter().find(|e| fullmatch(&discard_rx, &e.name.replace('&', ""), true));
            if let Some(d) = discard
                && d.role == "radio button"
                && d.selected
            {
                discard = snap.elements.iter().find(|e| e.window == d.window && e.role == "button" && e.name.replace('&', "") == words.choice_ok);
            }
            if discard.is_none() && keyed.as_ref() == Some(&fingerprint(&snap)) {
                discard = snap.elements.iter().find(|e| Some(e.window) == fgi && e.role == "button" && fullmatch(&leave_rx, &e.name.replace('&', ""), true));
                if discard.is_none() {
                    break;
                }
            }
            keyed = None;
            if let Some(d) = discard.cloned() {
                self.move_to(&d)?;
                self.agent.click(1)?;
            } else if let Some(w) = fg
                && apps.contains(&w.process_name.to_lowercase())
                && alt_f4_safe(Some(w))
            {
                // fg is the FOREGROUND window, so this is always the app's close key (Python's Esc branch is dead).
                self.agent.chord(chord(&close)?)?;
                keyed = Some(fingerprint(&snap));
            } else {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(300));
        }
        self.log.write(json!({"event": "ladder", "step": 4, "action": "restart app (operator)", "host_ms": now_ms(), "task": t}))?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(super) fn log_step(
        &self,
        task: &mut Task,
        sg: &str,
        snap: &Snapshot,
        o: &Opt,
        d: &Decision,
        result: &str,
        step: i64,
        host_ms: i64,
        options: &[Opt],
        check: Option<(&Subgoal, bool)>,
        code_done: Option<bool>,
        image: Option<&'static str>,
    ) -> Result<()> {
        let fg = foreground(snap);
        let pixels = fg.is_some_and(pixels_window);
        let mut r = Map::new();
        r.insert("event".into(), json!("step"));
        r.insert("host_ms".into(), json!(if host_ms != 0 { host_ms } else { now_ms() }));
        r.insert("guest_ms".into(), json!(snap.guest_time_ms));
        r.insert("persona".into(), json!(self.persona.user));
        r.insert("vm".into(), json!(self.agent.vm()));
        r.insert("task".into(), json!(task.template.task));
        r.insert("subgoal".into(), json!(sg));
        r.insert("step".into(), json!(step));
        r.insert("window".into(), json!(fg.map_or("", |w| w.title.as_str())));
        r.insert("process".into(), json!(fg.map_or("", |w| w.process_name.as_str())));
        if pixels {
            r.insert("trust".into(), json!("pixels"));
            if !options.is_empty() {
                let mut offered = Map::new();
                offered.insert("clicks".into(), json!(options.iter().filter(|x| x.kind == "click").count()));
                for x in options.iter().filter(|x| x.kind != "click") {
                    offered.insert(x.key.clone(), json!(x.kind));
                }
                r.insert("offered".into(), Value::Object(offered));
            }
            if let Some((s, typed_ok)) = check
                && let Some(f) = filename_diag(snap, task, s, typed_ok)?
            {
                r.insert("filename".into(), f);
            }
        }
        r.insert("element".into(), o.element.as_ref().map_or(Value::Null, |e| json!({"id": e.id, "key": e.key, "role": e.role, "name": e.name})));
        r.insert("action".into(), json!(o.kind));
        r.insert("option".into(), json!(o.text));
        r.insert(
            "decision".into(),
            json!({"choice": d.choice, "confidence": d.confidence, "top": d.top(3), "subgoal_done": d.subgoal_done, "off_track": d.off_track,
                   "cost": d.cost, "model": d.model, "image": image, "latency_ms": d.latency_ms, "retries_429": d.retries_429}),
        );
        r.insert("code_done".into(), json!(code_done));
        r.insert("result".into(), json!(result));
        self.log.write(Value::Object(r))
    }
}
