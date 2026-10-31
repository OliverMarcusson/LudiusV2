//! Forensic artifact coverage and scenario survival from a psort timeline export (port of artifacts.py).
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Context, Result, bail};
use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};
use regex::Regex;
use serde_json::{Map, Value, json};

const DYNAMIC_REQUIRED: [&str; 4] = ["datetime", "parser", "display_name", "message"];
const L2T_REQUIRED: [&str; 9] = ["date", "time", "timezone", "MACB", "user", "host", "desc", "filename", "format"];
const L2T_RENAME: [(&str, &str); 4] = [("desc", "message"), ("filename", "display_name"), ("format", "parser"), ("sourcetype", "source_long")];
pub const MATCH_FIELDS: [&str; 6] = ["display_name", "message", "parser", "source_long", "user", "host"];

fn rx(s: &str) -> Regex {
    Regex::new(&format!("(?i){s}")).unwrap()
}

static USER_TYPES: LazyLock<Vec<(&str, Regex)>> = LazyLock::new(|| {
    [
        ("jump_list", r"automatic_destinations|custom_destinations"),
        ("userassist", r"userassist"),
        ("shellbags", r"bagmru|shellbag"),
        ("office_mru", r"office_mru"),
        ("mail", r"outlook|\bpst\b|\bost\b|mail"),
        ("browser_history", r"history|webcache"),
        ("lnk", r"(^|/)lnk\b"),
    ]
    .into_iter()
    .map(|(n, r)| (n, rx(r)))
    .collect()
});
static HOST_TYPES: LazyLock<Vec<(&str, Regex)>> = LazyLock::new(|| vec![("prefetch", rx("prefetch"))]);
static PROFILE: LazyLock<Regex> = LazyLock::new(|| rx(r"[\\/]Users[\\/]([^\\/]+)[\\/]"));

#[derive(Debug, Clone, Default)]
pub struct Row {
    pub fields: BTreeMap<&'static str, String>,
    pub time: Option<DateTime<Utc>>,
}

impl Row {
    fn get(&self, f: &str) -> &str {
        self.fields.get(f).map_or("", String::as_str)
    }
}

fn clean(v: &str) -> String {
    let v = v.trim();
    let b = v.as_bytes();
    let v = if b.len() > 2 && b[0] == b'\'' && b"=+-@".contains(&b[1]) { &v[1..] } else { v };
    if v == "-" { String::new() } else { v.into() }
}

fn time_dynamic(v: &str) -> Option<DateTime<Utc>> {
    if v.is_empty() || v.starts_with("0000-00-00") || v == "Invalid" {
        return None;
    }
    // naive times have no zone and stay untimed, as in Python
    DateTime::parse_from_rfc3339(v).or_else(|_| DateTime::parse_from_str(v, "%Y-%m-%d %H:%M:%S%.f%:z")).ok().map(|t| t.to_utc())
}

fn time_l2t(date: &str, time: &str, zone: &str) -> Option<DateTime<Utc>> {
    if matches!(date, "" | "00/00/0000") || matches!(time, "" | "--:--:--") {
        return None;
    }
    let naive = NaiveDateTime::parse_from_str(&format!("{date} {time}"), "%m/%d/%Y %H:%M:%S").ok()?;
    if zone.eq_ignore_ascii_case("UTC") {
        return Some(naive.and_utc());
    }
    zone.parse::<chrono_tz::Tz>().ok()?.from_local_datetime(&naive).earliest().map(|t| t.to_utc())
}

