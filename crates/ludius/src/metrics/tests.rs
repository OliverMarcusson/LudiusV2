use super::*;

fn dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("ludius-metrics-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn log(path: PathBuf, gap_ms: i64, launch: &str, n: i64) -> PathBuf {
    let mut t = 0;
    let mut lines = vec![json!({"event": "step", "host_ms": 1, "persona": "p"})];
    for k in 0..n {
        lines.push(json!({"event": "task_start", "host_ms": t + 1, "persona": "p", "task": "tpl", "subgoals": [launch]}));
        for _ in 0..3 {
            t += gap_ms + if launch == "human" { k * 7 } else { 0 };
            lines.push(json!({"event": "step", "host_ms": t, "action": "click", "process": "x.exe"}));
        }
        lines.push(json!({"event": "task_end", "host_ms": t + 1, "task": "tpl", "status": if k > 0 { "done" } else { "abandoned" }}));
    }
    std::fs::write(&path, lines.iter().map(Value::to_string).collect::<Vec<_>>().join("\n")).unwrap();
    path
}

fn logs(d: &Path, prefix: &str, launch: &str, n: i64) -> Vec<PathBuf> {
    (0..n).map(|k| log(d.join(format!("{prefix}{k}.jsonl")), 1000 + 50 * k, launch, 4)).collect()
}

#[test]
fn range_metrics_and_filterability() {
    let d = dir("a");
    let (noise, human) = (log(d.join("n.jsonl"), 1000, "open_from_start", 4), log(d.join("h.jsonl"), 1000, "human", 4));
    let r = report(&[noise], &[human], None).unwrap();
    assert_eq!(r["coherence"]["completion_by_template"], json!({"tpl": 0.75}));
    assert_eq!(r["coherence"]["orphan_steps"], 1);
    assert_eq!(r["guardrails"]["failure_rate_by_app"], json!({"x.exe": 0.25}));
    assert_eq!(r["repetition"]["sequence_repeat_by_template"], json!({"tpl": 0.75}));
    let f = &r["filterability"];
    assert!(f["best"][0] == 1.0 && f["pass"] == false);
    assert_eq!(f["rules"]["steps"][0], 0.5);
}

#[test]
fn held_out_splits_whole_source_logs_and_fits_on_train_only() {
    let d = dir("b");
    let (noise, human) = (logs(&d, "n", "open_from_start", 4), logs(&d, "h", "human", 3));
    let csv_path = d.join("features.csv");
    let r = report(&noise, &human, Some(&csv_path)).unwrap();
    let h = &r["filterability"]["held_out"];
    assert!(h["available"] == true && h["grouping"] == "source log" && h["salt"] == HOLDOUT_SALT);
    let limits = h["limits"].as_str().unwrap();
    assert!(limits.contains("near-duplicate") && limits.contains("genuine humans"));
    let set = |s: &str| h[s]["sources"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect::<BTreeSet<_>>();
    let (train, test) = (set("train"), set("test"));
    let all: BTreeSet<String> = noise.iter().chain(&human).map(|p| p.file_name().unwrap().to_string_lossy().into()).collect();
    assert!(train.is_disjoint(&test) && train.union(&test).cloned().collect::<BTreeSet<_>>() == all);
    for s in ["train", "test"] {
        assert!(h[s]["noise"] != 0 && h[s]["human"] != 0);
    }
    assert!(h["held_out_score"] == 1.0 && h["pass"] == false);
    assert_eq!(&report(&noise, &human, None).unwrap()["filterability"]["held_out"], h);
    let ds = &r["dataset"];
    assert!(ds["human_baseline"] == false && ds["labels"].as_str().unwrap().starts_with("stand-in"));
    assert_eq!(ds["noise"].as_array().unwrap().iter().map(|x| x["source"].as_str().unwrap()).collect::<Vec<_>>(), ["n0.jsonl", "n1.jsonl", "n2.jsonl", "n3.jsonl"]);
    let rows: Vec<csv::StringRecord> = csv::Reader::from_path(&csv_path).unwrap().records().map(Result::unwrap).collect();
    assert_eq!((rows.len(), ds["features_csv"]["rows"].as_u64()), (28, Some(28)));
    assert_eq!(&rows[0][3], "2");
    let pairs: BTreeSet<(String, String)> = rows.iter().map(|r| (r[2].to_string(), r[1].to_string())).collect();
    let want: BTreeSet<(String, String)> = train.iter().map(|s| (s.clone(), "train".into())).chain(test.iter().map(|s| (s.clone(), "test".into()))).collect();
    assert_eq!(pairs, want);
}

#[test]
fn held_out_is_unavailable_rather_than_optimistic() {
    let d = dir("c");
    let same = |p: &str| (0..3).map(|k| log(d.join(format!("{p}{k}.jsonl")), 1000, "open_from_start", 4)).collect::<Vec<_>>();
    let h = &report(&same("n"), &same("h"), None).unwrap()["filterability"]["held_out"];
    assert!(h["available"] == false && h["reason"] == "no training rule beats chance");
    let one = report(&logs(&d, "x", "open_from_start", 3), &logs(&d, "y", "human", 1), None).unwrap();
    let h = &one["filterability"]["held_out"];
    assert!(h["available"] == false && h["reason"].as_str().unwrap().contains("1 independent human source"));
    assert_eq!(one["filterability"]["best"][0], 1.0);
    let shared = logs(&d, "s", "human", 2);
    let inst = load(&shared, &root_of(&shared).unwrap()).unwrap().instances;
    assert!(held_out(&inst, &inst, HOLDOUT_FRACTION, HOLDOUT_SALT)["reason"].as_str().unwrap().contains("both labels"));
}
