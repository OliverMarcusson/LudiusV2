//! Task templates, their plan-load validators, personas and planned instances.
use super::*;

pub(crate) fn save_extension(app: &str) -> Option<&'static str> {
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

pub(crate) fn d_budget_steps() -> u32 {
    40
}
pub(crate) fn d_budget_minutes() -> f64 {
    8.0
}
pub(crate) fn d_abandon() -> String {
    "close_without_saving".into()
}
pub(crate) fn d_weight() -> f64 {
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

    pub(crate) fn dones(&self) -> impl Iterator<Item = &Done> {
        self.subgoals.values().filter_map(|s| s.done.as_ref())
    }

    /// Pydantic's after-validators, in their order; some fill in derived fields.
    pub(crate) fn validate(&mut self) -> Result<()> {
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
pub(crate) fn d_hours() -> (String, String) {
    ("08:30".into(), "17:00".into())
}
pub(crate) fn d_typing() -> u32 {
    140
}
pub(crate) fn d_jitter() -> u32 {
    60
}
pub(crate) fn d_think() -> f64 {
    1.6
}
pub(crate) fn d_pointer() -> (u32, u32) {
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
