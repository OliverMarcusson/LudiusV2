//! Guest files in the noise folders, read through the host agent; Office parts for outcome checks.
use std::collections::BTreeMap;
use std::io::{Cursor, Read};

use roxmltree::{Document, Node};

use crate::hostagent::HostAgent;

pub const MAX_BYTES: u32 = 1_000_000;
pub const NOISE_FOLDERS: [&str; 3] = ["Documents", "Pictures", "Downloads"];
const MAX_PART: u64 = 5_000_000;

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct GuestFile {
    pub exists: bool,
    pub written_ms: i64,
    pub data: Vec<u8>,
}

impl GuestFile {
    pub fn missing() -> Self {
        Self::default()
    }
}

pub fn split(rel: &str) -> Vec<&str> {
    rel.split(['\\', '/']).collect()
}

pub fn profile_path(user: &str, rel: &str) -> Option<String> {
    let parts = split(rel);
    let user_ok = !user.is_empty() && user.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit());
    if !user_ok || parts.len() < 2 || !NOISE_FOLDERS.contains(&parts[0]) {
        return None;
    }
    if parts.iter().any(|p| matches!(*p, "" | "." | "..") || p.contains(['<', '>', ':', '"', '|', '?', '*'])) {
        return None;
    }
    Some(format!("C:\\Users\\{user}\\{}", parts.join("\\")))
}

/// None means unknown (unreadable, no VM, agent error): never read as "not there".
pub fn read(user: &str, rel: &str) -> Option<GuestFile> {
    let path = profile_path(user, rel)?;
    let vm = std::env::var("LUDIUS_VM").ok().filter(|v| !v.is_empty())?;
    // ponytail: the Hyper-V PowerShell fallback (agent without ReadGuestFile) is not ported; the Rust agent has it.
    let f = HostAgent::from_env(&vm).ok()?.read_file(&path, MAX_BYTES).ok()?;
    if !f.exists {
        return Some(GuestFile::missing());
    }
    if f.data.len() > MAX_BYTES as usize {
        return None;
    }
    Some(GuestFile { exists: true, written_ms: f.written_ms, data: f.data })
}

type Zip<'a> = zip::ZipArchive<Cursor<&'a [u8]>>;

fn open(data: &[u8]) -> Option<(Zip<'_>, Vec<String>)> {
    let mut z = zip::ZipArchive::new(Cursor::new(data)).ok()?;
    let mut names = Vec::new();
    for i in 0..z.len() {
        let f = z.by_index(i).ok()?;
        if f.size() > MAX_PART {
            return None;
        }
        names.push(f.name().to_string());
    }
    Some((z, names))
}

fn part(z: &mut Zip<'_>, name: &str) -> Option<String> {
    let mut s = String::new();
    z.by_name(name).ok()?.read_to_string(&mut s).ok()?;
    Some(s)
}

const M: &str = "http://schemas.openxmlformats.org/spreadsheetml/2006/main";
const W: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";
const A: &str = "http://schemas.openxmlformats.org/drawingml/2006/main";
const P: &str = "http://schemas.openxmlformats.org/presentationml/2006/main";
const REL: &str = "http://schemas.openxmlformats.org/officeDocument/2006/relationships";
const PKG_REL: &str = "http://schemas.openxmlformats.org/package/2006/relationships";

fn is(n: &Node, ns: &str, name: &str) -> bool {
    n.is_element() && n.tag_name().namespace() == Some(ns) && n.tag_name().name() == name
}

fn child<'a, 'i>(n: Node<'a, 'i>, ns: &str, name: &str) -> Option<Node<'a, 'i>> {
    n.children().find(|c| is(c, ns, name))
}

fn texts(n: Node, ns: &str) -> String {
    n.descendants().filter(|e| is(e, ns, "t")).map(|e| e.text().unwrap_or("")).collect()
}

/// Paragraph text: runs joined, a tab element as "\t" (the Python `_runs`).
fn runs(p: Node, ns: &str) -> String {
    p.descendants()
        .filter_map(|e| {
            if is(&e, ns, "t") {
                Some(e.text().unwrap_or(""))
            } else if is(&e, ns, "tab") {
                Some("\t")
            } else {
                None
            }
        })
        .collect()
}

pub fn xlsx_cells(data: &[u8]) -> Option<BTreeMap<String, String>> {
    let (mut z, names) = open(data)?;
    let sheets: Vec<&String> = names
        .iter()
        .filter(|n| n.strip_prefix("xl/worksheets/sheet").and_then(|r| r.strip_suffix(".xml")).is_some_and(|d| !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())))
        .collect();
    if sheets.len() != 1 {
        return None;
    }
    let sheet = sheets[0].clone();
    let mut shared = Vec::new();
    if names.iter().any(|n| n == "xl/sharedStrings.xml") {
        let xml = part(&mut z, "xl/sharedStrings.xml")?;
        let doc = Document::parse(&xml).ok()?;
        shared = doc.root_element().children().filter(|c| is(c, M, "si")).map(|si| texts(si, M)).collect();
    }
    let xml = part(&mut z, &sheet)?;
    let doc = Document::parse(&xml).ok()?;
    let mut cells = BTreeMap::new();
    let data_el = child(doc.root_element(), M, "sheetData")?;
    for row in data_el.children().filter(|c| is(c, M, "row")) {
        for c in row.children().filter(|c| is(c, M, "c")) {
            if child(c, M, "f").is_some() {
                return None;
            }
            let kind = c.attribute("t");
            let v = child(c, M, "v");
            let text = if kind == Some("inlineStr") {
                texts(c, M)
            } else if let Some(v) = v {
                let t = v.text().unwrap_or("");
                match kind {
                    Some("b") => match t {
                        "1" => "TRUE".into(),
                        "0" => "FALSE".into(),
                        _ => return None,
                    },
                    Some("s") => shared.get(t.parse::<usize>().ok()?)?.clone(),
                    _ => t.to_string(),
                }
            } else {
                continue;
            };
            if !text.is_empty() {
                cells.insert(c.attribute("r").unwrap_or("").to_string(), text);
            }
        }
    }
    Some(cells)
}

