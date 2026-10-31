//! On-screen done checks and preconditions.
use super::*;

pub(crate) fn is_kind(w: &Window, k: Kind) -> bool {
    w.kind == k as i32
}

pub fn foreground(snap: &Snapshot) -> Option<usize> {
    snap.windows.iter().position(|w| is_kind(w, Kind::Foreground))
}

pub fn window_matches(w: &Window, c: &WindowCheck, task: &Task) -> Result<bool> {
    if let Some(p) = c.process.as_deref().filter(|p| !p.is_empty())
        && w.process_name.to_lowercase() != p.to_lowercase()
    {
        return Ok(false);
    }
    if let Some(cl) = c.class_name.as_deref().filter(|p| !p.is_empty())
        && w.class_name != cl
    {
        return Ok(false);
    }
    Ok(match c.title.as_deref().filter(|t| !t.is_empty()) {
        Some(t) => search(&task.fmt_re(t)?, &w.title, true),
        None => true,
    })
}

pub fn pixels_window(w: &Window) -> bool {
    w.framework_id == "visual" && w.process_name.is_empty()
}

pub fn compact(s: &str) -> String {
    s.split_whitespace().collect()
}

pub(crate) fn visible(pattern: &str, rows: &[&Element], task: &Task) -> bool {
    let rx = task.pixels_re(pattern);
    rows.iter().any(|e| search(&rx, &compact(&e.name), true))
}

pub const TASKBAR: &str = "ypeheretosearch";

pub fn pixels_region<'a>(snap: &'a Snapshot, window: u32, task: &Task) -> Vec<&'a Element> {
    let Some(px) = &task.template.pixels else { return vec![] };
    let rows: Vec<&Element> = snap.elements.iter().filter(|e| e.window == window).collect();
    if !(visible(&px.title, &rows, task) && px.anchors.iter().all(|a| visible(a, &rows, task))) {
        return vec![];
    }
    let rx = task.pixels_re(&px.title);
    let b = |e: &Element| e.bounds.unwrap_or_default();
    let Some(title) = rows.iter().filter(|e| search(&rx, &compact(&e.name), true)).min_by_key(|e| b(e).y) else { return vec![] };
    let tb = b(title);
    let bar: Vec<i32> = rows.iter().filter(|e| search(TASKBAR, &compact(&e.name), true)).map(|e| b(e).y).collect();
    let dpi = if snap.dpi == 0 { 96 } else { snap.dpi };
    let bottom = bar.iter().min().map(|y| f64::from(y - 4)).unwrap_or(f64::from(snap.screen_height - 48 * dpi / 96));
    rows.into_iter()
        .filter(|e| {
            let r = b(e);
            f64::from(r.x) + f64::from(r.width) / 2.0 >= f64::from(tb.x - 48)
                && f64::from(tb.y - 12) <= f64::from(r.y) + f64::from(r.height) / 2.0
                && f64::from(r.y) + f64::from(r.height) / 2.0 < bottom
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
pub fn check_done(
    done: Option<&Done>,
    snap: &Snapshot,
    task: &mut Task,
    typed_ok: bool,
    outside: bool,
    pressed_ok: bool,
    clicked_ok: bool,
    last_click: Option<&Element>,
) -> Result<Option<bool>> {
    let Some(done) = done.filter(|d| d.ask.is_none()) else { return Ok(None) };
    if done.outside() && !done.on_screen() {
        return Ok(if outside { Some(outside_ok(done, task)?) } else { None });
    }
    let on_pixels = snap.windows.iter().any(pixels_window);
    if done.pixels.is_some() && !on_pixels && !(done.window.is_some() || done.foreground.is_some() || done.element.is_some() || done.added.is_some() || done.typed) {
        return Ok(None);
    }
    if (done.typed && !typed_ok) || (done.pressed && !pressed_ok) || (done.clicked && !clicked_ok) {
        return Ok(Some(false));
    }
    if let Some(c) = &done.clicked_on
        && !last_click.map(|e| element_matches(e, c, task)).transpose()?.unwrap_or(false)
    {
        return Ok(Some(false));
    }
    if on_pixels && done.pixels.is_none() {
        return Ok(Some(false));
    }
    if let Some(px) = done.pixels.as_ref().filter(|_| on_pixels) {
        let rows: Vec<&Element> =
            snap.elements.iter().filter(|e| snap.windows.get(e.window as usize).is_some_and(pixels_window)).collect();
        let seen = px.present.iter().all(|p| visible(p, &rows, task)) && !px.absent.iter().any(|p| visible(p, &rows, task));
        return Ok(Some(seen && (!outside || outside_ok(done, task)?)));
    }
    if (done.window.is_some() || done.foreground.is_some()) && on_pixels {
        return Ok(Some(false));
    }
    if let Some(c) = &done.window {
        let mut hit = false;
        for w in snap.windows.iter().chain(&snap.apps) {
            if window_matches(w, c, task)? {
                hit = true;
                break;
            }
        }
        if hit == c.absent {
            return Ok(Some(false));
        }
    }
    if let Some(c) = &done.foreground {
        let fg = snap.windows.iter().find(|w| is_kind(w, Kind::Foreground));
        let hit = match fg {
            Some(w) => window_matches(w, c, task)?,
            None => false,
        };
        if hit == c.absent {
            return Ok(Some(false));
        }
    }
    if let Some(c) = &done.element {
        let candidates: Vec<&Element> = match &c.at {
            None => snap.elements.iter().collect(),
            Some(at) => marked(at, snap, task)?,
        };
        let mut any = false;
        for e in candidates {
            if element_matches(e, c, task)? {
                any = true;
                break;
            }
        }
        if !any {
            return Ok(Some(false));
        }
    }
    if let Some(a) = &done.added
        && !added_ok(a, snap, task)?
    {
        return Ok(Some(false));
    }
    Ok(Some(!outside || outside_ok(done, task)?))
}

pub(crate) fn items<'a>(c: &ElementCheck, snap: &'a Snapshot, task: &Task) -> Result<Vec<&'a Element>> {
    let mut v = Vec::new();
    for e in &snap.elements {
        if element_matches(e, c, task)? {
            v.push(e);
        }
    }
    v.sort_by_key(|e| {
        let b = e.bounds.unwrap_or_default();
        (b.y, b.x)
    });
    Ok(v)
}