pub fn read_timeline(path: &Path) -> Result<Vec<Row>> {
    let mut r = csv::Reader::from_path(path).with_context(|| path.display().to_string())?;
    let header: Vec<String> = r.headers()?.iter().map(|h| h.trim_start_matches('\u{feff}').to_string()).collect();
    let has = |req: &[&str]| req.iter().all(|k| header.iter().any(|h| h == k));
    let l2t = if has(&DYNAMIC_REQUIRED) {
        false
    } else if has(&L2T_REQUIRED) {
        true
    } else {
        let mut sorted = header.clone();
        sorted.sort();
        bail!("{}: unsupported header {sorted:?}; expected psort dynamic {:?} or l2tcsv {:?}", path.display(), DYNAMIC_REQUIRED, L2T_REQUIRED);
    };
    let mut rows = vec![];
    for rec in r.records() {
        let rec = rec?;
        let mut m: BTreeMap<String, String> = header.iter().zip(rec.iter()).filter(|(k, _)| !k.is_empty()).map(|(k, v)| (k.clone(), clean(v))).collect();
        let g = |m: &BTreeMap<String, String>, k: &str| m.get(k).cloned().unwrap_or_default();
        let when = if l2t {
            let w = time_l2t(&g(&m, "date"), &g(&m, "time"), &g(&m, "timezone"));
            m = m.into_iter().map(|(k, v)| (L2T_RENAME.iter().find(|(a, _)| *a == k).map_or(k, |(_, b)| b.to_string()), v)).collect();
            w
        } else {
            time_dynamic(&g(&m, "datetime"))
        };
        let mut row = Row { fields: MATCH_FIELDS.iter().map(|f| (*f, g(&m, f))).collect(), time: when };
        let or = |a: &str, b: &str| Some(g(&m, a)).filter(|v| !v.is_empty()).unwrap_or_else(|| g(&m, b));
        row.fields.insert("user", or("username", "user"));
        row.fields.insert("host", or("hostname", "host"));
        if row.get("display_name").is_empty() && !g(&m, "filename").is_empty() {
            row.fields.insert("display_name", g(&m, "filename"));
        }
        rows.push(row);
    }
    Ok(rows)
}

pub fn account(row: &Row) -> String {
    if !row.get("user").is_empty() {
        return row.get("user").rsplit('\\').next().unwrap_or("").to_lowercase();
    }
    PROFILE.captures(row.get("display_name")).or_else(|| PROFILE.captures(row.get("message"))).map_or(String::new(), |c| c[1].to_lowercase())
}

pub fn classify(row: &Row) -> (&'static str, &'static str) {
    let chain = format!("{} {}", row.get("parser"), row.get("source_long"));
    for (scope, table) in [("host", &*HOST_TYPES), ("user", &*USER_TYPES)] {
        if let Some((name, _)) = table.iter().find(|(_, r)| r.is_match(&chain)) {
            return (scope, name);
        }
    }
    ("", "")
}

type Window = (DateTime<Utc>, DateTime<Utc>);

pub fn persona_windows(logs: &[PathBuf]) -> Result<BTreeMap<String, Vec<Window>>> {
    let mut out: BTreeMap<String, Vec<Window>> = BTreeMap::new();
    for path in logs {
        let (mut persona, mut ts) = (String::new(), vec![]);
        for line in std::fs::read_to_string(path).with_context(|| path.display().to_string())?.lines().filter(|l| !l.trim().is_empty()) {
            let r: Value = serde_json::from_str(line)?;
            if let Some(p) = r["persona"].as_str().filter(|p| !p.is_empty()) {
                persona = p.into();
            }
            let ms = |k: &str| r[k].as_f64().filter(|x| *x != 0.0);
            if let Some(ms) = ms("guest_ms").or(ms("host_ms")) {
                ts.push(ms as i64);
            }
        }
        if !persona.is_empty() && ts.len() > 1 {
            let t = |ms: i64| DateTime::from_timestamp_millis(ms).unwrap_or_default();
            out.entry(persona.to_lowercase()).or_default().push((t(*ts.iter().min().unwrap()), t(*ts.iter().max().unwrap())));
        }
    }
    Ok(out)
}

/// A Counter as a JSON object in insertion order.
fn bump(m: &mut Map<String, Value>, k: &str) {
    let n = m.get(k).and_then(Value::as_u64).unwrap_or(0);
    m.insert(k.into(), (n + 1).into());
}

