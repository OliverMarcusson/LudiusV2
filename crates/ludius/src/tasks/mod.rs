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

mod checks;
mod profile;
mod templates;
mod screen;
mod outcome;
mod docs;
pub use checks::*;
pub use profile::*;
pub use templates::*;
pub use screen::*;
pub use outcome::*;
pub use docs::*;

pub const HEAD: usize = 24;
pub(crate) const ROWS: [&str; 3] = ["qwertyuiop", "asdfghjkl", "zxcvbnm"];

pub(crate) fn neighbour(c: char) -> char {
    let l = c.to_ascii_lowercase();
    let Some(row) = ROWS.iter().find(|r| r.contains(l)) else { return c };
    let b = row.as_bytes();
    let i = row.find(l).unwrap();
    let n = b[(i + 1) % b.len()] as char;
    if c.is_uppercase() { n.to_ascii_uppercase() } else { n }
}

pub(crate) fn iso_date(v: &str) -> bool {
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
