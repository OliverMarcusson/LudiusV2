//! What the persona may never touch: the never-list, scenario-reserved resources, unconfirmed commits.
use std::path::Path;

use anyhow::{Result, ensure};
use ludius_proto::v1::{Element, Snapshot, Window, window::Kind};
use serde::Deserialize;

use crate::tasks::{COMMITS, pixels_window, profile};
use crate::template::{escape, fullmatch, search};

const NEVER: &str = r"clear (browsing )?(history|cache|data)|empty recycle bin|disk clean-?up|uninstall|clear (the )?log|clear (all )?events|log size|format (disk|drive)|delete browsing";
pub const SHELL_PROCESSES: [&str; 5] = ["explorer.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe"];
const DESKTOP_CLASSES: [&str; 3] = ["Progman", "WorkerW", "Shell_TrayWnd"];
const ACTIVATION_KEYS: [&str; 9] = ["enter", "space", "ctrl+o", "alt+enter", "ctrl+enter", "shift+enter", "f2", "delete", "shift+delete"];
const FOCUS_KEYS: [&str; 5] = ["enter", "space", "ctrl+enter", "shift+enter", "alt+enter"];
const EDITABLE_ROLES: [&str; 3] = ["edit", "document", "combo box"];
pub const OVERWRITE_REASON: &str = "overwrite confirmation: abandon, never replace";

fn bare(name: &str) -> String {
    name.replace('&', "").trim().to_string()
}

pub fn commits(e: &Element) -> bool {
    let name = bare(&e.name);
    e.hints.iter().any(|h| h == "destructive") || COMMITS.iter().any(|c| fullmatch(c, &name, true))
}

pub fn overwrite_prompt(snap: &Snapshot) -> bool {
    let ow = &profile("default").expect("default app profile").overwrite;
    let text = format!("(?s){}", ow.text);
    snap.windows.iter().any(|w| fullmatch(&ow.titles, &w.title, true))
        || snap.elements.iter().any(|e| search(&text, &e.name, true))
        || snap.context.iter().any(|c| search(&text, c, true))
}

pub fn alt_f4_safe(fg: Option<&Window>) -> bool {
    fg.is_some_and(|w| !pixels_window(w) && !w.process_name.is_empty() && !DESKTOP_CLASSES.contains(&w.class_name.as_str()))
}

pub fn shell_dialog(fg: Option<&Window>) -> bool {
    fg.is_some_and(|w| w.class_name == "#32770" && SHELL_PROCESSES.contains(&w.process_name.to_lowercase().as_str()))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReservedPath {
    path: String,
    #[serde(default)]
    red_herring: bool,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields, default)]
struct Scenario {
    reserved: Vec<ReservedPath>,
    mailboxes: Vec<String>,
    messages: Vec<String>,
    pages: Vec<String>,
}

fn glob(g: &str) -> String {
    g.chars()
        .map(|c| match c {
            '*' => r"[^\\/]*".to_string(),
            '?' => r"[^\\/]".to_string(),
            c => escape(&c.to_string()),
        })
        .collect()
}

fn last(path: &str) -> &str {
    path.rsplit(['\\', '/']).next().unwrap_or("")
}

#[derive(Debug, Clone, Default)]
pub struct Policy {
    pub reserved_paths: Vec<String>,
    pub reserved_names: Vec<String>,
    pub reserved_stems: Vec<String>,
    pub reserved_name_stems: Vec<String>,
    pub reserved_subjects: Vec<String>,
    pub reserved_pages: Vec<String>,
}