pub fn coverage(rows: &[Row], personas: &[String], logs: &[PathBuf]) -> Result<Value> {
    let personas: Vec<String> = personas.iter().map(|p| p.to_lowercase()).collect();
    let mut per: BTreeMap<&str, Map<String, Value>> = personas.iter().map(|p| (p.as_str(), Map::new())).collect();
    let (mut events, mut hosts, mut unmapped) = (BTreeMap::<String, u64>::new(), Map::new(), Map::new());
    let (mut unattributed, mut untimed) = (0, 0);
    let windows = persona_windows(logs)?;
    for row in rows {
        let (scope, kind) = classify(row);
        if scope == "host" {
            let h = Some(row.get("host")).filter(|h| !h.is_empty()).unwrap_or("unknown host");
            let entry = hosts.entry(h).or_insert_with(|| json!({}));
            bump(entry.as_object_mut().unwrap(), kind);
            continue;
        }
        let who = account(row);
        if who.is_empty() {
            unattributed += 1;
            continue;
        }
        let Some(types) = per.get_mut(who.as_str()) else {
            bump(&mut unmapped, &who);
            continue;
        };
        if !kind.is_empty() {
            bump(types, kind);
        }
        match row.time {
            None => untimed += 1,
            Some(t) if windows.get(&who).is_some_and(|ws| ws.iter().any(|(a, b)| *a <= t && t <= *b)) => *events.entry(who).or_default() += 1,
            Some(_) => {}
        }
    }
    let mut volume = Map::new();
    for p in &personas {
        let hours = windows.get(p).map_or(0.0, |ws| ws.iter().map(|(a, b)| (*b - *a).num_milliseconds() as f64 / 1000.0).sum::<f64>()) / 3600.0;
        let n = events.get(p).copied().unwrap_or(0);
        let round = |x: f64, d: usize| format!("{x:.d$}").parse::<f64>().unwrap_or(x);
        volume.insert(
            p.clone(),
            if hours != 0.0 { json!({"events": n, "hours": round(hours, 2), "events_per_hour": round(n as f64 / hours, 1)}) } else { json!({"events_per_hour": null, "reason": "no ground-truth log span for this persona"}) },
        );
    }
    let personas_out: Map<String, Value> = personas
        .iter()
        .map(|p| {
            let types = &per[p.as_str()];
            let missing: Vec<&str> = USER_TYPES.iter().map(|(n, _)| *n).filter(|n| !types.contains_key(*n)).collect();
            (p.clone(), json!({"types": types, "missing": missing}))
        })
        .collect();
    Ok(json!({
        "personas": personas_out,
        "host_types": hosts,
        "volume": volume,
        "unmapped_accounts": unmapped,
        "unattributed_rows": unattributed,
        "untimed_rows": untimed,
        "rows": rows.len(),
    }))
}

fn matches(row: &Row, m: &[(String, Regex)]) -> bool {
    m.iter().all(|(f, r)| r.is_match(row.get(f)))
}

