//! Task templates, app profiles, instances, and the done checks (UI and outside the UI).
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock};

use anyhow::{Context, Result, anyhow, bail, ensure};
use indexmap::IndexMap;
use ludius_proto::v1::{Element, Snapshot, Window, window::Kind};
use serde::{Deserialize, Serialize};
use serde_yaml::Value;
use rand::RngExt as _;
use sha2::{Digest, Sha256};

use crate::guestfile::{self, profile_path};
use crate::template::{casefold, escape, format_map, fullmatch, search};
use crate::{chat, mail, share};

// ---------- checks ----------

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct WindowCheck {
    pub process: Option<String>,
    pub title: Option<String>,
    pub class_name: Option<String>,
    pub absent: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ElementCheck {
    pub role: Option<String>,
    pub name: Option<String>,
    pub value: Option<String>,
    pub focused: Option<bool>,
    pub selected: Option<bool>,
    pub at: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AddedCheck {
    pub items: ElementCheck,
    pub new: ElementCheck,
    pub mark: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PixelsCheck {
    pub present: Vec<String>,
    #[serde(default)]
    pub absent: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pixels {
    pub title: String,
    pub anchors: Vec<String>,
    #[serde(default)]
    pub done: IndexMap<String, PixelsCheck>,
    #[serde(default)]
    pub hints: IndexMap<String, String>,
}

fn d_subject() -> String {
    "{subject}".into()
}
fn d_body() -> String {
    "{body}".into()
}
fn d_to() -> String {
    "{to}".into()
}
fn d_inbox() -> String {
    "inbox".into()
}
fn d_true() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MailCheck {
    pub mailbox: String,
    #[serde(default = "d_inbox")]
    pub folder: String,
    #[serde(default = "d_subject")]
    pub subject: String,
    #[serde(default = "d_body")]
    pub body: String,
    #[serde(default)]
    pub above_quote: bool,
    #[serde(default)]
    pub reply_to: String,
    #[serde(default)]
    pub attachment: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChatFileCheck {
    #[serde(default = "d_to")]
    pub to: String,
    pub path: String,
    #[serde(default = "d_true")]
    pub posted: bool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChatPostCheck {
    pub to: String,
    #[serde(default = "message_default")]
    pub message: String,
}
fn message_default() -> String {
    "{message}".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InviteCheck {
    #[serde(default = "d_to")]
    pub mailbox: String,
    #[serde(default = "d_subject")]
    pub subject: String,
    #[serde(default = "d_body")]
    pub body: String,
    #[serde(default = "date_default")]
    pub date: String,
    #[serde(default = "start_default")]
    pub start: String,
    #[serde(default = "end_default")]
    pub end: String,
}
fn date_default() -> String {
    "{date}".into()
}
fn start_default() -> String {
    "{start}".into()
}
fn end_default() -> String {
    "{end}".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResponseCheck {
    #[serde(default = "d_to")]
    pub mailbox: String,
    #[serde(default = "d_subject")]
    pub subject: String,
    #[serde(default = "response_default")]
    pub response: String,
}
fn response_default() -> String {
    "{response}".into()
}

pub const EDITABLE: [&str; 3] = [".docx", ".txt", ".xlsx"];
pub const ATTACHABLE: [&str; 5] = [".docx", ".xlsx", ".pptx", ".txt", ".pdf"];
pub const ATTACH_MAX_BYTES: usize = 5_000_000;

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct FileCheck {
    pub path: String,
    pub cells: Option<String>,
    pub paragraphs: Option<String>,
    pub slides: Option<Vec<String>>,
    pub download: Option<String>,
    pub append: Option<String>,
    pub source: Option<String>,
    pub recycled: bool,
    pub share: Option<String>,
    pub moved_from: Option<String>,
    pub attachment_of: Option<Vec<String>>,
    pub export_of: Option<String>,
    pub extract_of: Option<String>,
    pub comment: Option<String>,
}

fn noise(rel: &str) -> bool {
    profile_path("u", rel).is_some()
}

fn last5(s: &str) -> String {
    let l: Vec<char> = s.to_lowercase().chars().collect();
    l[l.len().saturating_sub(5)..].iter().collect()
}

fn ends(s: &str, exts: &[&str]) -> bool {
    let l = s.to_lowercase();
    exts.iter().any(|e| l.ends_with(e))
}

impl FileCheck {
    fn set(&self, k: &str) -> bool {
        match k {
            "cells" => self.cells.is_some(),
            "paragraphs" => self.paragraphs.is_some(),
            "slides" => self.slides.is_some(),
            "download" => self.download.is_some(),
            "append" => self.append.is_some(),
            "share" => self.share.is_some(),
            "moved_from" => self.moved_from.is_some(),
            "attachment_of" => self.attachment_of.is_some(),
            "export_of" => self.export_of.is_some(),
            "extract_of" => self.extract_of.is_some(),
            _ => unreachable!("{k}"),
        }
    }

    fn any(&self, keys: &[&str]) -> bool {
        keys.iter().any(|k| self.set(k))
    }

    pub fn validate(&self) -> Result<()> {
        const NEED: [&str; 3] = ["cells", "paragraphs", "slides"];
        let p = &self.path;
        let ext = last5(p);
        if let Some(src) = &self.source
            && (self.append.is_none() || !noise(src) || last5(src) != ext || src == p)
        {
            bail!("file check {p:?}: a source goes with append, another noise file, same type");
        }
        let with = |extra: &[&'static str]| -> Vec<&'static str> { NEED.iter().copied().chain(extra.iter().copied()).collect() };
        if self.comment.is_some() {
            ensure!(noise(p) && p.to_lowercase().ends_with(".docx"), "file check {p:?}: a comment goes on a noise .docx");
            let others = with(&["download", "append", "share", "moved_from", "attachment_of", "export_of", "extract_of"]);
            ensure!(!self.recycled && !self.any(&others), "file check {p:?}: a comment check stands alone");
            return Ok(());
        }
        if let Some(z) = &self.extract_of {
            ensure!(noise(p) && noise(z) && last5(z).ends_with(".zip"), "file check {p:?}: an extract goes from a noise .zip to a noise file");
            let others = with(&["download", "append", "share", "moved_from", "attachment_of", "export_of"]);
            ensure!(!self.recycled && !self.any(&others), "file check {p:?}: an extract check stands alone");
            return Ok(());
        }
        if let Some(src) = &self.export_of {
            let csv_in = src.to_lowercase().ends_with(".csv");
            let ok = noise(p) && noise(src) && ends(p, &[".pdf", ".zip", ".xlsx"]) && csv_in == p.to_lowercase().ends_with(".xlsx");
            ensure!(ok, "file check {p:?}: an export goes to a noise .pdf, .zip or (from .csv) .xlsx");
            let others = with(&["download", "append", "share", "moved_from", "attachment_of"]);
            ensure!(!self.recycled && !self.any(&others), "file check {p:?}: an export check stands alone");
            return Ok(());
        }
        if let Some(a) = &self.attachment_of {
            ensure!(a.len() == 2 && noise(p), "file check {p:?}: an attachment ([sender, subject]) saves to a noise folder");
            let others = with(&["download", "append", "share", "moved_from"]);
            ensure!(!self.recycled && !self.any(&others), "file check {p:?}: a saved attachment check stands alone");
            return Ok(());
        }
        if self.recycled || self.share.is_some() {
            let kind = if self.recycled { "recycled" } else { "share" };
            ensure!(noise(p), "file check {p:?}: a {kind} file is in a noise folder");
            let mut others = with(&["download", "append", "moved_from"]);
            if self.recycled {
                others.push("share");
            }
            ensure!(!self.any(&others), "file check {p:?}: a {kind} check stands alone");
            return Ok(());
        }
        if let Some(from) = &self.moved_from {
            ensure!(noise(from) && noise(p), "file check {p:?}: a move goes from one noise folder to another");
            ensure!(from != p && !self.any(&with(&["download", "append"])), "file check {p:?}: a move check stands alone");
            return Ok(());
        }
        if self.append.is_some() {
            ensure!(noise(p) && ends(p, &EDITABLE), "file check {p:?}: append edits a .docx, .txt or .xlsx in a noise folder");
            ensure!(!self.any(&with(&["download"])), "file check {p:?}: an append check stands alone");
            return Ok(());
        }
        if self.download.is_some() && noise(p) {
            ensure!(!self.any(&NEED), "file check {p:?}: a download check stands alone");
            return Ok(());
        }
        let ext = if p.to_lowercase().ends_with(".txt") { ".txt".to_string() } else { ext };
        let need = |e: &str| match e {
            ".xlsx" => Some("cells"),
            ".docx" | ".txt" => Some("paragraphs"),
            ".pptx" => Some("slides"),
            _ => None,
        };
        let Some(want) = need(&ext).filter(|_| noise(p)) else {
            bail!("file check {p:?}: an .xlsx/.docx/.pptx/.txt in a noise folder");
        };
        let got: Vec<&str> = NEED.iter().copied().filter(|k| self.set(k)).collect();
        ensure!(got == [want], "file check {p:?}: needs {want} and no other content check");
        Ok(())
    }
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct Done {
    pub window: Option<WindowCheck>,
    pub foreground: Option<WindowCheck>,
    pub element: Option<ElementCheck>,
    pub added: Option<AddedCheck>,
    pub typed: bool,
    pub pressed: bool,
    pub clicked: bool,
    pub clicked_on: Option<ElementCheck>,
    pub pixels: Option<PixelsCheck>,
    pub mail: Option<MailCheck>,
    pub invite: Option<InviteCheck>,
    pub response: Option<ResponseCheck>,
    pub file: Option<FileCheck>,
    pub read: bool,
    pub filed: bool,
    pub chat_file: Option<ChatFileCheck>,
    pub chat_post: Option<ChatPostCheck>,
    pub ask: Option<String>,
}

impl Done {
    pub fn validate(&self) -> Result<()> {
        if let Some(m) = &self.mail {
            ensure!(m.folder == "inbox" || m.folder == "sent", "mail check: folder {:?} is inbox or sent", m.folder);
            if let Some(a) = &m.attachment {
                ensure!(noise(a), "mail check: attachment {a:?} is a file in a noise folder");
            }
        }
        if let Some(px) = &self.pixels {
            ensure!(!px.present.is_empty(), "pixels check: present needs at least one pattern");
        }
        if let Some(c) = &self.chat_file {
            ensure!(noise(&c.path), "chat file check: {:?} is a file in a noise folder", c.path);
        }
        if let Some(f) = &self.file {
            f.validate()?;
        }
        if self.ask.is_some() {
            ensure!(Done { ask: None, ..self.clone() } == Done::default(), "done.ask stands alone: the Decider's answer is the whole check");
        }
        Ok(())
    }

    fn outside(&self) -> bool {
        self.mail.is_some()
            || self.invite.is_some()
            || self.response.is_some()
            || self.file.is_some()
            || self.read
            || self.filed
            || self.chat_file.is_some()
            || self.chat_post.is_some()
    }

    fn on_screen(&self) -> bool {
        self.window.is_some()
            || self.foreground.is_some()
            || self.element.is_some()
            || self.added.is_some()
            || self.typed
            || self.pressed
            || self.clicked
            || self.clicked_on.is_some()
            || self.pixels.is_some()
    }
}

fn d_steps() -> u32 {
    12
}
fn d_end() -> String {
    "end".into()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Subgoal {
    pub goal: String,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub search: String,
    #[serde(default)]
    pub keys: Vec<String>,
    #[serde(default)]
    pub launch: bool,
    #[serde(default)]
    pub copy_text: bool,
    #[serde(default)]
    pub allow_destructive: Vec<String>,
    #[serde(default)]
    pub require: Vec<ElementCheck>,
    #[serde(default)]
    pub in_window: Option<WindowCheck>,
    #[serde(default)]
    pub breadcrumb: Vec<String>,
    #[serde(default)]
    pub confirm: Option<String>,
    #[serde(default)]
    pub unreadable_ok: bool,
    #[serde(default)]
    pub needs: Option<String>,
    #[serde(default = "d_end")]
    pub caret: String,
    #[serde(default)]
    pub replace: bool,
    #[serde(default)]
    pub field: String,
    #[serde(default)]
    pub forbid: Vec<ElementCheck>,
    #[serde(default)]
    pub avoid: Vec<ElementCheck>,
    #[serde(default)]
    pub done: Option<Done>,
    #[serde(default = "d_steps")]
    pub steps: u32,
    #[serde(default)]
    pub pixels_hint: String,
}

impl Subgoal {
    pub fn validate(&self) -> Result<()> {
        ensure!(self.search.chars().count() <= 40 && self.search.chars().all(|c| (' '..='~').contains(&c)), "search {:?}: printable ASCII, at most 40", self.search);
        ensure!(self.keys.len() <= 16, "at most 16 keys");
        ensure!(matches!(self.caret.as_str(), "end" | "start" | "append"), "caret {:?}", self.caret);
        ensure!(
            !(self.text.as_deref().is_some_and(|t| !t.is_empty()) && !self.search.is_empty()),
            "a subgoal types either a content value (text) or template text (search), not both"
        );
        if let Some(d) = &self.done {
            d.validate()?;
        }
        Ok(())
    }
}

pub const RECIPIENT: &str = r"^({to}|{to_name}|{to_name} <{to}>);?\s*$";
pub const CAPABILITIES: [&str; 21] = [
    "mail.send", "mail.reply", "mail.read", "mail.save", "mail.file", "calendar.invite", "calendar.respond", "file.move",
    "file.write", "file.edit", "file.reuse", "file.export", "file.recycle", "file.share", "file.organize", "file.download",
    "chat.post", "chat.dm", "chat.read", "chat.read_channel", "chat.file",
];
pub fn sends(cap: &str) -> Option<&'static str> {
    match cap {
        "mail.send" | "mail.reply" => Some("mail"),
        "calendar.invite" => Some("meeting"),
        _ => None,
    }
}
pub fn irreversible(cap: &str) -> bool {
    sends(cap).is_some() || matches!(cap, "calendar.respond" | "chat.post" | "chat.dm" | "chat.file")
}
pub fn refers_to_existing(cap: &str) -> &'static [&'static str] {
    match cap {
        "mail.send" => &["to", "to_name", "file_name"],
        "mail.reply" => &["to", "to_name", "subject"],
        "mail.read" | "mail.save" | "mail.file" => &["sender", "subject"],
        "calendar.invite" | "chat.dm" | "chat.read" => &["to", "to_name"],
        "calendar.respond" => &["to", "to_name", "subject", "response"],
        "file.move" => &["file_name", "folder_name"],
        "file.edit" | "file.reuse" | "file.export" | "file.organize" => &["file_name"],
        "file.recycle" => &["file_name", "file_ext"],
        "file.share" => &["file_name", "file_ext", "share"],
        "chat.post" | "chat.read_channel" => &["channel"],
        "chat.file" => &["to", "to_name", "file_name"],
        _ => &[],
    }
}
pub const COMMITS: [&str; 7] = ["send", "send anyway", "save", "yes", "ok", "replace", "delete"];
pub const LAUNCH_TEXT: &str = "launch_query";
pub const CLOSE_KEYS: [&str; 3] = ["Alt+F4", "Esc", "Ctrl+W"];
pub const MOVES: [&str; 2] = ["file.organize", "file.move"];
pub const NAME_PARAMS: [&str; 3] = ["file_name", "folder_name", "new_name"];

pub fn declared_commits(task: &Task, sg: &Subgoal) -> Vec<String> {
    let mut v = sg.allow_destructive.clone();
    if !task.template.writes.is_empty() {
        v.push("Save".into());
    }
    v
}

// ---------- app profiles ----------

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Form {
    #[serde(default = "value_default")]
    pub recipients: String,
    pub to: ElementCheck,
    #[serde(default)]
    pub empty: Vec<ElementCheck>,
    #[serde(default)]
    pub never_filled: Vec<ElementCheck>,
    #[serde(default)]
    pub avoid: Vec<ElementCheck>,
}
fn value_default() -> String {
    "value".into()
}

impl Form {
    fn validate(&self) -> Result<()> {
        ensure!(self.recipients == "value", "form recipients must be value");
        let all = std::iter::once(&self.to).chain(&self.empty).chain(&self.never_filled);
        ensure!(all.into_iter().all(|c| c.value.is_none() && c.name.as_deref().is_some_and(|n| !n.is_empty())), "form fields are selectors: a name and no value");
        Ok(())
    }

    pub fn contract(&self) -> (Vec<ElementCheck>, Vec<ElementCheck>, Vec<ElementCheck>) {
        let mut require = vec![ElementCheck { value: Some(RECIPIENT.into()), ..self.to.clone() }];
        require.extend(self.empty.iter().map(|c| ElementCheck { value: Some("^$".into()), ..c.clone() }));
        let forbid =
            self.empty.iter().chain(&self.never_filled).map(|c| ElementCheck { name: c.name.clone(), value: Some(r"\S".into()), ..Default::default() }).collect();
        (require, forbid, self.avoid.clone())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dialogs {
    pub discard: Vec<String>,
    pub choice_ok: String,
    pub leave: Vec<String>,
}

impl Dialogs {
    fn validate(&self) -> Result<()> {
        for w in self.discard.iter().chain(&self.leave) {
            ensure!(!COMMITS.iter().any(|c| fullmatch(w, c, true)), "dialog word {w:?} names a button that commits");
        }
        let ok = self.choice_ok.to_lowercase();
        ensure!(ok == "ok" || !COMMITS.contains(&ok.as_str()), "choice_ok {:?} names a button that commits", self.choice_ok);
        Ok(())
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Overwrite {
    pub text: String,
    pub titles: String,
    pub safe: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Profile {
    #[serde(default)]
    pub process: String,
    #[serde(default)]
    pub launch_query: String,
    pub keys: HashMap<String, String>,
    pub locale: HashMap<String, String>,
    pub dialogs: Dialogs,
    #[serde(default)]
    pub forms: HashMap<String, Form>,
    pub overwrite: Overwrite,
}

fn merge(base: Value, own: Value) -> Value {
    match (base, own) {
        (Value::Mapping(mut b), Value::Mapping(o)) => {
            for (k, v) in o {
                let old = b.remove(&k).unwrap_or(Value::Null);
                b.insert(k, merge(old, v));
            }
            Value::Mapping(b)
        }
        (Value::Sequence(mut b), Value::Sequence(o)) => {
            b.extend(o);
            Value::Sequence(b)
        }
        (_, own) => own,
    }
}

pub fn apps_dir() -> std::path::PathBuf {
    crate::paths::data().join("apps")
}

/// The app's profile merged over default.yaml (cached per process).
pub fn profile(app: &str) -> Result<Arc<Profile>> {
    static CACHE: OnceLock<Mutex<HashMap<String, Arc<Profile>>>> = OnceLock::new();
    let cache = CACHE.get_or_init(Default::default);
    if let Some(p) = cache.lock().unwrap().get(app) {
        return Ok(p.clone());
    }
    let dir = apps_dir();
    let base: Value = serde_yaml::from_str(&std::fs::read_to_string(dir.join("default.yaml"))?)?;
    let path = dir.join(format!("{app}.yaml"));
    let mine = app != "default" && fullmatch("[a-z0-9_]+", app, false) && path.exists();
    let own: Value = if mine { serde_yaml::from_str(&std::fs::read_to_string(&path)?)? } else { Value::Mapping(Default::default()) };
    ensure!(own.get("overwrite").is_none(), "{}: the overwrite guard is range-wide, not an app's", path.display());
    let p: Profile = serde_yaml::from_value(merge(base, own)).with_context(|| format!("profile {app}"))?;
    ensure!(
        p.keys.get("close").is_some_and(|k| CLOSE_KEYS.contains(&k.as_str())),
        "close key {:?} is not one of {CLOSE_KEYS:?}",
        p.keys.get("close")
    );
    ensure!(p.locale.keys().all(|k| k == "date") && p.locale.contains_key("date"), "locale needs date only");
    ensure!(p.forms.keys().all(|k| k == "mail" || k == "meeting"), "forms are mail or meeting");
    p.dialogs.validate()?;
    for f in p.forms.values() {
        f.validate()?;
    }
    let p = Arc::new(p);
    cache.lock().unwrap().insert(app.into(), p.clone());
    Ok(p)
}

// ---------- templates ----------

fn save_extension(app: &str) -> Option<&'static str> {
    match app {
        "notepad" => Some(".txt"),
        "wordpad" => Some(".rtf"),
        "paint" => Some(".png"),
        "excel" => Some(".xlsx"),
        "word" => Some(".docx"),
        "powerpoint" => Some(".pptx"),
        _ => None,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyCommit {
    pub keys: String,
    pub focus: ElementCheck,
}

fn d_budget_steps() -> u32 {
    40
}
fn d_budget_minutes() -> f64 {
    8.0
}
fn d_abandon() -> String {
    "close_without_saving".into()
}
fn d_weight() -> f64 {
    1.0
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Template {
    pub task: String,
    pub app: String,
    #[serde(default)]
    pub capability: Option<String>,
    #[serde(default)]
    pub processes: Vec<String>,
    pub variants: Vec<Vec<String>>,
    #[serde(default)]
    pub weights: Option<Vec<f64>>,
    pub subgoals: IndexMap<String, Subgoal>,
    #[serde(default = "d_budget_steps")]
    pub budget_steps: u32,
    #[serde(default = "d_budget_minutes")]
    pub budget_minutes: f64,
    #[serde(default = "d_abandon")]
    pub on_abandon: String,
    #[serde(default)]
    pub params: Vec<String>,
    #[serde(default)]
    pub defaults: IndexMap<String, String>,
    #[serde(default)]
    pub about: String,
    #[serde(default)]
    pub example: IndexMap<String, String>,
    #[serde(default)]
    pub resembles: String,
    #[serde(default = "d_weight")]
    pub plan_weight: f64,
    #[serde(default)]
    pub pixels: Option<Pixels>,
    #[serde(default)]
    pub writes: Vec<String>,
    #[serde(default)]
    pub removes: Vec<String>,
    #[serde(default)]
    pub fallback_for: String,
    #[serde(default)]
    pub key_commits: Vec<KeyCommit>,
}

impl Template {
    pub fn cap(&self) -> &str {
        self.capability.as_deref().unwrap_or("")
    }

    pub fn attaches(&self) -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for d in self.subgoals.values().filter_map(|s| s.done.as_ref()) {
            for p in [d.mail.as_ref().and_then(|m| m.attachment.clone()), d.chat_file.as_ref().map(|c| c.path.clone())].into_iter().flatten() {
                if !p.is_empty() && !out.contains(&p) {
                    out.push(p);
                }
            }
        }
        out
    }

    pub fn sends_mail(&self) -> bool {
        sends(self.cap()).is_some()
    }

    pub fn irreversible(&self) -> bool {
        irreversible(self.cap())
    }

    pub fn send_form(&self) -> &'static str {
        sends(self.cap()).unwrap_or("mail")
    }

    pub fn task_content(&self) -> Option<&'static str> {
        (self.cap() == "mail.reply").then_some("reply")
    }

    fn dones(&self) -> impl Iterator<Item = &Done> {
        self.subgoals.values().filter_map(|s| s.done.as_ref())
    }

    /// Pydantic's after-validators, in their order; some fill in derived fields.
    fn validate(&mut self) -> Result<()> {
        let t = self.task.clone();
        if let Some(c) = &self.capability {
            ensure!(CAPABILITIES.contains(&c.as_str()), "{t}: unknown capability {c:?}");
        }
        ensure!(self.plan_weight > 0.0 && self.plan_weight <= 1.0, "{t}: plan_weight in (0, 1]");
        if let Some(px) = &self.pixels {
            ensure!(!px.anchors.is_empty() && px.done.values().all(|c| !c.present.is_empty()), "{t}: pixels need anchors and present");
        }
        for sg in self.subgoals.values() {
            sg.validate().with_context(|| t.clone())?;
        }
        // _profile
        let p = profile(&self.app)?;
        if self.processes.is_empty() && !p.process.is_empty() {
            self.processes = vec![p.process.clone()];
        }
        ensure!(!self.processes.is_empty(), "{t}: no processes, and app {:?} has no profile process", self.app);
        if !p.launch_query.is_empty() {
            self.defaults.entry("launch_query".into()).or_insert(p.launch_query.clone());
        }
        for sg in self.subgoals.values_mut() {
            sg.keys = sg.keys.iter().map(|k| p.keys.get(k).cloned().unwrap_or_else(|| k.clone())).collect();
        }
        // _task_content
        if self.task_content() == Some("reply") {
            let checks: Vec<&MailCheck> = self.dones().filter_map(|d| d.mail.as_ref()).collect();
            ensure!(
                !checks.is_empty() && checks.iter().all(|c| c.reply_to == "{reply_to}") && !self.params.contains(&"body".into()),
                "{t}: a task-time reply needs mail checks with reply_to and no planned body"
            );
        }
        // _read
        let params: BTreeSet<&str> = self.params.iter().map(String::as_str).collect();
        let reads = self.dones().any(|d| d.read);
        ensure!(
            reads == (self.cap() == "mail.read") && (!reads || params.is_superset(&BTreeSet::from(["sender", "subject"]))),
            "{t}: mail.read needs a read done check and params sender, subject (only it)"
        );
        let typed = self.subgoals.values().any(|sg| {
            sg.text.as_deref().is_some_and(|x| x != LAUNCH_TEXT) || !sg.search.is_empty() || sg.copy_text || !sg.allow_destructive.is_empty()
        });
        ensure!(!(reads && (!self.writes.is_empty() || typed)), "{t}: mail.read types only the launch query, no writes or destructive clicks");
        let files = self.dones().any(|d| d.filed);
        ensure!(
            files == (self.cap() == "mail.file") && (!files || params == BTreeSet::from(["sender", "subject", "folder"])),
            "{t}: mail.file needs a filed done check and params sender, subject, folder"
        );
        let chats = self.dones().any(|d| d.chat_file.as_ref().is_some_and(|c| c.posted));
        ensure!(chats == (self.cap() == "chat.file"), "{t}: chat.file needs a chat_file done check (only it)");
        // _writes_declared
        let attaches = self.attaches();
        let own = !attaches.is_empty() || matches!(self.cap(), "file.recycle" | "file.share");
        let names: Vec<&str> = NAME_PARAMS.iter().copied().filter(|n| params.contains(n)).collect();
        ensure!(names.is_empty() || !self.writes.is_empty() || own, "{t}: creates {names:?} but declares no writes");
        for r in &self.removes {
            ensure!(noise(r.strip_suffix(".*").unwrap_or(r)), "{t}: remove {r:?} is not in a noise folder {:?}", guestfile::NOISE_FOLDERS);
        }
        for w in &self.writes {
            ensure!(noise(w.strip_suffix(".*").unwrap_or(w)), "{t}: write {w:?} is not in a noise folder {:?}", guestfile::NOISE_FOLDERS);
            if let Some(ext) = save_extension(&self.app)
                && w.contains("{file_name}")
            {
                let ok = if self.app == "word" { ends(w, &[ext, ".pdf"]) } else { ends(w, &[ext]) };
                ensure!(ok, "{t}: {} saves {ext}, so write {w:?} must end with it", self.app);
            }
        }
        let marks: BTreeSet<&str> = self.dones().filter_map(|d| d.added.as_ref().map(|a| a.mark.as_str())).collect();
        for (name, sg) in &self.subgoals {
            if let Some(at) = sg.done.as_ref().and_then(|d| d.element.as_ref()).and_then(|e| e.at.as_deref()) {
                ensure!(marks.contains(at), "{t}: {name} checks the item at {at:?}, which no added check marks");
            }
        }
        for f in self.dones().filter_map(|d| d.file.as_ref()) {
            if f.recycled {
                ensure!(self.removes.contains(&f.path) && self.cap() == "file.recycle", "{t}: recycled check {:?} is not a file.recycle remove", f.path);
            } else if f.share.is_some() {
                ensure!(self.cap() == "file.share", "{t}: share check {:?} needs capability file.share", f.path);
            } else if let Some(from) = &f.moved_from {
                ensure!(self.removes.contains(from) && MOVES.contains(&self.cap()), "{t}: move check {:?} needs a move capability removing it", f.path);
                ensure!(self.writes.contains(&f.path), "{t}: file check {:?} is not a declared write", f.path);
            } else {
                ensure!(self.writes.contains(&f.path), "{t}: file check {:?} is not a declared write", f.path);
            }
        }
        let appends: Vec<&FileCheck> = self.dones().filter_map(|d| d.file.as_ref()).filter(|f| f.append.is_some() || f.comment.is_some()).collect();
        let kinds: BTreeSet<&str> = appends.iter().map(|c| if c.source.is_some() { "file.reuse" } else { "file.edit" }).collect();
        let revise = self.cap() == "mail.send" && kinds == BTreeSet::from(["file.edit"]);
        if revise {
            ensure!(appends.iter().all(|c| attaches.contains(&c.path)), "{t}: a mail.send edits only the file it attaches");
        } else {
            let want: BTreeSet<&str> = [self.cap()].into_iter().filter(|c| matches!(*c, "file.edit" | "file.reuse")).collect();
            ensure!(kinds == want, "{t}: file.edit takes an append or comment check, file.reuse one with a source");
        }
        ensure!(!appends.iter().any(|c| c.source.as_ref().is_some_and(|s| self.writes.contains(s))), "{t}: a reused source is read, never a write");
        // _send_contract
        let sends_names: Vec<String> =
            self.subgoals.iter().filter(|(_, sg)| sg.done.as_ref().is_some_and(|d| d.mail.is_some() || d.invite.is_some())).map(|(n, _)| n.clone()).collect();
        ensure!(sends_names.is_empty() || self.sends_mail(), "{t}: a mail or meeting done check needs a sending capability");
        let form_kind = self.send_form();
        let wrong = self.dones().any(|d| if form_kind == "mail" { d.invite.is_some() } else { d.mail.is_some() });
        ensure!(!wrong, "{t}: a {} done check does not fit send_form {form_kind:?}", if form_kind == "mail" { "invite" } else { "mail" });
        if self.sends_mail() {
            ensure!(!sends_names.is_empty(), "{t}: {} without a subgoal whose done check is a mail check", self.cap());
            let form = p.forms.get(form_kind).ok_or_else(|| anyhow!("{t}: app {:?} has no {form_kind} form in its profile", self.app))?;
            let (require, forbid, avoid) = form.contract();
            for (name, sg) in self.subgoals.iter_mut() {
                sg.avoid = avoid.iter().cloned().chain(sg.avoid.drain(..)).collect();
                if sends_names.contains(name) {
                    sg.require = require.iter().cloned().chain(sg.require.drain(..)).collect();
                    sg.forbid = forbid.iter().cloned().chain(sg.forbid.drain(..)).collect();
                }
            }
        }
        // _pixels_done
        if let Some(px) = self.pixels.clone() {
            for (name, check) in px.done {
                let sg = self.subgoals.get_mut(&name).ok_or_else(|| anyhow!("{t}: pixels done for unknown subgoal {name:?}"))?;
                sg.done.get_or_insert_with(Done::default).pixels = Some(check);
            }
            for (name, hint) in px.hints {
                let sg = self.subgoals.get_mut(&name).ok_or_else(|| anyhow!("{t}: pixels hint for unknown subgoal {name:?}"))?;
                sg.pixels_hint = hint;
            }
        }
        // check()
        for v in &self.variants {
            for name in v {
                ensure!(self.subgoals.contains_key(name), "{t}: variant uses unknown subgoal {name:?}");
            }
        }
        Ok(())
    }

    pub fn from_yaml(text: &str) -> Result<Self> {
        let mut t: Template = serde_yaml::from_str(text)?;
        t.validate()?;
        Ok(t)
    }
}

pub fn load_template(path: &Path) -> Result<Template> {
    Template::from_yaml(&std::fs::read_to_string(path)?).with_context(|| path.display().to_string())
}

pub fn load_yaml<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    Ok(serde_yaml::from_str(&std::fs::read_to_string(path)?).with_context(|| path.display().to_string())?)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Persona {
    pub name: String,
    pub user: String,
    #[serde(default)]
    pub role: String,
    #[serde(default)]
    pub department: String,
    #[serde(default = "d_hours")]
    pub hours: (String, String),
    #[serde(default)]
    pub style: String,
    #[serde(default = "d_typing")]
    pub typing_mean_ms: u32,
    #[serde(default = "d_jitter")]
    pub typing_jitter_ms: u32,
    #[serde(default = "d_think")]
    pub think_mean_s: f64,
    #[serde(default = "d_pointer")]
    pub pointer_ms: (u32, u32),
    #[serde(default)]
    pub risk_profile: Vec<String>,
    #[serde(default = "d_tz")]
    pub tz: String,
}
fn d_hours() -> (String, String) {
    ("08:30".into(), "17:00".into())
}
fn d_typing() -> u32 {
    140
}
fn d_jitter() -> u32 {
    60
}
fn d_think() -> f64 {
    1.6
}
fn d_pointer() -> (u32, u32) {
    (180, 520)
}
pub fn d_tz() -> String {
    "Europe/Stockholm".into()
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TaskInstance {
    pub template: String,
    #[serde(default)]
    pub params: IndexMap<String, String>,
    #[serde(default)]
    pub content: IndexMap<String, String>,
    #[serde(default)]
    pub variant: Option<usize>,
    #[serde(default)]
    pub context: String,
    #[serde(default)]
    pub request: String,
}

// ---------- a task in flight ----------

pub const HEAD: usize = 24;
const ROWS: [&str; 3] = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];

fn neighbour(c: char) -> char {
    let l = c.to_ascii_lowercase();
    let Some(row) = ROWS.iter().find(|r| r.contains(l)) else { return c };
    let b = row.as_bytes();
    let i = row.find(l).unwrap();
    let n = b[(i + 1) % b.len()] as char;
    if c.is_uppercase() { n.to_ascii_uppercase() } else { n }
}

fn iso_date(v: &str) -> bool {
    let b = v.as_bytes();
    b.len() == 10 && b[4] == b'-' && b[7] == b'-' && b.iter().enumerate().all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit())
}

#[derive(Debug, Clone)]
pub struct Task {
    pub template: Template,
    pub inst: TaskInstance,
    pub owner: String,
    pub started_ms: i64,
    pub tz: String,
    pub variant: usize,
    pub subgoal_names: Vec<String>,
    pub values: HashMap<String, String>,
    pub mail_before: Option<mail::Seen>,
    pub files_absent: Option<BTreeSet<String>>,
    pub files_before: HashMap<String, Vec<String>>,
    pub sources_before: HashMap<String, Vec<u8>>,
    pub comments_before: HashMap<String, Vec<String>>,
    pub saved_sha: HashMap<String, String>,
    pub files_present: BTreeSet<String>,
    pub read_id: u64,
    pub file_msgid: String,
    pub added_base: Option<usize>,
    pub marks: HashMap<String, (ElementCheck, usize)>,
}

impl Task {
    pub fn new(template: Template, inst: TaskInstance, rng: &mut impl rand::Rng) -> Result<Self> {
        let t = &template.task;
        let mut env = HashMap::new();
        let mut unset = Vec::new();
        for (k, v) in &template.defaults {
            if let Some(var) = v.strip_prefix('$') {
                let got = std::env::var(var).unwrap_or_default();
                if got.is_empty() {
                    unset.push(v.clone());
                }
                env.insert(k.clone(), got);
            }
        }
        unset.sort();
        ensure!(unset.is_empty(), "{t}: {} unset", unset.join(", "));
        let mut have: HashMap<String, String> = template.defaults.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
        have.extend(env);
        have.extend(inst.params.iter().map(|(k, v)| (k.clone(), v.clone())));
        have.extend(inst.content.iter().map(|(k, v)| (k.clone(), v.clone())));
        let missing: Vec<&String> =
            template.params.iter().filter(|p| have.get(*p).is_none_or(|v| v.is_empty()) && !template.defaults.contains_key(*p)).collect();
        ensure!(missing.is_empty(), "{t}: missing parameters {missing:?}");
        let snapshot = have.clone();
        for (k, v) in &template.defaults {
            if v.contains('{') && !inst.params.contains_key(k) {
                have.insert(k.clone(), crate::template::format(v, &snapshot)?);
            }
        }
        let n = template.variants.len();
        ensure!(n > 0, "{t}: no variants");
        let idx = match inst.variant {
            Some(v) => {
                ensure!(v < n, "{t}: variant {v} out of range");
                v
            }
            None => {
                let w = template.weights.clone().unwrap_or_else(|| vec![1.0; n]);
                ensure!(w.len() == n, "{t}: weights do not match variants");
                let total: f64 = w.iter().sum();
                let mut x = rng.random::<f64>() * total;
                let mut pick = n - 1;
                for (i, wi) in w.iter().enumerate() {
                    if x < *wi {
                        pick = i;
                        break;
                    }
                    x -= wi;
                }
                pick
            }
        };
        let skip: BTreeSet<&String> = template
            .subgoals
            .iter()
            .filter(|(_, sg)| {
                sg.text.as_ref().or(sg.needs.as_ref()).filter(|k| !k.is_empty()).is_some_and(|k| template.defaults.contains_key(k) && have.get(k).is_none_or(|v| v.is_empty()))
            })
            .map(|(n, _)| n)
            .collect();
        let subgoal_names: Vec<String> = template.variants[idx].iter().filter(|n| !skip.contains(n)).cloned().collect();
        let date = profile(&template.app)?.locale["date"].clone();
        let mut values = have.clone();
        for (k, v) in &have {
            if !v.is_empty() && !v.contains('\n') {
                values.insert(format!("{k}_typo"), format!("{v}{}", neighbour(v.chars().last().unwrap())));
            }
            if !v.is_empty() {
                values.insert(format!("{k}_enter"), format!("{}\n", v.trim_end_matches('\n')));
                values.insert(format!("{k}_line"), format!("\n{}", v.trim_start_matches('\n')));
            }
            if iso_date(v) {
                let m: HashMap<String, String> = [("y", &v[..4]), ("m", &v[5..7]), ("d", &v[8..])].iter().map(|(a, b)| (a.to_string(), b.to_string())).collect();
                values.insert(format!("{k}_ui"), crate::template::format(&date, &m)?);
            }
            if NAME_PARAMS.contains(&k.as_str()) {
                let stem = regex::Regex::new(r"\.\w{1,5}$").unwrap().replace(v, "").into_owned();
                values.insert(format!("{k}_stem"), stem);
            }
            if !v.trim().is_empty() {
                let line = v.lines().map(str::trim).find(|x| !x.is_empty()).unwrap_or("");
                values.insert(format!("{k}_head"), line.chars().take(HEAD).collect());
            }
        }
        Ok(Self {
            template,
            inst,
            owner: String::new(),
            started_ms: 0,
            tz: d_tz(),
            variant: idx,
            subgoal_names,
            values,
            mail_before: None,
            files_absent: None,
            files_before: HashMap::new(),
            sources_before: HashMap::new(),
            comments_before: HashMap::new(),
            saved_sha: HashMap::new(),
            files_present: BTreeSet::new(),
            read_id: 0,
            file_msgid: String::new(),
            added_base: None,
            marks: HashMap::new(),
        })
    }

    pub fn fmt(&self, s: &str) -> Result<String> {
        crate::template::format(s, &self.values)
    }

    pub fn fmt_re(&self, s: &str) -> Result<String> {
        format_map(s, |k| self.values.get(k).map(|v| escape(v)))
    }

    pub fn pixels_re(&self, s: &str) -> String {
        let re = regex::Regex::new(r"\{(\w+)\}").unwrap();
        re.replace_all(s, |c: &regex::Captures| match self.values.get(&c[1]) {
            Some(v) => escape(&compact(v)),
            None => c[0].to_string(),
        })
        .into_owned()
    }

    pub fn subgoal(&self, name: &str) -> Result<Subgoal> {
        let sg = &self.template.subgoals[name];
        Ok(Subgoal { goal: self.fmt(&sg.goal)?, ..sg.clone() })
    }

    pub fn text(&self, sg: &Subgoal) -> Option<String> {
        match &sg.text {
            Some(t) if !t.is_empty() => self.values.get(t).cloned(),
            _ => (!sg.search.is_empty()).then(|| sg.search.clone()),
        }
    }
}

// ---------- on-screen checks ----------

fn is_kind(w: &Window, k: Kind) -> bool {
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

fn visible(pattern: &str, rows: &[&Element], task: &Task) -> bool {
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

fn items<'a>(c: &ElementCheck, snap: &'a Snapshot, task: &Task) -> Result<Vec<&'a Element>> {
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

fn added_ok(c: &AddedCheck, snap: &Snapshot, task: &mut Task) -> Result<bool> {
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

fn marked<'a>(mark: &str, snap: &'a Snapshot, task: &Task) -> Result<Vec<&'a Element>> {
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

// ---------- outside the UI ----------

fn outside_ok(done: &Done, task: &mut Task) -> Result<bool> {
    if let Some(c) = &done.mail
        && !mail_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.invite
        && !invite_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.response
        && !response_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.file
        && !file_ok(c, task)?
    {
        return Ok(false);
    }
    if done.read && !(task.read_id > 0 && !task.owner.is_empty() && mail::read_state(&task.owner, task.read_id, "inbox") == Some(true)) {
        return Ok(false);
    }
    if done.filed {
        let folder = task.values.get("folder").cloned().unwrap_or_default();
        if task.owner.is_empty() || mail::placed(&task.owner, &task.file_msgid, &folder) != Some((0, 1)) {
            return Ok(false);
        }
    }
    if let Some(c) = &done.chat_file
        && !chat_file_ok(c, task)?
    {
        return Ok(false);
    }
    if let Some(c) = &done.chat_post
        && !chat_post_ok(c, task)?
    {
        return Ok(false);
    }
    Ok(true)
}

pub fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data).iter().map(|b| format!("{b:02x}")).collect()
}

fn split_addr(a: &str) -> (String, String) {
    let l = a.to_lowercase();
    match l.split_once('@') {
        Some((u, d)) => (u.into(), d.into()),
        None => (l, String::new()),
    }
}

fn chat_file_ok(c: &ChatFileCheck, task: &Task) -> Result<bool> {
    let (rel, to) = (task.fmt(&c.path)?, task.fmt(&c.to)?);
    let (user, dom) = split_addr(&to);
    let Some(data) = task.sources_before.get(&rel) else { return Ok(false) };
    if task.owner.is_empty() || dom != mail::domain() {
        return Ok(false);
    }
    Ok(chat::shared(&task.owner, &user, &leaf(&rel), task.started_ms, c.posted) == Some(vec![sha256_hex(data)]))
}

fn chat_post_ok(c: &ChatPostCheck, task: &Task) -> Result<bool> {
    let mut to = task.fmt(&c.to)?;
    if !to.starts_with('~') {
        let (u, d) = split_addr(&to);
        if d != mail::domain() {
            return Ok(false);
        }
        to = u;
    }
    Ok(!task.owner.is_empty() && chat::posted(&task.owner, &to, &task.fmt(&c.message)?, task.started_ms).unwrap_or(0) > 0)
}

fn mail_ok(c: &MailCheck, task: &Task) -> Result<bool> {
    let sender = if task.owner.is_empty() { String::new() } else { format!("{}@{}", task.owner, mail::domain()) };
    let mut attachment = None;
    if let Some(a) = &c.attachment {
        let rel = task.fmt(a)?;
        let Some(data) = task.sources_before.get(&rel) else { return Ok(false) };
        attachment = Some((casefold(&leaf(&rel)), sha256_hex(data)));
    }
    Ok(mail::delivered(
        &task.fmt(&c.mailbox)?,
        &c.folder,
        &task.fmt(&c.subject)?,
        &task.fmt(&c.body)?,
        &sender,
        task.started_ms,
        c.above_quote,
        task.mail_before.as_ref(),
        &task.fmt(&c.reply_to)?,
        attachment.as_ref(),
    ))
}

pub fn local_ms(date: &str, hhmm: &str, tz: &str) -> Result<i64> {
    let tz: chrono_tz::Tz = tz.parse().map_err(|e| anyhow!("{e}"))?;
    let naive = chrono::NaiveDateTime::parse_from_str(&format!("{date}T{hhmm}"), "%Y-%m-%dT%H:%M")
        .or_else(|_| chrono::NaiveDateTime::parse_from_str(&format!("{date}T{hhmm}"), "%Y-%m-%dT%H:%M:%S"))?;
    // Python's replace(tzinfo=ZoneInfo) takes the earlier offset (fold=0) on a DST overlap.
    let t = naive.and_local_timezone(tz).earliest().ok_or_else(|| anyhow!("{date} {hhmm} does not exist in {tz}"))?;
    Ok(t.timestamp_millis())
}

fn response_ok(c: &ResponseCheck, task: &Task) -> Result<bool> {
    Ok(!task.owner.is_empty()
        && mail::responded(
            &task.fmt(&c.mailbox)?,
            &task.fmt(&c.subject)?,
            &format!("{}@{}", task.owner, mail::domain()),
            &task.fmt(&c.response)?,
            task.started_ms,
            task.mail_before.as_ref(),
        ))
}

fn invite_ok(c: &InviteCheck, task: &Task) -> Result<bool> {
    if task.owner.is_empty() {
        return Ok(false);
    }
    let date = task.fmt(&c.date)?;
    Ok(mail::invited(
        &task.fmt(&c.mailbox)?,
        &task.fmt(&c.subject)?,
        &task.fmt(&c.body)?,
        &format!("{}@{}", task.owner, mail::domain()),
        task.started_ms,
        local_ms(&date, &task.fmt(&c.start)?, &task.tz)?,
        local_ms(&date, &task.fmt(&c.end)?, &task.tz)?,
        task.mail_before.as_ref(),
    ))
}

/// (mailbox, folder, subject, the sender's folder) of a subgoal's sent check.
fn sent_check(done: Option<&Done>) -> Option<(String, String, String, &'static str)> {
    let d = done?;
    if let Some(m) = &d.mail {
        return Some((m.mailbox.clone(), m.folder.clone(), m.subject.clone(), "sent"));
    }
    if let Some(i) = &d.invite {
        return Some((i.mailbox.clone(), "inbox".into(), i.subject.clone(), "calendar"));
    }
    d.response.as_ref().map(|r| (r.mailbox.clone(), "inbox".into(), r.subject.clone(), "sent"))
}

pub fn mail_baseline(task: &Task) -> Result<Option<mail::Seen>> {
    let sender = format!("{}@{}", task.owner, mail::domain());
    let mut seen = mail::Seen::new();
    for sg in task.template.subgoals.values() {
        if let Some((mb, folder, subject, mine)) = sent_check(sg.done.as_ref()) {
            match mail::present(&task.fmt(&mb)?, &folder, &task.fmt(&subject)?, &sender, mine) {
                Some(found) => seen.extend(found),
                None => return Ok(None),
            }
        }
    }
    Ok(Some(seen))
}

pub const RESERVED_RECIPIENT: &str = "precondition: the planned recipient is scenario-reserved";

pub fn recipient_reserved(sg: &Subgoal, task: &Task, policy: &crate::policy::Policy) -> Result<bool> {
    Ok(match sent_check(sg.done.as_ref()) {
        Some((mb, ..)) => policy.reserved(&task.fmt(&mb)?),
        None => false,
    })
}

pub fn write_paths(task: &Task) -> Result<Vec<String>> {
    task.template.writes.iter().map(|w| task.fmt(w)).collect()
}

pub fn file_reserved(user: &str, rel: &str, policy: &crate::policy::Policy) -> bool {
    let any_ext = if rel.ends_with(".*") { ".*" } else { "" };
    let full = if user.is_empty() { None } else { profile_path(user, rel.strip_suffix(".*").unwrap_or(rel)) };
    policy.path_reserved(rel) || full.is_some_and(|f| policy.path_reserved(&format!("{f}{any_ext}")))
}

/// Read what the done checks will compare against; a reason string means abandon before acting.
pub fn file_baseline(task: &mut Task, policy: &crate::policy::Policy) -> Result<Option<String>> {
    let files: Vec<FileCheck> = task.template.dones().filter_map(|d| d.file.clone()).collect();
    let owner = task.owner.clone();
    let mut sources = Vec::new();
    for c in &files {
        if let Some(s) = c.source.as_ref().or(c.export_of.as_ref()).or(c.extract_of.as_ref()) {
            sources.push(task.fmt(s)?);
        }
    }
    let attached: Vec<String> = task.template.attaches().iter().map(|a| task.fmt(a)).collect::<Result<_>>()?;
    let removes: Vec<String> = task.template.removes.iter().map(|r| task.fmt(r)).collect::<Result<_>>()?;
    for rel in write_paths(task)?.iter().chain(&removes).chain(&sources).chain(&attached) {
        if file_reserved(&owner, rel, policy) {
            return Ok(Some(format!("{rel}: scenario-reserved file")));
        }
    }
    let mut absent = BTreeSet::new();
    let mut present = BTreeSet::new();
    let recycled: BTreeSet<String> = files.iter().filter(|c| c.recycled).map(|c| task.fmt(&c.path)).collect::<Result<_>>()?;
    for rel in recycled {
        match guestfile::read(&owner, &rel) {
            None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
            Some(f) if !f.exists => return Ok(Some(format!("{rel}: not there, nothing to recycle"))),
            Some(_) => {
                present.insert(rel);
            }
        }
    }
    task.files_present = present;
    for c in files.iter().filter(|c| c.share.is_some()) {
        let (rel, dest) = (task.fmt(&c.path)?, task.fmt(c.share.as_deref().unwrap_or(""))?);
        let copy = share::unc(&dest, &leaf(&rel));
        if file_reserved(&owner, &rel, policy) || copy.is_empty() || policy.path_reserved(&copy) {
            let shown = if copy.is_empty() { "(LUDIUS_SHARE_HOST unset)".to_string() } else { copy };
            return Ok(Some(format!("{rel} or its copy {shown}: scenario-reserved or unknown")));
        }
        let (f, s) = (guestfile::read(&owner, &rel), share::read(&dest, &leaf(&rel)));
        let (Some(f), Some(s)) = (f.clone(), s) else {
            return Ok(Some(format!("{}: unreadable", if f.is_none() { rel } else { copy })));
        };
        if !f.exists {
            return Ok(Some(format!("{rel}: not there, nothing to copy")));
        }
        if s.exists {
            return Ok(Some(format!("{copy}: already exists, never overwritten")));
        }
        absent.insert(copy);
    }
    let moved: BTreeSet<String> = files.iter().filter_map(|c| c.moved_from.as_ref()).map(|m| task.fmt(m)).collect::<Result<_>>()?;
    for src in moved {
        match guestfile::read(&owner, &src) {
            None => return Ok(Some(format!("{src}: unreadable on the guest"))),
            Some(f) if !f.exists => return Ok(Some(format!("{src}: not there, nothing to move"))),
            Some(f) => {
                task.sources_before.insert(src, f.data);
            }
        }
    }
    for rel in attached.iter().collect::<BTreeSet<_>>() {
        match guestfile::read(&owner, rel) {
            None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
            Some(f) if !f.exists || f.data.len() > ATTACH_MAX_BYTES => {
                return Ok(Some(format!("{rel}: not there or over {ATTACH_MAX_BYTES} bytes, nothing to attach")));
            }
            Some(f) => {
                task.sources_before.insert(rel.clone(), f.data);
            }
        }
    }
    let exports: BTreeSet<String> = files.iter().filter_map(|c| c.export_of.as_ref()).map(|m| task.fmt(m)).collect::<Result<_>>()?;
    for src in exports {
        match guestfile::read(&owner, &src) {
            None => return Ok(Some(format!("{src}: unreadable on the guest"))),
            Some(f) if !f.exists => return Ok(Some(format!("{src}: not there, nothing to export"))),
            Some(f) => {
                task.sources_before.insert(src, f.data);
            }
        }
    }
    for c in files.iter().filter(|c| c.extract_of.is_some()) {
        let (rel, src) = (task.fmt(&c.path)?, task.fmt(c.extract_of.as_deref().unwrap_or(""))?);
        let Some(f) = guestfile::read(&owner, &src) else { return Ok(Some(format!("{src}: unreadable on the guest"))) };
        let member = if f.exists { zip_single(&f.data) } else { None };
        let Some((name, data)) = member.filter(|(n, _)| casefold(n) == casefold(&leaf(&rel))) else {
            return Ok(Some(format!("{src}: not an archive of exactly {}, nothing to extract", leaf(&rel))));
        };
        let _ = name;
        task.sources_before.insert(rel.clone(), data);
        let parent = rel.rsplit_once('\\').map(|(p, _)| p.to_string()).unwrap_or_default();
        let folder = guestfile::read(&owner, &parent);
        if folder.as_ref().is_some_and(|f| f.exists) {
            return Ok(Some(format!("{parent}: a file, not a folder to extract into")));
        }
        if folder.is_some() {
            absent.insert(rel);
        }
    }
    for c in files.iter().filter(|c| c.attachment_of.as_ref().is_some_and(|a| !a.is_empty())) {
        let a = c.attachment_of.as_ref().unwrap();
        let (rel, sender, subject) = (task.fmt(&c.path)?, task.fmt(&a[0])?, task.fmt(&a[1])?);
        let Some(got) = mail::matching(&owner, "inbox", &subject, true) else { return Ok(Some(format!("{subject:?}: inbox unreadable"))) };
        let address = format!("{}@{}", split_addr(sender.trim()).0, mail::domain());
        let hits: Vec<&mail::Message> = got.iter().filter(|m| m.sender.trim().to_lowercase() == address).collect();
        if hits.len() != 1 || hits[0].attachments.is_none() {
            return Ok(Some(format!("{subject:?} from {address}: not exactly one mail with readable attachments")));
        }
        if policy.mail_reserved(&hits[0].sender, &hits[0].subject, &hits[0].body) {
            return Ok(Some(format!("{subject:?} from {address}: scenario-reserved mail")));
        }
        let want = casefold(&leaf(&rel));
        let sha: Vec<&String> = hits[0].attachments.as_ref().unwrap().iter().filter(|(n, _)| *n == want).map(|(_, h)| h).collect();
        if sha.len() != 1 {
            return Ok(Some(format!("{subject:?}: no single attachment named {:?}", leaf(&rel))));
        }
        task.saved_sha.insert(rel, sha[0].clone());
    }
    let mut edits = BTreeMap::new();
    let mut appends = HashMap::new();
    for c in files.iter().filter(|c| c.append.is_some()) {
        let rel = task.fmt(&c.path)?;
        edits.insert(rel.clone(), task.fmt(c.source.as_deref().unwrap_or(&c.path))?);
        appends.insert(rel, c.append.clone().unwrap_or_default());
    }
    for (rel, src) in &edits {
        let f = guestfile::read(&owner, src);
        if f.as_ref().is_some_and(|f| !f.exists) {
            return Ok(Some(format!("{src}: not there, nothing to edit")));
        }
        let Some(f) = f else { return Ok(Some(format!("{src}: unreadable on the guest"))) };
        let Some(lines) = doc_lines(src, &f.data) else { return Ok(Some(format!("{src}: unreadable on the guest"))) };
        if rel.to_lowercase().ends_with(".xlsx")
            && let Some(why) = completes(&lines, &task.fmt(&appends[rel])?)
        {
            return Ok(Some(format!("{rel}: {why}")));
        }
        task.files_before.insert(rel.clone(), lines);
        if src != rel {
            task.sources_before.insert(src.clone(), f.data);
            match guestfile::read(&owner, rel) {
                None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
                Some(n) if n.exists => return Ok(Some(format!("{rel}: already exists, never overwritten"))),
                Some(_) => {
                    absent.insert(rel.clone());
                }
            }
        }
    }
    let mut commented: Vec<String> = Vec::new();
    for c in files.iter().filter(|c| c.comment.is_some()) {
        let rel = task.fmt(&c.path)?;
        if !commented.contains(&rel) {
            commented.push(rel);
        }
    }
    for rel in commented {
        let f = guestfile::read(&owner, &rel);
        if f.as_ref().is_some_and(|f| !f.exists) {
            return Ok(Some(format!("{rel}: not there, nothing to comment on")));
        }
        let (paras, notes) = match &f {
            Some(f) => (guestfile::docx_paragraphs(&f.data), guestfile::docx_comments(&f.data)),
            None => (None, None),
        };
        let (Some(paras), Some(notes)) = (paras, notes) else { return Ok(Some(format!("{rel}: unreadable on the guest"))) };
        task.files_before.insert(rel.clone(), paras.iter().map(|x| casefold(x)).collect());
        task.comments_before.insert(rel, notes);
    }
    let mut checked: Vec<String> = Vec::new();
    for c in files.iter().filter(|c| c.append.is_none() && c.comment.is_none() && !c.recycled && c.share.is_none()) {
        checked.push(task.fmt(&c.path)?);
    }
    if !task.inst.request.is_empty() {
        checked.extend(write_paths(task)?.into_iter().filter(|w| !w.ends_with(".*") && !edits.contains_key(w)));
    }
    let mut done_rel: Vec<String> = Vec::new();
    for rel in checked {
        if absent.contains(&rel) || done_rel.contains(&rel) {
            continue;
        }
        done_rel.push(rel.clone());
        match guestfile::read(&owner, &rel) {
            None => return Ok(Some(format!("{rel}: unreadable on the guest"))),
            Some(f) if f.exists => return Ok(Some(format!("{rel}: already exists, never overwritten"))),
            Some(_) => {
                absent.insert(rel);
            }
        }
    }
    task.files_absent = Some(absent);
    Ok(None)
}

fn file_ok(c: &FileCheck, task: &mut Task) -> Result<bool> {
    let rel = task.fmt(&c.path)?;
    let owner = task.owner.clone();
    if let Some(dest) = &c.share {
        let (dest, name) = (task.fmt(dest)?, leaf(&rel));
        if !task.files_absent.as_ref().is_some_and(|a| a.contains(&share::unc(&dest, &name))) {
            return Ok(false);
        }
        let (s, f) = (share::read(&dest, &name), guestfile::read(&owner, &rel));
        return Ok(matches!((s, f), (Some(s), Some(f)) if s.exists && f.exists && s.data == f.data));
    }
    if c.recycled {
        let f = guestfile::read(&owner, &rel);
        return Ok(task.files_present.contains(&rel) && f.is_some_and(|f| !f.exists));
    }
    if let Some(comment) = &c.comment {
        let (f, before) = (guestfile::read(&owner, &rel), task.comments_before.get(&rel));
        let (Some(f), Some(before)) = (f, before) else { return Ok(false) };
        if !f.exists || f.written_ms < task.started_ms {
            return Ok(false);
        }
        let (Some(paras), Some(notes)) = (guestfile::docx_paragraphs(&f.data), guestfile::docx_comments(&f.data)) else { return Ok(false) };
        if Some(&paras.iter().map(|x| casefold(x)).collect::<Vec<_>>()) != task.files_before.get(&rel) {
            return Ok(false);
        }
        let mut count: HashMap<&String, i64> = HashMap::new();
        for n in &notes {
            *count.entry(n).or_default() += 1;
        }
        for b in before {
            *count.entry(b).or_default() -= 1;
        }
        if count.values().any(|n| *n < 0) {
            return Ok(false);
        }
        let new: Vec<String> = notes
            .iter()
            .filter(|n| {
                let c = count.get_mut(n).unwrap();
                if *c > 0 {
                    *c -= 1;
                    true
                } else {
                    false
                }
            })
            .map(|x| casefold(&x.split_whitespace().collect::<Vec<_>>().join(" ")))
            .collect();
        let want = casefold(&task.fmt(comment)?.split_whitespace().collect::<Vec<_>>().join(" "));
        return Ok(new == [want]);
    }
    if let Some(append) = &c.append {
        if let Some(src) = &c.source {
            let src = task.fmt(src)?;
            let s = guestfile::read(&owner, &src);
            let unchanged = s.is_some_and(|s| s.exists && Some(&s.data) == task.sources_before.get(&src));
            if !task.files_absent.as_ref().is_some_and(|a| a.contains(&rel)) || !unchanged {
                return Ok(false);
            }
        }
        let f = guestfile::read(&owner, &rel);
        let Some(before) = task.files_before.get(&rel).cloned() else { return Ok(false) };
        let Some(f) = f.filter(|f| f.exists && f.written_ms >= task.started_ms) else { return Ok(false) };
        if rel.to_lowercase().ends_with(".xlsx") {
            let Some(got) = doc_lines(&rel, &f.data) else { return Ok(false) };
            let mut want = below(&before, &task.fmt(append)?);
            let gotset: BTreeSet<&String> = got.iter().collect();
            let beforeset: BTreeSet<&String> = before.iter().collect();
            let mut new: Vec<String> = gotset.difference(&beforeset).map(|x| fold_value(x)).collect();
            new.sort();
            want.sort();
            return Ok(!want.is_empty() && beforeset.is_subset(&gotset) && new == want);
        }
        let add = lines(&task.fmt(append)?);
        let ok = !add.is_empty() && doc_lines(&rel, &f.data) == Some([before, add].concat());
        let attaches: Vec<String> = task.template.attaches().iter().map(|a| task.fmt(a)).collect::<Result<_>>()?;
        if ok && task.sources_before.contains_key(&rel) && attaches.contains(&rel) {
            task.sources_before.insert(rel, f.data);
        }
        return Ok(ok);
    }
    if !task.files_absent.as_ref().is_some_and(|a| a.contains(&rel)) {
        return Ok(false);
    }
    let f = guestfile::read(&owner, &rel);
    if let Some(from) = &c.moved_from {
        let mut gone_paths: Vec<String> = Vec::new();
        for r in std::iter::once(from).chain(&task.template.removes) {
            if !gone_paths.contains(r) {
                gone_paths.push(r.clone());
            }
        }
        let mut all_gone = true;
        for r in gone_paths {
            all_gone &= guestfile::read(&owner, &task.fmt(&r)?).is_some_and(|s| !s.exists);
        }
        let from = task.fmt(from)?;
        let moved = f.is_some_and(|f| f.exists && Some(&f.data) == task.sources_before.get(&from));
        return Ok(moved && all_gone);
    }
    if let Some(name) = &c.download {
        let want = download_bytes(&task.fmt(name)?);
        return Ok(f.is_some_and(|f| f.exists && Some(f.data) == want));
    }
    if c.extract_of.is_some() {
        return Ok(f.is_some_and(|f| f.exists && Some(&f.data) == task.sources_before.get(&rel)));
    }
    if c.attachment_of.is_some() {
        return Ok(f.is_some_and(|f| f.exists && Some(&sha256_hex(&f.data)) == task.saved_sha.get(&rel)));
    }
    let Some(f) = f.filter(|f| f.exists && f.written_ms >= task.started_ms) else { return Ok(false) };
    if let Some(src) = &c.export_of {
        let src = task.fmt(src)?;
        let s = guestfile::read(&owner, &src);
        let unchanged = s.as_ref().is_some_and(|s| s.exists && Some(&s.data) == task.sources_before.get(&src));
        let sdata = s.map(|s| s.data).unwrap_or_default();
        let l = rel.to_lowercase();
        if l.ends_with(".zip") {
            return Ok(unchanged && zip_holds(&f.data, &leaf(&src), &sdata));
        }
        if l.ends_with(".xlsx") {
            let (cells, want) = (guestfile::xlsx_cells(&f.data), csv_cells(&sdata));
            return Ok(unchanged && !want.is_empty() && cells.is_some_and(|c| fold(&c) == fold(&want)));
        }
        let tail = &f.data[f.data.len().saturating_sub(1024)..];
        return Ok(unchanged && f.data.starts_with(b"%PDF-") && tail.windows(5).any(|w| w == b"%%EOF"));
    }
    if let Some(cells) = &c.cells {
        let (got, want) = (guestfile::xlsx_cells(&f.data), guestfile::grid_cells(&task.fmt(cells)?));
        return Ok(!want.is_empty() && got.is_some_and(|g| fold(&g) == fold(&want)));
    }
    if let Some(p) = &c.paragraphs {
        let want = lines(&task.fmt(p)?);
        return Ok(!want.is_empty() && doc_lines(&rel, &f.data) == Some(want));
    }
    let slides = guestfile::pptx_slides(&f.data);
    let mut want = Vec::new();
    for s in c.slides.iter().flatten() {
        let w = lines(&task.fmt(s)?);
        if !w.is_empty() {
            want.push(w);
        }
    }
    Ok(!want.is_empty() && slides.is_some_and(|s| s.iter().map(|x| x.iter().map(|y| casefold(y)).collect::<Vec<_>>()).collect::<Vec<_>>() == want))
}

#[cfg(feature = "intranet")]
fn download_bytes(name: &str) -> Option<Vec<u8>> {
    crate::intranet::download_bytes(name)
}

/// Without the intranet add-on a download is unknown, so its check never passes.
#[cfg(not(feature = "intranet"))]
fn download_bytes(_name: &str) -> Option<Vec<u8>> {
    None
}

fn zip_holds(data: &[u8], name: &str, want: &[u8]) -> bool {
    zip_single(data).is_some_and(|(n, d)| casefold(&n) == casefold(name) && d == want)
}

pub fn csv_cells(data: &[u8]) -> BTreeMap<String, String> {
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data);
    let Ok(text) = std::str::from_utf8(data) else { return BTreeMap::new() };
    let mut out = BTreeMap::new();
    let mut r = csv::ReaderBuilder::new().has_headers(false).flexible(true).from_reader(text.as_bytes());
    for (i, row) in r.records().enumerate() {
        let Ok(row) = row else { return BTreeMap::new() };
        for (j, c) in row.iter().enumerate() {
            if !c.is_empty() {
                out.insert(format!("{}{}", guestfile::column(j), i + 1), excel_value(c));
            }
        }
    }
    out
}

fn excel_value(cell: &str) -> String {
    if iso_date(cell)
        && let Ok(d) = chrono::NaiveDate::parse_from_str(cell, "%Y-%m-%d")
    {
        return (d - chrono::NaiveDate::from_ymd_opt(1899, 12, 30).unwrap()).num_days().to_string();
    }
    cell.to_string()
}

pub fn zip_single(data: &[u8]) -> Option<(String, Vec<u8>)> {
    let mut z = zip::ZipArchive::new(std::io::Cursor::new(data)).ok()?;
    if z.len() != 1 {
        return None;
    }
    let mut f = z.by_index(0).ok()?;
    if f.is_dir() {
        return None;
    }
    let mut buf = Vec::new();
    std::io::Read::read_to_end(&mut f, &mut buf).ok()?;
    Some((f.name().to_string(), buf))
}

pub fn leaf(rel: &str) -> String {
    rel.rsplit(['\\', '/']).next().unwrap_or("").to_string()
}

pub fn doc_lines(rel: &str, data: &[u8]) -> Option<Vec<String>> {
    let l = rel.to_lowercase();
    if l.ends_with(".xlsx") {
        return guestfile::xlsx_cells(data).map(|c| {
            let mut v: Vec<String> = c.iter().map(|(k, v)| format!("{k}\t{v}")).collect();
            v.sort();
            v
        });
    }
    if l.ends_with(".docx") {
        return guestfile::docx_paragraphs(data).map(|p| p.iter().map(|x| casefold(x)).collect());
    }
    let data = data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data);
    std::str::from_utf8(data).ok().map(|t| lines(&t.replace('\r', "")))
}

fn row(r: &str) -> i64 {
    r.trim_start_matches(|c: char| c.is_ascii_uppercase()).parse().unwrap_or(0)
}

fn col(r: &str) -> &str {
    r.trim_end_matches(|c: char| c.is_ascii_digit())
}

fn fold_value(cell: &str) -> String {
    match cell.split_once('\t') {
        Some((r, v)) => format!("{r}\t{}", casefold(v)),
        None => cell.into(),
    }
}

fn below(before: &[String], rows: &str) -> Vec<String> {
    let last = before.iter().map(|x| row(x.split('\t').next().unwrap_or(""))).max().unwrap_or(0);
    guestfile::grid_cells(rows).iter().map(|(k, v)| format!("{}{}\t{}", col(k), row(k) + last, casefold(v))).collect()
}

fn completes(before: &[String], rows: &str) -> Option<String> {
    let mut cols: HashMap<&str, BTreeSet<String>> = HashMap::new();
    for x in before {
        if let Some((r, v)) = x.split_once('\t') {
            cols.entry(col(r)).or_default().insert(casefold(v));
        }
    }
    for (r, v) in guestfile::grid_cells(rows) {
        let v = casefold(&v);
        let alpha = v.chars().next().is_some_and(char::is_alphabetic);
        if alpha && cols.get(col(&r)).is_some_and(|s| s.iter().any(|a| *a != v && a.starts_with(&v))) {
            return Some(format!("Excel would complete {v:?} to a longer cell above it"));
        }
    }
    None
}

pub fn lines(text: &str) -> Vec<String> {
    text.split('\n').filter(|x| !x.trim().is_empty()).map(casefold).collect()
}

fn fold(cells: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    cells.iter().map(|(k, v)| (k.clone(), casefold(v))).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn data_dir() -> std::path::PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR")).join("../../controller/data")
    }

    #[test]
    fn all_repo_templates_load() {
        // SAFETY: tests in this module only read LUDIUS_DATA.
        unsafe { std::env::set_var("LUDIUS_DATA", data_dir()) };
        let mut n = 0;
        for e in std::fs::read_dir(data_dir().join("templates")).unwrap() {
            let p = e.unwrap().path();
            load_template(&p).unwrap_or_else(|e| panic!("{}: {e:#}", p.display()));
            n += 1;
        }
        assert!(n > 30);
    }

    #[test]
    fn task_values() {
        unsafe { std::env::set_var("LUDIUS_DATA", data_dir()) };
        let t = load_template(&data_dir().join("templates/notepad_write_note.yaml")).unwrap();
        let inst = TaskInstance {
            template: t.task.clone(),
            params: [("file_name".to_string(), "q3 notes".to_string()), ("body".to_string(), "line one\nline two".to_string())].into_iter().collect(),
            variant: Some(0),
            ..Default::default()
        };
        let task = Task::new(t, inst, &mut rand::rng()).unwrap();
        assert_eq!(task.values["file_name_typo"], "q3 notesd");
        assert_eq!(task.values["body_head"], "line one");
        assert_eq!(task.values["file_name_stem"], "q3 notes");
        assert_eq!(task.subgoal_names[0], "open_from_start");
        assert_eq!(task.fmt_re("^{file_name}$").unwrap(), "^q3 notes$");
        assert_eq!(task.pixels_re("^{file_name}.{0,2}"), r"^q3notes.{0,2}");
        assert!(task.fmt("{nope}").is_err());
    }

    #[test]
    fn file_check_rules() {
        let ok = FileCheck { path: "Documents\\a.txt".into(), paragraphs: Some("x".into()), ..Default::default() };
        assert!(ok.validate().is_ok());
        let two = FileCheck { cells: Some("x".into()), ..ok.clone() };
        assert!(two.validate().is_err());
        let outside = FileCheck { path: "Desktop\\a.txt".into(), ..ok };
        assert!(outside.validate().is_err());
        let comment = FileCheck { path: "Documents\\a.docx".into(), comment: Some("c".into()), ..Default::default() };
        assert!(comment.validate().is_ok());
        let px = Done { pixels: Some(PixelsCheck { present: vec![], absent: vec![] }), ..Default::default() };
        assert!(px.validate().is_err());
        let sent: Done = serde_yaml::from_str("mail: {mailbox: m, folder: Sent}").unwrap();
        assert!(sent.validate().unwrap_err().to_string().contains("inbox or sent"));
    }

    #[test]
    fn helpers() {
        assert_eq!(leaf("Documents\\x\\a.txt"), "a.txt");
        assert_eq!(lines("A\n\n b \n"), vec!["a", " b "]);
        assert_eq!(excel_value("2026-01-01"), "46023");
        assert_eq!(below(&["A1\tx".into(), "A2\ty".into()], "New"), vec!["A3\tnew"]);
        assert!(completes(&["A1\tLondon".into()], "Lon").is_some());
        assert!(completes(&["A1\tLondon".into()], "Paris").is_none());
        assert_eq!(neighbour('s'), 'd');
        assert_eq!(neighbour('P'), 'Q');
        let at = local_ms("2026-10-31", "09:00", "Europe/Stockholm").unwrap();
        assert_eq!(at, 1_793_433_600_000);
    }
}
