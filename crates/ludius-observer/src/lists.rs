//! Platform-free parts of the snapshot: geometry, list viewports, text trimming, keys and hints.
use ludius_proto::v1::{Element, Rect, window::Kind};
use regex::Regex;
use sha1::{Digest, Sha1};
use std::sync::LazyLock;

/// Screen rectangle (x, y, width, height).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct R {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

impl R {
    pub const fn new(x: i32, y: i32, w: i32, h: i32) -> R {
        R { x, y, w, h }
    }
    pub fn proto(self) -> Rect {
        Rect { x: self.x, y: self.y, width: self.w, height: self.h }
    }
}

pub fn clip(a: R, b: R) -> R {
    let (x1, y1) = (a.x.max(b.x), a.y.max(b.y));
    let (x2, y2) = ((a.x + a.w).min(b.x + b.w), (a.y + a.h).min(b.y + b.h));
    if x2 > x1 && y2 > y1 { R::new(x1, y1, x2 - x1, y2 - y1) } else { R::default() }
}

/// The part of a list below its column header; `None` when the list has no bounds.
pub fn content_viewport(list: R, kids: &[(String, R)]) -> Option<R> {
    if list.w <= 0 || list.h <= 0 {
        return None;
    }
    let (mut top, bottom) = (list.y, list.y + list.h);
    for (role, b) in kids {
        let h = clip(*b, list);
        if role == "header" && h.h > 0 && 2 * h.y + h.h < 2 * list.y + list.h {
            top = top.max(h.y + h.h);
        }
    }
    Some(if top < bottom { R::new(list.x, top, list.w, bottom - top) } else { R::default() })
}

/// A row is a target only if at least half of it (or 16 px) shows in the viewport.
pub fn visible_row(row: R, content: R) -> R {
    let v = clip(row, content);
    if v.h * 2 >= row.h || v.h >= 16 { v } else { R::default() }
}

pub const ROWS: &[&str] = &["list item", "tree item", "data item"];

pub fn visible_rows(kids: &[(String, R)], content: R) -> usize {
    kids.iter().filter(|(role, b)| ROWS.contains(&role.as_str()) && visible_row(*b, content).w > 0).count()
}

pub fn list_shown(visible: usize, pct: f64) -> String {
    match visible {
        0 => "list continues".into(),
        n if pct == 0.0 => format!("items 1–{n} shown, list continues"),
        n => format!("{n} items shown, list continues"),
    }
}

pub fn trim(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        Some((i, _)) => format!("{}…", &s[..i]),
        None => s.to_string(),
    }
}

/// The first 8 bytes of SHA-1, lower-case hex.
pub fn hash(s: &str) -> String {
    Sha1::digest(s.as_bytes())[..8].iter().map(|b| format!("{b:02x}")).collect()
}

/// UIA control type id (50000..) to role, the UIA_*ControlTypeId names split into lower-case words.
pub fn role_name(control_type: i32) -> Option<&'static str> {
    const NAMES: &[&str] = &[
        "button", "calendar", "check box", "combo box", "edit", "hyperlink", "image", "list item", "list", "menu",
        "menu bar", "menu item", "progress bar", "radio button", "scroll bar", "slider", "spinner", "status bar",
        "tab", "tab item", "text", "tool bar", "tool tip", "tree", "tree item", "custom", "group", "thumb",
        "data grid", "data item", "document", "split button", "window", "pane", "header", "header item", "table",
        "title bar", "separator", "semantic zoom", "app bar",
    ];
    usize::try_from(control_type - 50000).ok().and_then(|i| NAMES.get(i)).copied()
}

pub const INTERACTIVE: &[&str] = &[
    "button", "check box", "combo box", "edit", "hyperlink", "list item", "menu item", "radio button", "slider",
    "spinner", "split button", "tab item", "tree item", "data item", "document", "menu bar", "scroll bar",
];
pub const LISTS: &[&str] = &["list", "data grid", "tree", "table"];

static DESTRUCTIVE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)\b(delete|remove|discard|send|pay|uninstall|format|don't save|empty|erase)\b").unwrap());
static SUBMITS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?i)^(ok|save|yes|submit|send|apply|open|print|finish|next|continue|install|replace|retry)$").unwrap());
static CLOSES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(?i)^(cancel|close|no|don't save|not now|skip)$").unwrap());

/// Hints shared by UIA and Java elements.
pub fn common_hints(e: &mut Element, kind: Kind, window_class: &str) {
    let label = e.name.replace('&', "");
    let label = label.trim();
    if e.actions.iter().any(|a| a == "expand") && e.role != "tree item" {
        e.hints.push("opens list/menu".into());
    }
    if e.role == "hyperlink" {
        e.hints.push(if e.value.is_empty() { "link".into() } else { format!("link to {}", trim(&e.value, 120)) });
    }
    let dialog = kind == Kind::Owned || window_class == "#32770" || window_class == "SunAwtDialog";
    if dialog && (e.role == "button" || e.role == "split button") {
        if SUBMITS.is_match(label) {
            e.hints.push("submits".into());
        } else if CLOSES.is_match(label) {
            e.hints.push("closes dialog".into());
        }
    }
    if DESTRUCTIVE.is_match(label) {
        e.hints.push("destructive".into());
    }
}

/// Java Access Bridge role (en-US) to the observer's role; `None` for containers the walk passes through.
pub fn java_role(role: &str, editable: bool) -> Option<&'static str> {
    Some(match role {
        "push button" | "toggle button" => "button",
        "check box" => "check box",
        "radio button" => "radio button",
        "combo box" => "combo box",
        "list item" => "list item",
        "menu item" | "check box menu item" | "radio button menu item" | "menu" => "menu item",
        "scroll bar" => "scroll bar",
        "slider" => "slider",
        "hyperlink" => "hyperlink",
        "menu bar" => "menu bar",
        "tool bar" => "tool bar",
        "progress bar" => "progress bar",
        "status bar" => "status bar",
        "list" => "list",
        "table" => "table",
        "tree" => "tree",
        "text" if editable => "edit",
        "text" | "label" => "text",
        "password text" => "edit",
        "page tab" => "tab item",
        "spinbox" => "spinner",
        _ => return None,
    })
}