pub fn survival(after: &[Row], manifest: &[Value], before: Option<&[Row]>) -> Result<Value> {
    let mut out = vec![];
    for e in manifest {
        let id = e.get("id").cloned().unwrap_or(json!("?"));
        let crit = e["match"].as_object().cloned().unwrap_or_default();
        let mut bad: Vec<&String> = crit.keys().filter(|k| !MATCH_FIELDS.contains(&k.as_str())).collect();
        bad.sort();
        let mut odd: Vec<&String> = crit.iter().filter(|(_, v)| !v.is_string()).map(|(k, _)| k).collect();
        odd.sort();
        if crit.is_empty() || !bad.is_empty() || !odd.is_empty() {
            let why = if !bad.is_empty() {
                format!("unknown match fields {}", crate::metrics::py_list(bad))
            } else if !odd.is_empty() {
                format!("match fields are not strings {}", crate::metrics::py_list(odd))
            } else {
                "no match criteria".to_string()
            };
            out.push(json!({"id": id, "status": "unknown", "reason": why}));
            continue;
        }
        let m = crit.iter().map(|(f, r)| Ok((f.clone(), Regex::new(&format!("(?i){}", r.as_str().unwrap_or_default()))?))).collect::<Result<Vec<_>>>()?;
        let now = after.iter().filter(|r| matches(r, &m)).count();
        let (status, reason) = match before {
            None => (if now > 0 { "present" } else { "missing" }, String::new()),
            Some(before) => match before.iter().filter(|r| matches(r, &m)).count() {
                0 => ("unknown", "not found in the before export".to_string()),
                was => (if now > 0 { "survived" } else { "lost" }, format!("{was} rows before, {now} after")),
            },
        };
        let mut a = json!({"id": id, "status": status, "rows_after": now});
        if !reason.is_empty() {
            a["reason"] = reason.into();
        }
        out.push(a);
    }
    let has = |s: &str| out.iter().any(|a| a["status"] == s);
    let verdict = if out.is_empty() || (!has("lost") && !has("missing") && has("unknown")) { Value::Null } else { json!(!has("lost") && !has("missing")) };
    let basis = if before.is_none() { "manifest only (no before export)" } else { "before/after exports" };
    Ok(json!({"artifacts": out, "pass": verdict, "basis": basis}))
}

