//! Template text: `{name}` placeholders, patterns with lookahead (fancy-regex), casefold.
use std::cell::RefCell;
use std::collections::HashMap;

use anyhow::{Result, anyhow, bail};
use fancy_regex::Regex;

/// `str.format_map`: `{name}` is looked up, `{{`/`}}` are literal braces; a missing name is an error.
pub fn format_map(s: &str, get: impl Fn(&str) -> Option<String>) -> Result<String> {
    let mut out = String::with_capacity(s.len());
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        match c {
            '{' if it.peek() == Some(&'{') => {
                it.next();
                out.push('{');
            }
            '{' => {
                let mut name = String::new();
                loop {
                    match it.next() {
                        Some('}') => break,
                        Some(c) => name.push(c),
                        None => bail!("Single '{{' encountered in format string"),
                    }
                }
                out.push_str(&get(&name).ok_or_else(|| anyhow!("KeyError: {name:?}"))?);
            }
            '}' if it.peek() == Some(&'}') => {
                it.next();
                out.push('}');
            }
            '}' => bail!("Single '}}' encountered in format string"),
            c => out.push(c),
        }
    }
    Ok(out)
}

pub fn format(s: &str, map: &HashMap<String, String>) -> Result<String> {
    format_map(s, |k| map.get(k).cloned())
}

thread_local! {
    static CACHE: RefCell<HashMap<(String, bool), Option<Regex>>> = RefCell::new(HashMap::new());
}

/// A compiled Python-style pattern; `icase` is re.I. Invalid patterns are an error, like re.error.
pub fn re(pattern: &str, icase: bool) -> Result<Regex> {
    CACHE.with(|c| {
        let mut c = c.borrow_mut();
        let key = (pattern.to_string(), icase);
        if let Some(r) = c.get(&key) {
            return r.clone().ok_or_else(|| anyhow!("bad pattern {pattern:?}"));
        }
        let full = if icase { format!("(?i){pattern}") } else { pattern.to_string() };
        let r = Regex::new(&full).ok();
        if c.len() > 4096 {
            c.clear();
        }
        c.insert(key, r.clone());
        r.ok_or_else(|| anyhow!("bad pattern {pattern:?}"))
    })
}

/// `re.search(pattern, text, flags)`; a pattern error or a backtrack limit is no match.
pub fn search(pattern: &str, text: &str, icase: bool) -> bool {
    re(pattern, icase).ok().and_then(|r| r.is_match(text).ok()).unwrap_or(false)
}

pub fn fullmatch(pattern: &str, text: &str, icase: bool) -> bool {
    search(&format!(r"\A(?:{pattern})\z"), text, icase)
}

pub fn escape(s: &str) -> String {
    fancy_regex::escape(s).into_owned()
}

/// `str.casefold` (to_lowercase plus the folds Python adds for German and Greek).
pub fn casefold(s: &str) -> String {
    s.to_lowercase().replace('ß', "ss").replace('ς', "σ")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn format_like_python() {
        let m: HashMap<String, String> = [("a".into(), "x".into())].into();
        assert_eq!(format("{a}-{{b}}", &m).unwrap(), "x-{b}");
        assert!(format("{b}", &m).is_err());
        assert!(format("a}", &m).is_err());
    }

    #[test]
    fn lookahead_and_fullmatch() {
        assert!(search(r"^(?!foo$).*", "bar", false));
        assert!(!search(r"^(?!foo$).*$", "foo", false));
        assert!(fullmatch("a|ab", "ab", false));
        assert!(search("ABC", "xabc", true));
    }
}
