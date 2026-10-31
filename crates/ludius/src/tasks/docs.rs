//! Document helpers: lines, cells, csv, zip.
use super::*;

pub(crate) fn zip_holds(data: &[u8], name: &str, want: &[u8]) -> bool {
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

pub(crate) fn excel_value(cell: &str) -> String {
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

pub(crate) fn row(r: &str) -> i64 {
    r.trim_start_matches(|c: char| c.is_ascii_uppercase()).parse().unwrap_or(0)
}

pub(crate) fn col(r: &str) -> &str {
    r.trim_end_matches(|c: char| c.is_ascii_digit())
}

pub(crate) fn fold_value(cell: &str) -> String {
    match cell.split_once('\t') {
        Some((r, v)) => format!("{r}\t{}", casefold(v)),
        None => cell.into(),
    }
}

pub(crate) fn below(before: &[String], rows: &str) -> Vec<String> {
    let last = before.iter().map(|x| row(x.split('\t').next().unwrap_or(""))).max().unwrap_or(0);
    guestfile::grid_cells(rows).iter().map(|(k, v)| format!("{}{}\t{}", col(k), row(k) + last, casefold(v))).collect()
}

pub(crate) fn completes(before: &[String], rows: &str) -> Option<String> {
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

pub(crate) fn fold(cells: &BTreeMap<String, String>) -> BTreeMap<String, String> {
    cells.iter().map(|(k, v)| (k.clone(), casefold(v))).collect()
}
