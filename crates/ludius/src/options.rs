//! What the persona sees and may do on one step: the filtered screen, the state for the Decider, the options.
use std::collections::{BTreeMap, BTreeSet, HashSet};

use anyhow::Result;
use ludius_proto::v1::{Element, Snapshot, Window, window::Kind};
use serde_json::{Value, json};

use crate::decisions::MAX_OPTIONS;
use crate::policy::{Policy, SHELL_PROCESSES, alt_f4_safe};
use crate::tasks::{LAUNCH_TEXT, Subgoal, Task, declared_commits, element_matches, guard_matches, pixels_region, pixels_window};
use crate::template::search;

const CLICKABLE: [&str; 16] = [
    "button", "split button", "menu item", "list item", "tab item", "hyperlink", "check box", "radio button", "combo box", "tree item",
    "data item", "edit", "document", "header item", "spinner", "slider",
];
const EDITABLE: [&str; 3] = ["edit", "document", "combo box"];
const OPENABLE: [&str; 2] = ["list item", "data item"];
const BASE_KEYS: [&str; 9] = ["Enter", "Esc", "Tab", "Shift+Tab", "Up", "Down", "Left", "Right", "Alt+F4"];
pub const MAX_ELEMENT_LINES: usize = 120;
const COPY_SOURCES: [&str; 6] = ["text", "hyperlink", "list item", "data item", "heading", "tree item"];
const MAX_COPY: usize = 12;
pub const LAUNCH_PROCESSES: [&str; 3] = ["searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe"];
pub const REASON_OTHER_FIELD: &str = "that field belongs to another step of this task (e.g. the address bar): click into the page first";

#[derive(Debug, Clone, Default)]
pub struct Opt {
    pub key: String,
    pub kind: String,
    pub text: String,
    pub element: Option<Element>,
    pub keys: String,
    pub content: String,
    pub literal: String,
    pub notches: i32,
}

#[derive(Debug, Clone)]
pub struct StepView {
    pub snapshot: Snapshot,
    pub allowed: Vec<Element>,
    pub windows: BTreeMap<u32, Window>,
}

fn is(w: &Window, k: Kind) -> bool {
    w.kind == k as i32
}

pub fn fg(snap: &Snapshot) -> Option<&Window> {
    snap.windows.iter().find(|w| is(w, Kind::Foreground))
}

fn win<'a>(snap: &'a Snapshot, e: &Element) -> Option<&'a Window> {
    snap.windows.get(e.window as usize)
}

fn has_area(e: &Element) -> bool {
    e.bounds.is_some_and(|b| b.width > 0)
}

pub fn describe(e: &Element) -> String {
    let mut s = format!("[{}] {} '{}'", e.id, e.role, e.name);
    if !e.value.is_empty() {
        s += &format!(" value='{}'", e.value.chars().take(80).collect::<String>());
    }
    let flags: Vec<&str> = [("focused", e.focused), ("selected", e.selected), ("expanded", e.expanded), ("read-only", e.read_only), ("disabled", !e.enabled)]
        .iter()
        .filter(|(_, on)| *on)
        .map(|(f, _)| *f)
        .collect();
    if !flags.is_empty() {
        s += &format!(" {}", flags.join(","));
    }
    if e.framework_id == "visual" {
        let b = e.bounds.unwrap_or_default();
        s += &format!(" at {},{} {}x{}", b.x, b.y, b.width, b.height);
    }
    if !e.hints.is_empty() {
        s += &format!(" hint={}", e.hints.join("; "));
    }
    s
}

pub fn window_line(w: &Window) -> String {
    format!("{} ({})", if w.title.is_empty() { &w.class_name } else { &w.title }, w.process_name)
}

fn plain(s: &str) -> String {
    s.replace('\u{200b}', "").trim().to_lowercase()
}

fn apps(task: &Task) -> HashSet<String> {
    task.template.processes.iter().map(|p| p.to_lowercase()).collect()
}