pub fn added_baseline(done: Option<&Done>, snap: &Snapshot, task: &mut Task) -> Result<()> {
    task.added_base = match done.and_then(|d| d.added.as_ref()) {
        Some(a) => Some(items(&a.items, snap, task)?.len()),
        None => None,
    };
    Ok(())
}

pub(crate) fn added_ok(c: &AddedCheck, snap: &Snapshot, task: &mut Task) -> Result<bool> {
    let found = items(&c.items, snap, task)?;
    let picked: Vec<usize> = found.iter().enumerate().filter(|(_, e)| e.selected).map(|(i, _)| i).collect();
    if task.added_base.is_none_or(|b| found.len() <= b) || picked.len() != 1 {
        return Ok(false);
    }
    if !element_matches(found[picked[0]], &c.new, task)? {
        return Ok(false);
    }
    task.marks.insert(c.mark.clone(), (c.items.clone(), picked[0]));
    Ok(true)
}

pub(crate) fn marked<'a>(mark: &str, snap: &'a Snapshot, task: &Task) -> Result<Vec<&'a Element>> {
    let Some((c, at)) = task.marks.get(mark) else { return Ok(vec![]) };
    Ok(items(c, snap, task)?.into_iter().skip(*at).take(1).collect())
}

pub fn element_matches(e: &Element, c: &ElementCheck, task: &Task) -> Result<bool> {
    let value = e.value.replace("\r\n", "\n").replace('\r', "\n");
    if c.role.as_deref().is_some_and(|r| !r.is_empty() && e.role != r) {
        return Ok(false);
    }
    if let Some(n) = c.name.as_deref().filter(|n| !n.is_empty())
        && !search(&task.fmt_re(n)?, &e.name, true)
    {
        return Ok(false);
    }
    if let Some(v) = c.value.as_deref().filter(|v| !v.is_empty())
        && !search(&task.fmt_re(v)?, &value, true)
    {
        return Ok(false);
    }
    Ok(c.focused.is_none_or(|f| e.focused == f) && c.selected.is_none_or(|s| e.selected == s))
}