/// "0.##": up to two decimals, no trailing zeros.
pub fn short_number(d: f64) -> String {
    let s = format!("{d:.2}");
    let s = s.trim_end_matches('0').trim_end_matches('.');
    if s == "-0" { "0".into() } else { s.into() }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rows(first_y: i32, count: i32) -> Vec<(String, R)> {
        (0..count).map(|i| ("list item".to_string(), R::new(568, first_y + 21 * i, 272, 22))).collect()
    }

    /// The C# ListSelfTest cases (a scrolled Explorer list under a column header).
    #[test]
    fn list_viewport() {
        let list = R::new(568, 299, 963, 520);
        let header = ("header".to_string(), R::new(568, 299, 272, 25));
        let mut top = vec![header.clone()];
        top.extend(rows(330, 24));
        let content = content_viewport(list, &top).unwrap();
        assert_eq!(content, R::new(568, 324, 963, 495), "content viewport sits below the header");
        assert_eq!(visible_row(R::new(568, 330, 272, 22), content), R::new(568, 330, 272, 22));
        assert_eq!(visible_row(R::new(568, 813, 272, 22), content), R::default(), "6 px showing is not a target");
        assert_eq!(visible_rows(&top, content), 23, "header not counted");
        assert_eq!(list_shown(23, 0.0), "items 1–23 shown, list continues");

        let mut down = vec![header.clone()];
        down.extend(rows(314, 25));
        let boot = visible_row(R::new(568, 314, 272, 22), content);
        assert_eq!(boot, R::new(568, 324, 272, 12), "clipped to the part below the header");
        assert!(boot.y + boot.h / 2 >= 324);
        assert_eq!(visible_row(R::new(568, 818, 272, 22), content), R::default());
        assert_eq!(visible_rows(&down, content), 24);
        assert_eq!(list_shown(24, 0.2), "24 items shown, list continues", "no invented ordinal off the top");
        assert_eq!(list_shown(23, 22.5), "23 items shown, list continues");
        assert_eq!(list_shown(24, f64::NAN), "24 items shown, list continues");
        assert_eq!(list_shown(24, -1.0), "24 items shown, list continues");

        let mut back = vec![header];
        back.extend(rows(303, 25));
        assert_eq!(visible_row(R::new(568, 303, 272, 22), content), R::default(), "under the header");
        assert_eq!(visible_row(R::new(568, 807, 272, 22), content), R::new(568, 807, 272, 12));
        assert_eq!(visible_rows(&back, content), 24);

        assert_eq!(content_viewport(list, &rows(330, 24)), Some(list), "no header, whole list");
        assert_eq!(content_viewport(R::default(), &top), None, "unknown bounds, unknown viewport");
        assert_eq!(list_shown(0, 0.0), "list continues");
    }

    #[test]
    fn text_helpers() {
        assert_eq!(hash("button|ok|OK|"), hash("button|ok|OK|"));
        assert_eq!(hash("").len(), 16);
        assert_eq!(hash("abc"), "a9993e364706816a", "SHA-1 prefix like Convert.ToHexString(.., 0, 8)");
        assert_eq!(trim("abcdef", 3), "abc…");
        assert_eq!(trim("abc", 3), "abc");
        assert_eq!(role_name(50000), Some("button"));
        assert_eq!(role_name(50028), Some("data grid"));
        assert_eq!(role_name(50040), Some("app bar"));
        assert_eq!(role_name(50041), None);
        assert_eq!(short_number(42.0), "42");
        assert_eq!(short_number(0.125), "0.12");
        assert_eq!(short_number(3.5), "3.5");
    }

    #[test]
    fn hints() {
        let mut e = Element { role: "button".into(), name: "&Delete".into(), ..Default::default() };
        common_hints(&mut e, Kind::Owned, "");
        assert_eq!(e.hints, ["destructive"]);
        let mut e = Element { role: "button".into(), name: "OK".into(), ..Default::default() };
        common_hints(&mut e, Kind::Foreground, "#32770");
        assert_eq!(e.hints, ["submits"]);
        let mut e = Element { role: "button".into(), name: "Don't Save".into(), ..Default::default() };
        common_hints(&mut e, Kind::Owned, "");
        assert_eq!(e.hints, ["closes dialog", "destructive"]);
        let mut e = Element { role: "hyperlink".into(), value: "https://x".into(), ..Default::default() };
        common_hints(&mut e, Kind::Foreground, "");
        assert_eq!(e.hints, ["link to https://x"]);
        assert_eq!(java_role("text", true), Some("edit"));
        assert_eq!(java_role("panel", false), None);
    }
}