pub fn view(snap: &Snapshot, task: &Task, sg: &Subgoal, policy: &Policy) -> StepView {
    let mut v = StepView { snapshot: snap.clone(), allowed: vec![], windows: BTreeMap::new() };
    let mut region = HashSet::new();
    let apps = apps(task);
    let owner_is_app = fg(snap).is_some_and(|w| apps.contains(&w.process_name.to_lowercase()));
    for (i, w) in snap.windows.iter().enumerate() {
        if pixels_window(w) {
            let rows = pixels_region(snap, i as u32, task);
            if !rows.is_empty() {
                v.windows.insert(i as u32, w.clone());
                region.extend(rows.iter().map(|e| e.id));
            }
            continue;
        }
        let proc = w.process_name.to_lowercase();
        let dialog_of_app = is(w, Kind::Owned) || is(w, Kind::Popup);
        let shell = is(w, Kind::Shell) || SHELL_PROCESSES.contains(&proc.as_str());
        if apps.contains(&proc) || (dialog_of_app && owner_is_app) || (sg.launch && shell) {
            v.windows.insert(i as u32, w.clone());
        }
    }
    let mut hidden = BTreeSet::new();
    if !sg.launch && fg(snap).is_none_or(|w| SHELL_PROCESSES.contains(&w.process_name.to_lowercase().as_str())) {
        hidden = snap
            .apps
            .iter()
            .filter(|a| apps.contains(&a.process_name.to_lowercase()))
            .map(|a| plain(a.title.rsplit(" - ").next().unwrap_or("")))
            .filter(|h| !h.is_empty())
            .collect();
    }
    let scope = v.windows.clone();
    for e in &snap.elements {
        let in_region = scope.get(&e.window).is_some_and(|w| region.contains(&e.id) || !pixels_window(w));
        let w = win(snap, e);
        let button = !hidden.is_empty()
            && w.is_some_and(|w| e.role == "button" && (w.class_name == "Shell_TrayWnd" || w.class_name == "Shell_SecondaryTrayWnd"))
            && hidden.iter().any(|h| plain(&e.name).contains(h.as_str()));
        if (in_region || button) && policy.element_allowed(e, &sg.allow_destructive, Some(snap)).is_none() {
            if button && let Some(w) = w {
                v.windows.insert(e.window, w.clone());
            }
            v.allowed.push(e.clone());
        }
    }
    v
}

pub fn build_state(v: &StepView, task: &Task, sg: &Subgoal, history: &[String], last_result: &str) -> Result<Value> {
    let snap = &v.snapshot;
    let fg = fg(snap);
    let focused = snap.elements.iter().find(|e| e.focused);
    let on_pixels = fg.is_some_and(pixels_window);
    let goal = if on_pixels && !sg.pixels_hint.is_empty() { format!("{} {}", sg.goal, task.fmt(&sg.pixels_hint)?) } else { sg.goal.clone() };
    let context = if task.inst.context.is_empty() { task.fmt(&task.template.task.replace('_', " "))? } else { task.inst.context.clone() };
    let focus = if on_pixels {
        "unknown (read from pixels)".to_string()
    } else {
        focused.map(|e| format!("[{}]", e.id)).unwrap_or("none".into())
    };
    let mut state = json!({
        "goal": goal,
        "task_context": context,
        "window": fg.map(window_line).unwrap_or("desktop".into()),
        "other_windows": snap.windows.iter().filter(|w| is(w, Kind::Owned) || is(w, Kind::Popup)).map(window_line).collect::<Vec<_>>(),
        "focused": focus,
        "history": history[history.len().saturating_sub(8)..],
        "last_result": last_result,
        "elements": v.allowed.iter().take(MAX_ELEMENT_LINES).map(describe).collect::<Vec<_>>(),
        "context": snap.context.iter().take(30).collect::<Vec<_>>(),
    });
    if let Some(c) = task.text(sg) {
        let shown = if c.chars().count() <= 160 { c } else { format!("{}…", c.chars().take(160).collect::<String>()) };
        state["text_to_enter"] = json!(shown);
    }
    if snap.busy_cursor || snap.indeterminate_progress {
        state["busy"] = json!(true);
    }
    Ok(state)
}

pub fn search_target(e: Option<&Element>, snap: &Snapshot, task: &Task) -> bool {
    let Some(e) = e.filter(|e| e.role == "edit" && !e.read_only && e.enabled && !e.password) else { return false };
    win(snap, e).is_some_and(|w| is(w, Kind::Foreground) && apps(task).contains(&w.process_name.to_lowercase()) && !pixels_window(w))
}

