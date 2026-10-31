//! Intranet pages (markdown with a header block) and their downloadable files.
use std::path::{Path, PathBuf};

use anyhow::{Result, bail};


pub const MAX_PAGE: u64 = 64 << 10;
pub const MAX_FILE: u64 = 1_000_000;
pub const MAX_FILES: u64 = 50 << 20;
pub const MAX_PAGES: usize = 200;
pub const RANGE_DOMAIN: &str = "corp.ludius.test";
const FIELDS: [&str; 7] = ["title", "section", "owner", "updated", "popular", "tags", "files"];
const FILE_RE: &str = r"(?i)[A-Za-z0-9][A-Za-z0-9 ._-]{0,79}\.(?:pdf|docx|xlsx|pptx|txt|csv|png|jpg)";

/// LUDIUS_INTRANET, else intranet/ under LUDIUS_DATA (default controller/data).
pub fn dir() -> PathBuf {
    if let Some(d) = std::env::var_os("LUDIUS_INTRANET") {
        return PathBuf::from(d);
    }
    let data = std::env::var_os("LUDIUS_DATA").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("controller").join("data"));
    data.join("intranet")
}

fn fullmatch(pattern: &str, text: &str) -> bool {
    regex::Regex::new(&format!(r"\A(?:{pattern})\z")).is_ok_and(|r| r.is_match(text))
}

#[derive(Debug, Clone, PartialEq)]
pub struct Page {
    pub id: String,
    pub title: String,
    pub section: String,
    pub owner: String,
    pub updated: String,
    pub popular: bool,
    pub tags: Vec<String>,
    pub body: Vec<String>,
    pub files: Vec<String>,
}

fn url_host(url: &str) -> String {
    let rest = url.split_once("//").map(|(_, r)| r).unwrap_or("");
    let auth = rest.split(['/', '?', '#']).next().unwrap_or("");
    let auth = auth.rsplit('@').next().unwrap_or("");
    let host = if let Some(v6) = auth.strip_prefix('[') { v6.split(']').next().unwrap_or("") } else { auth.split(':').next().unwrap_or("") };
    host.to_lowercase().trim_end_matches('.').to_string()
}