pub fn report(timeline: &Path, personas: &[String], logs: &[PathBuf], manifest: Option<&Path>, before: Option<&Path>) -> Result<Value> {
    let rows = read_timeline(timeline)?;
    let mut out = json!({"coverage": coverage(&rows, personas, logs)?});
    out["survival"] = match manifest {
        None => json!({"pass": null, "reason": "no scenario manifest given"}),
        Some(m) => {
            let entries: Vec<Value> = serde_json::from_str(&std::fs::read_to_string(m)?)?;
            let before = before.map(read_timeline).transpose()?;
            survival(&rows, &entries, before.as_deref())?
        }
    };
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    const DYN: &str = "datetime,timestamp_desc,source,source_long,message,parser,display_name,tag\n";
    const L2T: &str = "date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,inode,notes,format,extra\n";

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("ludius-artifacts-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn write(path: PathBuf, text: String) -> PathBuf {
        std::fs::write(&path, text).unwrap();
        path
    }

    #[test]
    fn coverage_volume_and_unmapped() {
        let d = tmp("a");
        let t = |m: u32| format!("2026-10-27T08:{m:02}:00.000000+00:00");
        let after = write(d.join("after.csv"), DYN.to_string() + &[
            format!("{},Executed,REG,UserAssist,'-run notepad,winreg/userassist,NTFS:\\Users\\aberg\\NTUSER.DAT,-", t(10)),
            format!("{},Creation Time,LNK,Windows Shortcut,note,lnk,NTFS:\\Users\\Aberg\\AppData\\Recent\\n.lnk,-", t(11)),
            format!("{},Executed,PE,Prefetch,NOTEPAD.EXE,prefetch,NTFS:\\Windows\\Prefetch\\NOTEPAD.EXE-1.pf,-", t(12)),
            format!("{},Creation Time,LNK,Windows Shortcut,x,lnk,NTFS:\\Users\\bob\\Desktop\\x.lnk,-", t(13)),
            format!("{},Modified,FILE,File stat,y,filestat,NTFS:\\Windows\\System32\\y.dll,-", t(14)),
            "0000-00-00T00:00:00.000000+00:00,Not a time,LNK,Windows Shortcut,z,lnk,NTFS:\\Users\\aberg\\z.lnk,-".into(),
            format!("{},Creation Time,LNK,Windows Shortcut,late,lnk,NTFS:\\Users\\aberg\\late.lnk,-", t(59)),
        ].join("\n"));
        let t0 = 1_793_088_000_000i64;
        let log = write(d.join("day.jsonl"), format!("{}\n{}", json!({"event": "day_start", "host_ms": t0, "persona": "aberg"}), json!({"event": "day_end", "host_ms": t0 + 30 * 60_000, "persona": "aberg"})));
        let r = report(&after, &["aberg".into(), "lnord".into()], &[log], None, None).unwrap();
        let cov = &r["coverage"];
        assert_eq!(cov["personas"]["aberg"]["types"], json!({"userassist": 1, "lnk": 3}));
        assert!(cov["personas"]["aberg"]["missing"].as_array().unwrap().contains(&json!("shellbags")));
        assert_eq!(cov["personas"]["lnord"]["types"], json!({}));
        assert_eq!(cov["host_types"], json!({"unknown host": {"prefetch": 1}}));
        assert_eq!(cov["unmapped_accounts"], json!({"bob": 1}));
        assert_eq!((cov["unattributed_rows"].as_u64(), cov["untimed_rows"].as_u64()), (Some(1), Some(1)));
        assert_eq!(cov["volume"]["aberg"], json!({"events": 2, "hours": 0.5, "events_per_hour": 4.0}));
        assert_eq!(cov["volume"]["lnord"]["events_per_hour"], Value::Null);
        assert_eq!(r["survival"]["pass"], Value::Null);
    }

    #[test]
    fn l2tcsv_layout_and_survival() {
        let d = tmp("b");
        let row = |user: &str, desc: &str, file: &str, fmt: &str| format!("10/27/2026,09:00:00,UTC,M...,REG,Registry Key,Last Written,{user},HOST1,s,{desc},2,{file},-,-,{fmt},-");
        let before = write(d.join("before.csv"), L2T.to_string() + &[
            row("-", "Run key evil.exe", "NTFS:\\Windows\\config\\SOFTWARE", "winreg/run"),
            row("-", "EVIL.EXE run", "NTFS:\\Windows\\Prefetch\\EVIL.EXE-1.pf", "prefetch"),
        ].join("\n"));
        let after = write(d.join("after.csv"), L2T.to_string() + &[
            row("CORP\\aberg", "bags", "NTFS:\\x", "winreg/bagmru"),
            row("-", "EVIL.EXE run", "NTFS:\\Windows\\Prefetch\\EVIL.EXE-1.pf", "prefetch"),
            row("-", "dropper.ps1", "NTFS:\\Users\\aberg\\dropper.ps1", "filestat"),
        ].join("\n"));
        let rows = read_timeline(&after).unwrap();
        assert!(rows[0].time.is_some() && account(&rows[0]) == "aberg");
        assert_eq!(classify(&rows[0]), ("user", "shellbags"));
        let manifest = write(d.join("m.json"), json!([
            {"id": "run-key", "match": {"message": "evil\\.exe", "parser": "winreg/run"}},
            {"id": "prefetch", "match": {"display_name": "EVIL\\.EXE-.*\\.pf"}},
            {"id": "dropper", "match": {"display_name": "dropper\\.ps1"}},
            {"id": "vague"},
            {"id": "typo", "match": {"path": "x"}},
            {"id": "null", "match": {"message": null}},
        ]).to_string());
        let status = |s: &Value| s["artifacts"].as_array().unwrap().iter().map(|a| (a["id"].as_str().unwrap().to_string(), a["status"].as_str().unwrap().to_string())).collect::<BTreeMap<_, _>>();
        let s = &report(&after, &["aberg".into()], &[], Some(&manifest), Some(&before)).unwrap()["survival"];
        let want = [("run-key", "lost"), ("prefetch", "survived"), ("dropper", "unknown"), ("vague", "unknown"), ("typo", "unknown"), ("null", "unknown")];
        assert_eq!(status(s), want.iter().map(|(a, b)| (a.to_string(), b.to_string())).collect());
        assert_eq!(s["pass"], false);
        let alone = &report(&after, &["aberg".into()], &[], Some(&manifest), None).unwrap()["survival"];
        assert_eq!(status(alone)["dropper"], "present");
        assert!(read_timeline(&write(d.join("x.csv"), "time,what\n1,2\n".into())).unwrap_err().to_string().contains("unsupported header"));
    }
}