pub fn launch_target(e: Option<&Element>, snap: &Snapshot) -> bool {
    let Some(e) = e.filter(|e| e.role == "edit" && !e.read_only && e.enabled && !e.password) else { return false };
    let Some(w) = win(snap, e).filter(|w| !pixels_window(w)) else { return false };
    let proc = w.process_name.to_lowercase();
    let run = proc == "explorer.exe" && w.class_name == "#32770" && w.title == "Run";
    LAUNCH_PROCESSES.contains(&proc.as_str()) || (run && e.name == "Open:")
}

/// A Start search result that leads to the Store or the web: off the range, never the app asked for.
pub fn off_range_result(e: &Element, snap: &Snapshot) -> bool {
    win(snap, e).is_some_and(|w| LAUNCH_PROCESSES.contains(&w.process_name.to_lowercase().as_str()))
        && search(r"\bstore\b|\bsearch the web\b|\bweb results?\b", &e.name, true)
}

pub fn field_of_another_step(e: Option<&Element>, task: &Task, sg: &Subgoal) -> Result<bool> {
    let Some(e) = e else { return Ok(false) };
    if !sg.field.is_empty() && search(&task.fmt_re(&sg.field)?, &e.name, false) {
        return Ok(false);
    }
    for s in task.template.subgoals.values() {
        if !s.field.is_empty() && search(&task.fmt_re(&s.field)?, &e.name, false) {
            return Ok(true);
        }
    }
    Ok(false)
}

pub fn commits_unguarded(k: &str, focused: Option<&Element>, task: &Task, sg: &Subgoal) -> Result<bool> {
    let mut commits = false;
    for c in &task.template.key_commits {
        if c.keys.to_lowercase() == k.to_lowercase() && focused.map(|f| element_matches(f, &c.focus, task)).transpose()?.unwrap_or(true) {
            commits = true;
            break;
        }
    }
    if !commits {
        return Ok(false);
    }
    let Some(f) = focused.filter(|_| !sg.require.is_empty()) else { return Ok(true) };
    for c in &sg.require {
        if guard_matches(f, c, task)? {
            return Ok(false);
        }
    }
    Ok(true)
}

fn copyable(t: &str) -> bool {
    (3..=200).contains(&t.chars().count()) && t.chars().all(|c| (' '..='~').contains(&c))
}

