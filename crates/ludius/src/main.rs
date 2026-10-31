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

const USAGE: &str = "usage: ludius [--agent ADDR] [--vm VM] run TASK.yaml [--persona P.yaml] [--clock realtime|compressed] [--seed N] [--variant N] [--model M] [--log PATH] [--restore CHECKPOINT]";

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
    let persona_path = flag("--persona")?.map(PathBuf::from).unwrap_or_else(|| data().join("personas").join("aberg.yaml"));
    let clock: Clock = flag("--clock")?.as_deref().unwrap_or("compressed").parse()?;
    if clock == Clock::TimeJump {
        bail!("--clock: realtime or compressed");
    }
    let seed: u64 = match flag("--seed")? {
        Some(s) => s.parse()?,
        None => std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs(),
    };
    let variant: Option<usize> = flag("--variant")?.map(|v| v.parse()).transpose()?;
    let model = flag("--model")?.unwrap_or_else(default_model);
    let log_flag = flag("--log")?;
    let restore = flag("--restore")?;
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
    std::fs::create_dir_all(desk.parent().unwrap())?;
    let _desk = std::fs::OpenOptions::new().read(true).write(true).create(true).truncate(false).open(&desk)?;
    _desk.try_lock().map_err(|_| anyhow::anyhow!("another controller is driving this desktop ({})", desk.display()))?;
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