impl Policy {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)?;
        let s: Scenario = serde_yaml::from_str::<Option<Scenario>>(&text)?.unwrap_or_default();
        let mut p = Self::default();
        let ext = regex::Regex::new(r"\.[^.\\/]*$").unwrap();
        for r in &s.reserved {
            ensure!(!r.path.trim().is_empty(), "{}: empty reserved path", path.display());
            if r.red_herring {
                continue;
            }
            let g = r.path.trim().trim_end_matches(['\\', '/']);
            p.reserved_paths.push(glob(g));
            p.reserved_names.push(glob(last(g)));
            let stem = ext.replace(g, "").into_owned();
            p.reserved_stems.push(glob(&stem));
            p.reserved_name_stems.push(glob(last(&stem)));
        }
        for m in &s.mailboxes {
            p.reserved_paths.push(glob(m.trim()));
            p.reserved_names.push(glob(m.trim()));
        }
        p.reserved_subjects = s.messages.iter().map(|m| m.trim()).filter(|m| !m.is_empty()).map(glob).collect();
        p.reserved_pages = s.pages.iter().map(|m| m.trim()).filter(|m| !m.is_empty()).map(glob).collect();
        p.reserved_pages.extend(Self::page_ids(&p.reserved_pages)?);
        Ok(p)
    }

    #[cfg(feature = "intranet")]
    fn page_ids(globs: &[String]) -> Result<Vec<String>> {
        if globs.is_empty() {
            return Ok(vec![]);
        }
        let pages = crate::intranet::load(&crate::intranet::dir())?;
        Ok(pages
            .iter()
            .filter(|p| globs.iter().any(|g| search(g, &p.title, true)))
            .map(|p| format!("[?&]a={}(?![a-z0-9-])", escape(&p.id)))
            .collect())
    }

    /// Without the intranet add-on there are no page ids; the title globs still guard typed text.
    #[cfg(not(feature = "intranet"))]
    fn page_ids(_globs: &[String]) -> Result<Vec<String>> {
        Ok(vec![])
    }

    pub fn from_env() -> Result<Self> {
        match std::env::var("LUDIUS_SCENARIO").ok().filter(|p| !p.is_empty()) {
            Some(p) => Self::load(Path::new(&p)),
            None => Ok(Self::default()),
        }
    }

    fn any_search(pats: &[String], text: &str) -> bool {
        pats.iter().any(|p| search(p, text, true))
    }

    fn any_full(pats: &[String], text: &str) -> bool {
        pats.iter().any(|p| fullmatch(p, text, true))
    }

    pub fn path_reserved(&self, path: &str) -> bool {
        if let Some(stem) = path.strip_suffix(".*") {
            let leaf = last(stem.trim());
            return Self::any_search(&self.reserved_paths, stem)
                || Self::any_search(&self.reserved_stems, stem)
                || Self::any_full(&self.reserved_names, leaf)
                || Self::any_full(&self.reserved_name_stems, leaf);
        }
        self.reserved(path) || Self::any_full(&self.reserved_names, last(path.trim()))
    }

    pub fn reserved(&self, text: &str) -> bool {
        Self::any_search(&self.reserved_paths, text)
    }

    pub fn page_reserved(&self, text: &str) -> bool {
        Self::any_search(&self.reserved_pages, text)
    }

    pub fn mail_reserved(&self, sender: &str, subject: &str, body: &str) -> bool {
        [sender, subject, body].iter().any(|x| self.reserved(x)) || Self::any_full(&self.reserved_subjects, subject.trim())
    }

    pub fn reserved_element(&self, e: &Element) -> bool {
        let name = e.name.trim();
        Self::any_full(&self.reserved_names, name)
            || self.reserved(&e.value)
            || self.reserved(name)
            || Self::any_search(&self.reserved_subjects, name)
            || self.page_reserved(name)
    }

    pub fn key_allowed(&self, keys: &str, snap: &Snapshot, declared: &[String]) -> Option<String> {
        let k = keys.to_lowercase();
        if overwrite_prompt(snap) && k != "esc" {
            return Some(OVERWRITE_REASON.into());
        }
        if !ACTIVATION_KEYS.contains(&k.as_str()) {
            return None;
        }
        let owned = snap.windows.iter().any(|w| w.kind == Kind::Owned as i32);
        let top = if owned { Kind::Owned } else { Kind::Foreground } as i32;
        let live: Vec<u32> = snap.windows.iter().enumerate().filter(|(_, w)| w.kind == Kind::Popup as i32 || w.kind == top).map(|(i, _)| i as u32).collect();
        let here: Vec<&Element> = snap
            .elements
            .iter()
            .filter(|e| live.contains(&e.window) && e.bounds.is_some_and(|b| b.width > 0 && b.height > 0))
            .collect();
        let typing = here.iter().any(|e| e.focused && EDITABLE_ROLES.contains(&e.role.as_str()) && !self.reserved_element(e));
        if !typing && here.iter().any(|e| (e.selected || e.focused) && self.reserved_element(e)) {
            return Some("scenario-reserved path".into());
        }
        if FOCUS_KEYS.contains(&k.as_str()) {
            let focus = here.iter().find(|e| e.focused);
            if focus.is_none() && declared.is_empty() {
                return Some("the focus is unknown and the task declares no committing effect".into());
            }
            if let Some(f) = focus {
                let name = bare(&f.name);
                if commits(f) && !Self::any_full(declared, &name) {
                    return Some(format!("the focused {} {name:?} commits or destroys, not declared", f.role));
                }
            }
        }
        None
    }

    pub fn element_allowed(&self, e: &Element, allow_destructive: &[String], snap: Option<&Snapshot>) -> Option<String> {
        if let Some(s) = snap
            && overwrite_prompt(s)
            && !fullmatch(&profile("default").expect("default app profile").overwrite.safe, &bare(&e.name), true)
        {
            return Some(OVERWRITE_REASON.into());
        }
        if e.password {
            return Some("password field".into());
        }
        if search(NEVER, &e.name, true) {
            return Some("never-list".into());
        }
        if self.reserved_element(e) {
            return Some("scenario-reserved path".into());
        }
        if e.hints.iter().any(|h| h == "destructive") && !Self::any_full(allow_destructive, &e.name.replace('&', "")) {
            return Some("destructive, not allowlisted".into());
        }
        None
    }

    pub fn text_allowed(&self, text: &str, target: Option<&Element>) -> Option<String> {
        if target.is_some_and(|t| t.password) {
            return Some("never type into password fields".into());
        }
        if self.reserved(text) || Self::any_full(&self.reserved_names, text.trim()) {
            return Some("scenario-reserved path".into());
        }
        if self.page_reserved(text) {
            return Some("scenario-reserved intranet page".into());
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reserved_globs() {
        let dir = std::env::temp_dir().join(format!("ludius-policy-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let f = dir.join("s.yaml");
        std::fs::write(&f, "reserved:\n  - path: 'Documents\\Q3*.xlsx'\n  - path: decoy.txt\n    red_herring: true\nmailboxes: [ceo@x]\nmessages: ['Board *']\n").unwrap();
        let p = Policy::load(&f).unwrap();
        assert!(p.path_reserved("C:\\Users\\a\\Documents\\Q3 budget.xlsx"));
        assert!(p.path_reserved("Documents\\Q3 budget.*"));
        assert!(!p.path_reserved("Documents\\Q4.xlsx"));
        assert!(!p.path_reserved("decoy.txt"));
        assert!(p.mail_reserved("a@x", "board minutes", ""));
        assert!(p.reserved("mail to CEO@x"));
        assert_eq!(p.text_allowed("q3 budget.xlsx", None).as_deref(), Some("scenario-reserved path"));
        let e = Element { name: "Empty Recycle Bin".into(), ..Default::default() };
        assert_eq!(p.element_allowed(&e, &[], None).as_deref(), Some("never-list"));
    }
}