pub fn parse(id: &str, text: &str) -> Result<Page> {
    if !fullmatch("[a-z0-9][a-z0-9-]{0,63}", id) {
        bail!("{id}.md: file name must be lowercase letters, digits and dashes");
    }
    let norm = text.replace("\r\n", "\n");
    let lines: Vec<&str> = norm.split('\n').collect();
    let Some(end) = lines.iter().skip(1).position(|l| *l == "---").map(|i| i + 1).filter(|_| lines[0] == "---") else {
        bail!("{id}.md: must start with a --- header --- block");
    };
    let mut head = std::collections::BTreeMap::new();
    for line in &lines[1..end] {
        match line.split_once(": ") {
            Some((k, v)) if FIELDS.contains(&k) && !head.contains_key(k) => {
                head.insert(k, v.trim().to_string());
            }
            _ => bail!("{id}.md: header line {line:?} is not one of {FIELDS:?} as 'key: value' once"),
        }
    }
    let missing: Vec<_> = ["owner", "section", "title", "updated"].into_iter().filter(|k| !head.contains_key(k)).collect();
    if !missing.is_empty() {
        bail!("{id}.md: header misses {missing:?}");
    }
    if chrono::NaiveDate::parse_from_str(&head["updated"], "%Y-%m-%d").is_err() {
        bail!("{id}.md: updated must be YYYY-MM-DD");
    }
    let popular = head.get("popular").map(String::as_str).unwrap_or("false");
    if popular != "true" && popular != "false" {
        bail!("{id}.md: popular must be true or false");
    }
    let rest = lines[end + 1..].join("\n");
    let paras = regex::Regex::new(r"\n\s*\n").unwrap();
    let body: Vec<String> =
        paras.split(&rest).filter(|p| !p.trim().is_empty()).map(|p| p.split_whitespace().collect::<Vec<_>>().join(" ")).collect();
    if body.is_empty() {
        bail!("{id}.md: no body text");
    }
    let urls = regex::Regex::new(r#"(?i)(?:[a-z][a-z0-9+.-]*:)?//[^\s"'<>()]+"#).unwrap();
    for m in urls.find_iter(text) {
        let host = url_host(m.as_str());
        if host != RANGE_DOMAIN && !host.ends_with(&format!(".{RANGE_DOMAIN}")) {
            bail!("{id}.md: link to {host} outside {RANGE_DOMAIN}");
        }
    }
    let list = |k: &str| -> Vec<String> {
        head.get(k).map(|s| s.split(',').map(str::trim).filter(|t| !t.is_empty()).map(String::from).collect()).unwrap_or_default()
    };
    let files = list("files");
    let bad: Vec<_> = files.iter().filter(|f| !fullmatch(FILE_RE, f) || f.contains("..")).collect();
    if !bad.is_empty() {
        bail!("{id}.md: files {bad:?}: letters, digits, space . _ - and a .pdf/docx/xlsx/pptx/txt/csv/png/jpg end");
    }
    Ok(Page {
        id: id.into(),
        title: head["title"].clone(),
        section: head["section"].clone(),
        owner: head["owner"].clone(),
        updated: head["updated"].clone(),
        popular: popular == "true",
        tags: list("tags"),
        body,
        files,
    })
}

pub fn load(folder: &Path) -> Result<Vec<Page>> {
    let mut files: Vec<PathBuf> = std::fs::read_dir(folder)?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| p.extension().is_some_and(|x| x == "md"))
        .collect();
    files.sort();
    if files.is_empty() {
        bail!("{}: no .md pages", folder.display());
    }
    if files.len() > MAX_PAGES {
        bail!("{}: {} pages, at most {MAX_PAGES}", folder.display(), files.len());
    }
    let mut pages = Vec::new();
    for f in &files {
        let name = f.file_name().unwrap().to_string_lossy();
        if std::fs::metadata(f)?.len() > MAX_PAGE {
            bail!("{name}: over {} KiB", MAX_PAGE >> 10);
        }
        pages.push(parse(&f.file_stem().unwrap().to_string_lossy(), &std::fs::read_to_string(f)?)?);
    }
    if !pages.iter().any(|p| p.popular) {
        bail!("{}: no page is popular: true (the home page would be empty)", folder.display());
    }
    let named: std::collections::BTreeSet<&String> = pages.iter().flat_map(|p| &p.files).collect();
    let mut there = std::collections::BTreeMap::new();
    if let Ok(rd) = std::fs::read_dir(folder.join("files")) {
        for e in rd.flatten() {
            if e.path().is_file() {
                there.insert(e.file_name().to_string_lossy().to_string(), e.metadata()?.len());
            }
        }
    }
    let missing: Vec<_> = named.iter().filter(|n| !there.contains_key(n.as_str())).collect();
    if !missing.is_empty() {
        bail!("{}: files {missing:?} named on a page but not in files/", folder.display());
    }
    let unused: Vec<_> = there.keys().filter(|n| !named.contains(n)).collect();
    if !unused.is_empty() {
        bail!("{}: files/{unused:?} not on any page (it would be served unlisted)", folder.display());
    }
    for (n, size) in &there {
        if *size > MAX_FILE {
            bail!("files/{n}: over {MAX_FILE} bytes");
        }
    }
    if there.values().sum::<u64>() > MAX_FILES {
        bail!("{}: files/ over {} MiB together", folder.display(), MAX_FILES >> 20);
    }
    Ok(pages)
}

pub fn index(folder: &Path, skip: impl Fn(&str) -> bool) -> Result<String> {
    Ok(load(folder)?
        .iter()
        .filter(|p| !skip(&p.title))
        .map(|p| {
            let tags = if p.tags.is_empty() { String::new() } else { format!(" ({})", p.tags.iter().take(8).cloned().collect::<Vec<_>>().join(", ")) };
            format!("- {}{tags}", p.title)
        })
        .collect::<Vec<_>>()
        .join("\n"))
}

pub fn downloads(folder: &Path, skip: impl Fn(&str) -> bool) -> Result<Vec<(String, String)>> {
    Ok(load(folder)?.iter().filter(|p| !skip(&p.title)).flat_map(|p| p.files.iter().map(|f| (f.clone(), p.title.clone()))).collect())
}

pub fn download_bytes(name: &str) -> Option<Vec<u8>> {
    let folder = dir();
    let listed = downloads(&folder, |_| false).ok()?.iter().any(|(f, _)| f == name);
    if listed { std::fs::read(folder.join("files").join(name)).ok() } else { None }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_and_checks_links() {
        let ok = "---\ntitle: T\nsection: S\nowner: O\nupdated: 2026-01-02\nfiles: a.pdf\n---\nHello  there\n\nSecond https://intranet.corp.ludius.test/x\n";
        let p = parse("a-page", ok).unwrap();
        assert_eq!(p.body, vec!["Hello there", "Second https://intranet.corp.ludius.test/x"]);
        assert_eq!(p.files, vec!["a.pdf"]);
        assert!(parse("a", &ok.replace("intranet.corp.ludius.test", "example.com")).is_err());
        assert!(parse("A", ok).is_err());
        assert!(parse("a", &ok.replace("updated: 2026-01-02", "updated: soon")).is_err());
    }

    #[test]
    fn repo_intranet_loads() {
        let d = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../controller/data/intranet");
        assert!(!load(&d).unwrap().is_empty());
    }
}
