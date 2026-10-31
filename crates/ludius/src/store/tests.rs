use super::*;

// Service tests run when LUDIUS_TEST_PG (a server DSN without database, e.g. postgresql://ludius@127.0.0.1:5432)
// and, for screenshots, LUDIUS_TEST_S3 (http://KEY:SECRET@host:port) are set; otherwise they pass without checking.
fn pg() -> Option<Client> {
    let server = std::env::var("LUDIUS_TEST_PG").ok()?;
    let name = format!("t_{:x}", rand::random::<u64>());
    Client::connect(&format!("{server}/postgres"), NoTls).unwrap().batch_execute(&format!("create database {name}")).unwrap();
    Some(connect(&format!("{server}/{name}")).unwrap())
}

fn tmp(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("ludius-store-{name}-{:x}", rand::random::<u64>()));
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn append(p: &Path, s: &str) {
    use std::io::Write;
    std::fs::create_dir_all(p.parent().unwrap()).unwrap();
    std::fs::OpenOptions::new().create(true).append(true).open(p).unwrap().write_all(s.as_bytes()).unwrap();
}

#[test]
fn traces_and_world_round_trip_incrementally() {
    let Some(mut conn) = pg() else { return };
    let runs = tmp("a");
    let log = runs.join("days").join("2026-11-02-aberg.jsonl");
    append(&log, "{\"event\": \"day_start\", \"host_ms\": 1, \"persona\": \"aberg\"}\n{\"event\": \"item_end\", \"host_ms\": 2, \"params\": {\"body\": \"line one\\nåäö\"}}\n");
    append(&runs.join("world").join("narrative.yaml"), "company: Harbourline\nevents:\n- date: 2026-11-04\n  what: ERP\n");
    append(&runs.join("plans").join("2026-11-02-aberg.yaml"), "persona: aberg\nitems: []\n");
    assert_eq!(sync(&mut conn, &runs, None).unwrap(), Synced { lines: 2, docs: 2, shots: 0 });
    assert_eq!(sync(&mut conn, &runs, None).unwrap(), Synced::default());
    append(&log, "{\"event\": \"day_end\", \"host_ms\": 3}\n{\"event\": \"half-writ");
    std::fs::write(runs.join("world").join("narrative.yaml"), "company: Harbourline\nevents: []\n").unwrap();
    assert_eq!(sync(&mut conn, &runs, None).unwrap(), Synced { lines: 1, docs: 1, shots: 0 });
    let on_disk: Vec<Value> = std::fs::read_to_string(&log).unwrap().lines().take(3).map(|l| serde_json::from_str(l).unwrap()).collect();
    assert_eq!(trace(&mut conn, "days/2026-11-02-aberg.jsonl").unwrap(), on_disk);
    assert_eq!(doc(&mut conn, "world/narrative.yaml").unwrap(), serde_json::json!({"company": "Harbourline", "events": []}));
    assert_eq!(doc(&mut conn, "plans/2026-11-02-aberg.yaml").unwrap(), serde_json::json!({"persona": "aberg", "items": []}));
    assert_eq!(conn.query_one("select count(*) from trace where event = 'item_end'", &[]).unwrap().get::<_, i64>(0), 1);
}

#[test]
fn rewritten_log_is_refused() {
    let Some(mut conn) = pg() else { return };
    let d = tmp("b");
    let log = d.join("a.jsonl");
    std::fs::write(&log, "{\"event\": \"x\"}\n{\"event\": \"y\"}\n").unwrap();
    sync(&mut conn, &d, None).unwrap();
    std::fs::write(&log, "{\"event\": \"x\"}\n").unwrap();
    assert!(sync(&mut conn, &d, None).unwrap_err().to_string().contains("append-only"));
    std::fs::write(&log, "{\"event\": \"x\"}\n{\"event\": \"y\"}\nnot json\n").unwrap();
    assert!(sync(&mut conn, &d, None).unwrap_err().to_string().contains("a.jsonl:3: not JSON"));
    let events: Vec<Value> = trace(&mut conn, "a.jsonl").unwrap().iter().map(|r| r["event"].clone()).collect();
    assert_eq!(events, ["x", "y"]);
}

#[test]
fn screenshots_go_to_s3_once_and_read_back_checked() {
    let Some(s3) = std::env::var("LUDIUS_TEST_S3").ok() else { return };
    let Some(mut conn) = pg() else { return };
    let runs = tmp("c");
    let (a, b) = (b"PNG first screen".to_vec(), b"PNG second screen".to_vec());
    append(&runs.join("visual").join("a.png"), "PNG first screen");
    append(&runs.join("visual").join("copy.png"), "PNG first screen");
    append(&runs.join("screen.png"), "PNG second screen");
    let shots = bucket(&format!("{s3}/t-{:x}", rand::random::<u32>())).unwrap();
    assert_eq!(sync(&mut conn, &runs, None).unwrap().shots, 0);
    assert_eq!(sync(&mut conn, &runs, Some(&shots)).unwrap(), Synced { shots: 3, ..Default::default() });
    assert_eq!(sync(&mut conn, &runs, Some(&shots)).unwrap(), Synced::default());
    for (k, raw) in [("visual/a.png", &a), ("visual/copy.png", &a), ("screen.png", &b)] {
        assert_eq!(&screenshot(&mut conn, &shots, k).unwrap(), raw);
    }
    append(&runs.join("screen.png"), " changed");
    assert_eq!(sync(&mut conn, &runs, Some(&shots)).unwrap(), Synced { shots: 1, ..Default::default() });
    assert_eq!(screenshot(&mut conn, &shots, "screen.png").unwrap(), b"PNG second screen changed");
    assert!(screenshot(&mut conn, &shots, "missing.png").is_err());
    let sum: String = conn.query_one("select sha256 from screenshot where key = 'visual/a.png'", &[]).unwrap().get(0);
    shots.put(&sum, b"tampered".to_vec()).unwrap();
    assert!(screenshot(&mut conn, &shots, "visual/a.png").unwrap_err().to_string().contains("does not match"));
}

#[test]
fn s3_url_must_carry_credentials_and_bucket() {
    for bad in ["http://127.0.0.1:9000/b", "http://k:s@127.0.0.1:9000", "ftp://k:s@h/b"] {
        assert!(parse(bad).is_err(), "{bad}");
    }
    let b = parse("http://k%2Fx:s%40y@127.0.0.1:9000/shots/").unwrap();
    assert_eq!((b.key.as_str(), b.secret.as_str(), b.name.as_str(), b.host.as_str()), ("k/x", "s@y", "shots", "127.0.0.1:9000"));
}

#[test]
fn sigv4_matches_minio_py() {
    // reference: minio.signer.sign_v4_s3 for the same request
    let b = parse("http://ludius:ludius-test-secret@127.0.0.1:9000/shots").unwrap();
    let now = chrono::DateTime::parse_from_rfc3339("2026-10-31T12:00:00Z").unwrap().to_utc();
    let got = b.auth(&reqwest::Method::PUT, "/shots/sha256/ab.png", &sha(b"PNG"), now);
    assert!(got.ends_with("Signature=77c3b79af74c880633020ce8dded599afbc168450835cd56d734431da1b28309"), "{got}");
}