pub fn docx_paragraphs(data: &[u8]) -> Option<Vec<String>> {
    let (mut z, names) = open(data)?;
    if !names.iter().any(|n| n == "word/document.xml") {
        return None;
    }
    let xml = part(&mut z, "word/document.xml")?;
    let doc = Document::parse(&xml).ok()?;
    let body = child(doc.root_element(), W, "body")?;
    Some(body.descendants().filter(|e| is(e, W, "p")).map(|p| runs(p, W)).filter(|t| !t.trim().is_empty()).collect())
}

pub fn docx_comments(data: &[u8]) -> Option<Vec<String>> {
    let (mut z, names) = open(data)?;
    if !names.iter().any(|n| n == "word/document.xml") {
        return None;
    }
    if !names.iter().any(|n| n == "word/comments.xml") {
        return Some(vec![]);
    }
    let xml = part(&mut z, "word/comments.xml")?;
    let doc = Document::parse(&xml).ok()?;
    Some(
        doc.descendants()
            .filter(|e| is(e, W, "comment"))
            .map(|c| {
                let ps: Vec<String> =
                    c.descendants().filter(|e| is(e, W, "p")).map(|p| runs(p, W).trim().to_string()).filter(|x| !x.is_empty()).collect();
                ps.join(" ")
            })
            .collect(),
    )
}

pub fn pptx_slides(data: &[u8]) -> Option<Vec<Vec<String>>> {
    let (mut z, names) = open(data)?;
    let has = |n: &str| names.iter().any(|x| x == n);
    if !has("ppt/presentation.xml") || !has("ppt/_rels/presentation.xml.rels") {
        return None;
    }
    let rels_xml = part(&mut z, "ppt/_rels/presentation.xml.rels")?;
    let rels_doc = Document::parse(&rels_xml).ok()?;
    let rels: BTreeMap<&str, &str> = rels_doc
        .descendants()
        .filter(|e| is(e, PKG_REL, "Relationship"))
        .map(|r| (r.attribute("Id").unwrap_or(""), r.attribute("Target").unwrap_or("")))
        .collect();
    let pres_xml = part(&mut z, "ppt/presentation.xml")?;
    let pres = Document::parse(&pres_xml).ok()?;
    let mut slides = Vec::new();
    for sid in pres.descendants().filter(|e| is(e, P, "sldId")) {
        let target = rels.get(sid.attribute((REL, "id"))?)?.trim_start_matches('/');
        let name = if target.starts_with("ppt/") { target.to_string() } else { format!("ppt/{target}") };
        let xml = part(&mut z, &name)?;
        let doc = Document::parse(&xml).ok()?;
        let (mut title, mut rest) = (Vec::new(), Vec::new());
        for sp in doc.descendants().filter(|e| is(e, P, "sp")) {
            let ph = child(sp, P, "nvSpPr").and_then(|n| child(n, P, "nvPr")).and_then(|n| child(n, P, "ph"));
            let t: Vec<String> = sp.descendants().filter(|e| is(e, A, "p")).map(|p| runs(p, A)).filter(|x| !x.trim().is_empty()).collect();
            if ph.is_some_and(|ph| matches!(ph.attribute("type"), Some("title" | "ctrTitle"))) {
                title.extend(t);
            } else {
                rest.extend(t);
            }
        }
        title.extend(rest);
        slides.push(title);
    }
    Some(slides)
}

pub fn grid_cells(rows: &str) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    for (r, line) in rows.trim_matches('\n').split('\n').enumerate() {
        for (c, cell) in line.split('\t').enumerate() {
            if !cell.is_empty() {
                out.insert(format!("{}{}", column(c), r + 1), cell.to_string());
            }
        }
    }
    out
}

pub fn column(i: usize) -> String {
    let mut s = Vec::new();
    let mut i = i + 1;
    while i > 0 {
        let rem = (i - 1) % 26;
        i = (i - 1) / 26;
        s.push(b'A' + rem as u8);
    }
    s.reverse();
    String::from_utf8(s).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn noise_paths() {
        assert_eq!(profile_path("aberg", "Documents\\a.txt").as_deref(), Some("C:\\Users\\aberg\\Documents\\a.txt"));
        assert!(profile_path("aberg", "Desktop\\a.txt").is_none());
        assert!(profile_path("aberg", "Documents\\..\\x").is_none());
        assert!(profile_path("Aberg", "Documents\\a.txt").is_none());
        assert!(profile_path("aberg", "Documents").is_none());
    }

    #[test]
    fn grid() {
        assert_eq!(column(0), "A");
        assert_eq!(column(26), "AA");
        let g = grid_cells("a\t\tc\nd");
        assert_eq!(g.get("C1").map(String::as_str), Some("c"));
        assert_eq!(g.get("A2").map(String::as_str), Some("d"));
        assert!(!g.contains_key("B1"));
    }

    #[test]
    fn not_a_zip_is_unknown() {
        assert!(xlsx_cells(b"nope").is_none());
        assert!(docx_paragraphs(b"nope").is_none());
    }
}
