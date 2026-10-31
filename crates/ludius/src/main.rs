//! `ludius run`: one task instance on one VM (port of cli.py cmd_run). Other subcommands are still Python.
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use ludius::assist::Assist;
use ludius::decisions::{DecisionsClient, default_model};
use ludius::hostagent::HostAgent;
use ludius::llm::{DEFAULT_MODEL as PLANNER_MODEL, LlmClient};
use ludius::paths::{data, root, runs};
use ludius::policy::Policy;
use ludius::reply::Composer;
use ludius::runner::{Clock, GroundTruth, Runner};
use ludius::session::{Session, run_task};
use ludius::tasks::{Persona, Task, TaskInstance, load_template, load_yaml};
use ludius_proto::v1::lifecycle_request::Op;
use rand::SeedableRng;

const USAGE: &str = "usage: ludius [--agent ADDR] [--vm VM] run TASK.yaml [--persona P.yaml] [--clock realtime|compressed] [--seed N] [--variant N] [--model M] [--log PATH] [--restore CHECKPOINT]
       ludius plan-worker --temporal HOST:PORT [--model M]
       ludius [--agent ADDR] [--vm VM] worker --temporal HOST:PORT [--persona USER|P.yaml] [--model M]
       ludius [--agent ADDR] range up|status|interactions [USER...] [--temporal HOST:PORT] [--model M] [--timezone TZ]
       ludius metrics LOG... [--human LOG...] [--features CSV] [--timeline CSV [--before CSV] [--manifest JSON]]
       ludius timeline LOG.jsonl... [--out CSV]
       ludius store [--dsn DSN] [--s3 http://KEY:SECRET@host:port/bucket]   (defaults LUDIUS_PG, LUDIUS_S3)";

/// The checkout's .env fills variables the environment doesn't set.
fn load_env() {
    let Ok(text) = std::fs::read_to_string(root().join(".env")) else { return };
    for line in text.lines() {
        if let Some((k, v)) = line.split_once('=')
            && !k.trim().is_empty()
            && std::env::var_os(k.trim()).is_none()
        {
            unsafe { std::env::set_var(k.trim(), v.trim()) };
        }
    }
}

fn env(k: &str) -> String {
    std::env::var(k).unwrap_or_default()
}

fn planner_llm(key: &str) -> Result<LlmClient> {
    LlmClient::new(key, Some(std::env::var("LUDIUS_PLANNER_MODEL").as_deref().unwrap_or(PLANNER_MODEL)))
}

fn main() -> Result<()> {
    load_env();
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut flag = |name: &str| -> Result<Option<String>> {
        let Some(i) = args.iter().position(|a| a == name) else { return Ok(None) };
        if i + 1 >= args.len() {
            bail!("{name} needs a value\n{USAGE}");
        }
        let v = args.remove(i + 1);
        args.remove(i);
        Ok(Some(v))
    };
    let agent_addr = flag("--agent")?.unwrap_or_else(|| std::env::var("LUDIUS_AGENT").unwrap_or_else(|_| "127.0.0.1:7451".into()));
    let vm = flag("--vm")?.unwrap_or_else(|| env("LUDIUS_VM"));
    // a bare name is the persona file of that user (as the range supervisor passes it)
    let persona_path = flag("--persona")?.map(|p| if p.ends_with(".yaml") { PathBuf::from(p) } else { data().join("personas").join(format!("{p}.yaml")) });
    let persona_path = persona_path.unwrap_or_else(|| data().join("personas").join("aberg.yaml"));
    let clock: Clock = flag("--clock")?.as_deref().unwrap_or("compressed").parse()?;
    if clock == Clock::TimeJump {
        bail!("--clock: realtime or compressed");
    }
    let seed: u64 = match flag("--seed")? {
        Some(s) => s.parse()?,
        None => std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs(),
    };
    let variant: Option<usize> = flag("--variant")?.map(|v| v.parse()).transpose()?;
    let model_flag = flag("--model")?;
    let model = model_flag.clone().unwrap_or_else(default_model);
    let log_flag = flag("--log")?;
    let restore = flag("--restore")?;
    let temporal = flag("--temporal")?;
    let out = flag("--out")?;
    let tz = flag("--timezone")?;
    let (features, timeline, before, manifest) = (flag("--features")?, flag("--timeline")?, flag("--before")?, flag("--manifest")?);
    let (dsn, s3) = (flag("--dsn")?, flag("--s3")?);
    if args.first().is_some_and(|a| a == "metrics") && args.len() > 1 {
        let paths = |xs: &[String]| xs.iter().map(PathBuf::from).collect::<Vec<_>>();
        let cut = args.iter().position(|a| a == "--human").unwrap_or(args.len());
        let (logs, human) = (paths(&args[1..cut]), paths(args.get(cut + 1..).unwrap_or_default()));
        let mut out = ludius::metrics::report(&logs, &human, features.as_deref().map(Path::new))?;
        if let Some(t) = timeline {
            let mut files: Vec<PathBuf> = std::fs::read_dir(data().join("personas"))?.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|x| x == "yaml")).collect();
            files.sort();
            let personas = files.iter().map(|p| Ok(load_yaml::<Persona>(p)?.user)).collect::<Result<Vec<_>>>()?;
            out["artifacts"] = ludius::artifacts::report(Path::new(&t), &personas, &logs, manifest.as_deref().map(Path::new), before.as_deref().map(Path::new))?;
        }
        println!("{}", serde_json::to_string_pretty(&out)?);
        return Ok(());
    }
    #[cfg(not(feature = "store"))]
    if args.first().is_some_and(|a| a == "store") {
        let _ = (dsn, s3);
        bail!("ludius store: built without the store feature");
    }
    #[cfg(feature = "store")]
    if args.first().is_some_and(|a| a == "store") {
        use ludius::store;
        let Some(dsn) = dsn.or_else(|| std::env::var("LUDIUS_PG").ok()).filter(|d| !d.is_empty()) else { bail!("no database: pass --dsn or set LUDIUS_PG") };
        let s3 = s3.or_else(|| std::env::var("LUDIUS_S3").ok()).filter(|u| !u.is_empty());
        let shots = s3.as_deref().map(store::bucket).transpose()?;
        let done = store::sync(&mut store::connect(&dsn)?, &runs(), shots.as_ref())?;
        let tail = if shots.is_some() { format!(", {} screenshots", done.shots) } else { " (no S3: screenshots skipped)".into() };
        println!("{} trace lines, {} world/plan files{tail} synced from {}", done.lines, done.docs, runs().display());
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "range") {
        use ludius::rangerun;
        let known = rangerun::personas(&rangerun::vms())?;
        let users: Vec<String> = if args.len() > 2 { args[2..].to_vec() } else { known.clone() };
        let unknown: Vec<&str> = users.iter().filter(|u| !known.contains(u)).map(String::as_str).collect();
        if !unknown.is_empty() {
            bail!("not in {}: {}", rangerun::vms().display(), unknown.join(", "));
        }
        let tz: chrono_tz::Tz = tz.as_deref().unwrap_or("Europe/Stockholm").parse().map_err(|e| anyhow::anyhow!("--timezone: {e}"))?;
        let state = runs().join("range");
        match args.get(1).map(String::as_str) {
            Some("status") => println!("{}", rangerun::summary(&ludius::planner::World::new(&runs()), &users, chrono::Utc::now().with_timezone(&tz).date_naive(), &state, &env)),
            Some("up") => {
                let why = rangerun::refusals(&users, &env, &data().join("personas"));
                if !why.is_empty() {
                    bail!("range not started:
  {}", why.join("
  "));
                }
                // ponytail: the day and nightly schedules are still created by the Python `ludius range up`
                let temporal = temporal.unwrap_or_else(|| "127.0.0.1:7233".into());
                let specs = rangerun::specs(&users, &agent_addr, &temporal, &model, &env);
                rangerun::serve(rangerun::Supervisor::new(specs.into_iter().map(rangerun::Child::new).collect(), &state, Box::new(rangerun::spawn)))?;
            }
            Some("interactions") => {
                let days = runs().join("days");
                let mut t = ludius::interactions::Tracker::new(&days, known, &runs().join("monitor-interactions.jsonl"), ludius::interactions::kinds()?);
                tokio::runtime::Builder::new_current_thread().enable_all().build()?.block_on(async {
                    loop {
                        for i in t.step()? {
                            println!("{}", serde_json::json!({"interaction": i}));
                        }
                        tokio::select! {
                            _ = tokio::time::sleep(std::time::Duration::from_secs(5)) => {}
                            _ = tokio::signal::ctrl_c() => return anyhow::Ok(()),
                        }
                    }
                })?;
            }
            _ => bail!(USAGE),
        }
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "timeline") && args.len() > 1 {
        let out = out.map(PathBuf::from).unwrap_or_else(|| runs().join("timeline.csv"));
        println!("{} rows -> {}", ludius::timeline::export(&args[1..], &out)?, out.display());
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "plan-worker") {
        // the nightly planner worker: no desktop, only the world files and LLM calls
        let Some(address) = temporal else { bail!("plan-worker needs --temporal HOST:PORT
{USAGE}") };
        let model = model_flag.unwrap_or_else(|| std::env::var("LUDIUS_PLANNER_MODEL").unwrap_or_else(|_| PLANNER_MODEL.into()));
        let rt = tokio::runtime::Runtime::new()?;
        return rt.block_on(ludius::nightly::worker::serve(&address, "default", &runs(), &env("OPENROUTER_API_KEY"), Some(model)));
    }
    if args.first().is_some_and(|a| a == "worker") {
        // the persona's day worker (scheduled.serve): holds the desk lock while it serves
        let Some(address) = temporal else { bail!("worker needs --temporal HOST:PORT
{USAGE}") };
        if vm.is_empty() {
            bail!("--vm or LUDIUS_VM is required");
        }
        let persona: Persona = load_yaml(&persona_path)?;
        let cfg = ludius::durable::worker::Config { runs: runs(), persona, vm, agent: agent_addr, key: env("OPENROUTER_API_KEY"), model };
        return tokio::runtime::Runtime::new()?.block_on(ludius::durable::worker::serve(&address, "default", cfg));
    }
    let [cmd, task_path] = args.as_slice() else { bail!(USAGE) };
    if cmd != "run" {
        bail!("{cmd}: only `run` is ported to Rust; use the Python cli for the rest\n{USAGE}");
    }
    if vm.is_empty() {
        bail!("--vm or LUDIUS_VM is required");
    }
    // One controller per desktop (durable.desk_lock), and none while a Temporal day reserves it (scheduled.DayOwner).
    // The lock lives until the process exits; it interoperates with Python's flock/msvcrt lock on the same file.
    let desk = runs().join("locks").join(format!("{vm}.lock"));
    let _desk = ludius::durable::desk_lock(&desk)?;
    let day = desk.with_extension("day.json");
    if day.exists() {
        let owner = std::fs::read_to_string(&day).ok().and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok()).and_then(|v| v["owner"].as_str().map(String::from));
        bail!("desktop reserved by {}; resume that Temporal day first", owner.as_deref().unwrap_or("an unreadable reservation"));
    }
    // ponytail: LUDIUS_VISUAL (the OCR fallback agent) is not ported; a planned add-on.
    if !env("LUDIUS_VISUAL").is_empty() {
        bail!("LUDIUS_VISUAL={:?}: the visual fallback is not in the Rust controller yet", env("LUDIUS_VISUAL"));
    }

    let mut inst: TaskInstance = load_yaml(Path::new(task_path))?;
    if variant.is_some() {
        inst.variant = variant;
    }
    let template = load_template(&data().join("templates").join(format!("{}.yaml", inst.template)))?;
    let persona: Persona = load_yaml(&persona_path)?;
    let mut task = Task::new(template, inst, &mut rand::rngs::StdRng::seed_from_u64(seed))?;
    let agent = HostAgent::new(&agent_addr, &vm)?;
    let key = env("OPENROUTER_API_KEY");
    let decider = DecisionsClient::new(&key, Some(model), None)?;
    let name = task.template.task.clone();
    let log_path = log_flag.map(PathBuf::from).unwrap_or_else(|| runs().join(format!("{}-{name}.jsonl", chrono::Local::now().format("%Y%m%d-%H%M%S"))));
    let log = GroundTruth::open(&log_path)?;
    let lifecycle = |op: Op| agent.lifecycle(op, "", "");
    let password = std::env::var(format!("LUDIUS_PERSONA_PASSWORD_{}", persona.user.to_uppercase())).ok().filter(|p| !p.is_empty()).unwrap_or_else(|| env("LUDIUS_PERSONA_PASSWORD"));
    let session = Session::new(&agent, &log, password, Some(&lifecycle));
    if let Some(cp) = restore {
        // ponytail: Hyper-V only; Proxmox's observer restart after a RAM rollback is not ported.
        agent.lifecycle(Op::Restore, &cp, "")?;
        agent.lifecycle(Op::Start, "", "")?;
        session.wait_ready(180.0).context("observer did not report an unlocked desktop")?;
    }
    let assist = if env("LUDIUS_ASSIST") != "0" && !key.is_empty() { Some(Assist { llm: planner_llm(&key)? }) } else { None };
    let composer = if key.is_empty() { None } else { Some(Composer { llm: planner_llm(&key)?, narrative: runs().join("world").join("narrative.yaml"), confirm: true }) };
    let mut runner = Runner::new(&agent, &decider, persona, &log, clock, Policy::from_env()?, Some(seed), assist, composer);
    let out = run_task(&mut runner, &session, &mut task, None, true, &Default::default())?;
    let reason = if out.reason.is_empty() { String::new() } else { format!(" - {}", out.reason) };
    println!("{name} (variant {}, seed {seed}): {} in {} steps{reason}", task.variant, out.status, out.steps);
    println!("ground truth: {}", log_path.display());
    std::process::exit(if out.status == "done" { 0 } else { 1 });
}