/// A guard on a field value: only ASCII values the UI exposes as a value pattern.
pub fn guard_matches(e: &Element, c: &ElementCheck, task: &Task) -> Result<bool> {
    Ok(e.actions.iter().any(|a| a == "value") && e.value.is_ascii() && element_matches(e, c, task)?)
}

pub fn preconditions(sg: &Subgoal, snap: &Snapshot, task: &Task, target: Option<&Element>) -> Result<Option<String>> {
    let fg = foreground(snap);
    if (!sg.require.is_empty() || sg.in_window.is_some()) && target.is_some_and(|t| Some(t.window as usize) != fg) {
        return Ok(Some("precondition: the target is not in the foreground window".into()));
    }
    if let Some(iw) = &sg.in_window {
        let ok = match fg {
            Some(i) => window_matches(&snap.windows[i], iw, task)?,
            None => false,
        };
        if !ok {
            let want = iw.title.clone().or(iw.process.clone()).unwrap_or_default();
            return Ok(Some(format!("precondition: the foreground window is not {:?}", task.fmt(&want)?)));
        }
    }
    if !sg.breadcrumb.is_empty() {
        let got = explorer_crumbs(snap, fg);
        let ok = match &got {
            Some(g) if g.len() == sg.breadcrumb.len() => {
                let mut all = true;
                for (c, x) in sg.breadcrumb.iter().zip(g) {
                    all &= fullmatch(&task.fmt_re(c)?, x, false);
                }
                all
            }
            _ => false,
        };
        if !ok {
            let shown = got.unwrap_or_else(|| vec!["unknown".into()]).join(" > ");
            return Ok(Some(format!("precondition: the folder is {shown:?}, not {:?}", sg.breadcrumb.join(" > "))));
        }
    }
    let in_fg = |e: &&Element| Some(e.window as usize) == fg;
    for c in &sg.require {
        let mut hit = false;
        for e in snap.elements.iter().filter(in_fg) {
            if if c.value.is_some() { guard_matches(e, c, task)? } else { element_matches(e, c, task)? } {
                hit = true;
                break;
            }
        }
        if !hit {
            let loose = ElementCheck { value: None, ..c.clone() };
            let mut seen = Vec::new();
            for e in snap.elements.iter().filter(in_fg) {
                if element_matches(e, &loose, task)? {
                    seen.push(e.value.chars().take(80).collect::<String>());
                }
            }
            let where_ = fg.map(|i| &snap.windows[i]).map(|w| format!("{:?} {} no_tree={}", w.title, w.class_name, w.no_tree)).unwrap_or("none".into());
            let what = task.fmt(c.name.as_deref().or(c.role.as_deref()).unwrap_or(""))?;
            return Ok(Some(format!(
                "precondition: no readable {what:?} field with value {:?} (read: {seen:?}; foreground: {where_})",
                task.fmt(c.value.as_deref().unwrap_or(""))?
            )));
        }
    }
    for c in &sg.forbid {
        for e in &snap.elements {
            if element_matches(e, c, task)? {
                let what = c.name.clone().or(c.role.clone()).unwrap_or_default();
                return Ok(Some(format!("precondition: {what:?} must not match {:?}", task.fmt(c.value.as_deref().unwrap_or(""))?)));
            }
        }
    }
    Ok(None)
}

pub fn explorer_crumbs(snap: &Snapshot, fg: Option<usize>) -> Option<Vec<String>> {
    let els: Vec<&Element> = snap.elements.iter().filter(|e| Some(e.window as usize) == fg).collect();
    let start = els.iter().position(|e| e.role == "split button" && e.name == "All locations")?;
    let mut crumbs = Vec::new();
    for e in &els[start + 1..] {
        if e.role == "button" && e.name.starts_with("Refresh") {
            return Some(crumbs);
        }
        if e.role == "button" && e.name == "Previous Locations" {
            continue;
        }
        if e.role != "split button" && e.role != "menu item" {
            return None;
        }
        crumbs.push(e.name.clone());
    }
    None
}
