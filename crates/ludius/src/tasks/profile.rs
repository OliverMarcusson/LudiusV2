//! App profiles: an app's yaml merged over default.yaml.
use super::*;

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
pub(crate) fn value_default() -> String {
    "value".into()
}

impl Form {
    pub(crate) fn validate(&self) -> Result<()> {
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
    pub(crate) fn validate(&self) -> Result<()> {
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

pub(crate) fn merge(base: Value, own: Value) -> Value {
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
