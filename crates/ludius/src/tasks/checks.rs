//! Done checks, file checks and the capability tables.
use super::*;

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

pub(crate) fn d_subject() -> String {
    "{subject}".into()
}
pub(crate) fn d_body() -> String {
    "{body}".into()
}
pub(crate) fn d_to() -> String {
    "{to}".into()
}
pub(crate) fn d_inbox() -> String {
    "inbox".into()
}
pub(crate) fn d_true() -> bool {
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
pub(crate) fn message_default() -> String {
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
pub(crate) fn date_default() -> String {
    "{date}".into()
}
pub(crate) fn start_default() -> String {
    "{start}".into()
}
pub(crate) fn end_default() -> String {
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
pub(crate) fn response_default() -> String {
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

pub(crate) fn noise(rel: &str) -> bool {
    profile_path("u", rel).is_some()
}

pub(crate) fn last5(s: &str) -> String {
    let l: Vec<char> = s.to_lowercase().chars().collect();
    l[l.len().saturating_sub(5)..].iter().collect()
}

pub(crate) fn ends(s: &str, exts: &[&str]) -> bool {
    let l = s.to_lowercase();
    exts.iter().any(|e| l.ends_with(e))
}

impl FileCheck {
    pub(crate) fn set(&self, k: &str) -> bool {
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

    pub(crate) fn any(&self, keys: &[&str]) -> bool {
        keys.iter().any(|k| self.set(k))
    }

    pub fn validate(&self) -> Result<()> {
        pub(crate) const NEED: [&str; 3] = ["cells", "paragraphs", "slides"];
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

    pub(crate) fn outside(&self) -> bool {
        self.mail.is_some()
            || self.invite.is_some()
            || self.response.is_some()
            || self.file.is_some()
            || self.read
            || self.filed
            || self.chat_file.is_some()
            || self.chat_post.is_some()
    }

    pub(crate) fn on_screen(&self) -> bool {
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

pub(crate) fn d_steps() -> u32 {
    12
}
pub(crate) fn d_end() -> String {
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