pub fn build_options(v: &StepView, task: &Task, sg: &Subgoal, policy: &Policy) -> Result<Vec<Opt>> {
    let mut opts: Vec<Opt> = Vec::new();
    let add = |opts: &mut Vec<Opt>, o: Opt| {
        let key = format!("o{}", opts.len() + 1);
        opts.push(Opt { key, ..o });
    };
    let snap = &v.snapshot;
    let content = task.text(sg);
    let sg_text = sg.text.clone().unwrap_or_default();
    let literal = if sg_text.is_empty() { sg.search.clone() } else { String::new() };
    for e in &v.allowed {
        let mut avoided = off_range_result(e, snap);
        for c in &sg.avoid {
            avoided = avoided || element_matches(e, c, task)?;
        }
        if avoided || (content.is_none() && !sg.copy_text && e.role == "edit") {
            continue;
        }
        if (CLICKABLE.contains(&e.role.as_str()) || (e.role == "text" && e.framework_id == "visual")) && has_area(e) {
            add(&mut opts, Opt { kind: "click".into(), text: format!("click {}", describe(e)), element: Some(e.clone()), ..Default::default() });
            if OPENABLE.contains(&e.role.as_str()) {
                let text = format!("double-click to open [{}] {} '{}'", e.id, e.role, e.name);
                add(&mut opts, Opt { kind: "double_click".into(), text, element: Some(e.clone()), ..Default::default() });
            }
        }
        if let Some(c) = &content
            && EDITABLE.contains(&e.role.as_str())
            && !e.read_only
            && e.enabled
            && policy.text_allowed(c, Some(e)).is_none()
            && (!sg_text.is_empty() || search_target(Some(e), snap, task))
            && (sg_text != LAUNCH_TEXT || launch_target(Some(e), snap))
            && !field_of_another_step(Some(e), task, sg)?
        {
            let text = format!("type the text to enter into [{}] {} '{}'", e.id, e.role, e.name);
            add(&mut opts, Opt { kind: "type".into(), text, element: Some(e.clone()), content: sg_text.clone(), literal: literal.clone(), ..Default::default() });
        }
    }
    let elements = opts.len();
    let focused = snap.elements.iter().find(|e| e.focused);
    if !sg_text.is_empty()
        && content.as_deref().is_some_and(|c| policy.text_allowed(c, None).is_none())
        && !field_of_another_step(focused, task, sg)?
        && (sg_text != LAUNCH_TEXT || launch_target(focused, snap))
    {
        let text = "type the text to enter at the keyboard focus".into();
        add(&mut opts, Opt { kind: "type".into(), text, content: sg_text.clone(), literal: literal.clone(), ..Default::default() });
    }
    let focus = v.allowed.iter().find(|e| e.focused && EDITABLE.contains(&e.role.as_str()) && !e.read_only);
    if let Some(focus) = focus.filter(|_| sg.copy_text) {
        let mut sources: Vec<(String, String)> =
            v.allowed.iter().filter(|e| COPY_SOURCES.contains(&e.role.as_str()) && e.key != focus.key).map(|e| (format!("[{}] ", e.id), e.name.clone())).collect();
        sources.extend(snap.context.iter().map(|l| (String::new(), l.split_once(": ").map_or(l.as_str(), |x| x.1).to_string())));
        let mut seen = HashSet::new();
        for (r, t) in sources {
            if seen.len() >= MAX_COPY || seen.contains(&t) || !copyable(&t) || policy.text_allowed(&t, Some(focus)).is_some() {
                continue;
            }
            let text = format!("type:copy {r}'{}' into [{}]", t.chars().take(60).collect::<String>(), focus.id);
            seen.insert(t.clone());
            add(&mut opts, Opt { kind: "type".into(), text, element: Some(focus.clone()), literal: t, ..Default::default() });
        }
    }
    let fgw = fg(snap);
    let declared = declared_commits(task, sg);
    for k in BASE_KEYS.iter().map(|k| k.to_string()).chain(sg.keys.iter().cloned()) {
        if (k == "Win" && !sg.launch) || (k == "Alt+F4" && !alt_f4_safe(fgw)) {
            continue;
        }
        if policy.key_allowed(&k, snap, &declared).is_some() || commits_unguarded(&k, focused, task, sg)? {
            continue;
        }
        add(&mut opts, Opt { kind: "key".into(), text: format!("press {k}"), keys: k, ..Default::default() });
    }
    if v.allowed.iter().any(|e| e.hints.iter().any(|h| h.contains("list continues")) || e.role == "document") {
        add(&mut opts, Opt { kind: "scroll".into(), text: "scroll down".into(), notches: -3, ..Default::default() });
        add(&mut opts, Opt { kind: "scroll".into(), text: "scroll up".into(), notches: 3, ..Default::default() });
    }
    add(&mut opts, Opt { kind: "rescan".into(), text: "rescan: wait for the screen to finish loading and look again".into(), ..Default::default() });
    if opts.len() > MAX_OPTIONS {
        // Drop element options from the end of the element block; keys, scroll and rescan always stay.
        let keep = elements.saturating_sub(opts.len() - MAX_OPTIONS);
        opts.drain(keep..elements);
        for (i, o) in opts.iter_mut().enumerate() {
            o.key = format!("o{}", i + 1);
        }
    }
    Ok(opts)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ludius_proto::v1::Rect;

    #[test]
    fn describes_and_dumps() {
        let e = Element { id: 7, role: "button".into(), name: "Save".into(), focused: true, enabled: true, bounds: Some(Rect { x: 1, y: 2, width: 3, height: 4 }), ..Default::default() };
        assert_eq!(describe(&e), "[7] button 'Save' focused");
        assert_eq!(crate::assist::dumps(&json!({"a": [1], "b": "å"})), "{\n \"a\": [\n  1\n ],\n \"b\": \"å\"\n}");
        assert!(copyable("abc") && !copyable("ab") && !copyable("abc\u{e5}"));
    }
}
