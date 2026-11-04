//! The ludius cli (port of cli.py).
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
       ludius [--agent ADDR] range up|status|interactions [USER...] [--temporal HOST:PORT] [--model M] [--timezone TZ] [--clock C] [--day-at HH:MM] [--plan-at HH:MM] [--today]
       ludius [--agent ADDR] range warp [USER...] --start YYYY-MM-DD [--days 1-7] [--plan-at HH:MM] [--temporal HOST:PORT] [--timezone TZ]
       ludius [--agent ADDR] range unwarp|sweep
       ludius [--agent ADDR] [--vm VM] canary [APP...] [--persona P.yaml] [--model M] [--restore CHECKPOINT]
       ludius [--agent ADDR] [--vm VM] observe [--limit N]
       ludius intranet check [DIR]
       ludius plan narrative|day [--persona USER] [--date YYYY-MM-DD] [--model M]
       ludius day PLAN.yaml [--clock realtime|compressed] [--canary] [--temporal HOST:PORT] [--timezone TZ]
       ludius schedule create|describe|pause|unpause|delete --persona USER [--nightly] [--at HH:MM] [--timezone TZ] [--clock C] [--canary] [--unpause] [--temporal HOST:PORT]
       ludius [--vm VM] reservation [inspect|clear] [--confirm-vm VM --confirm-owner NS/WID] [--temporal HOST:PORT]
       ludius chat reset USER USER... | chat check USER
       ludius mail check USER
       ludius metrics LOG... [--human LOG...] [--features CSV] [--timeline CSV [--before CSV] [--manifest JSON]]
       ludius timeline LOG.jsonl... [--out CSV]
       ludius setup plan|apply RANGE.toml
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
    // a range set up by `ludius setup` keeps the key in a file, not in .env
    if std::env::var_os("OPENROUTER_API_KEY").is_none()
        && let Ok(f) = std::env::var("OPENROUTER_KEY_FILE")
        && let Ok(key) = std::fs::read_to_string(&f)
    {
        unsafe { std::env::set_var("OPENROUTER_API_KEY", key.trim()) };
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
    if args.first().is_some_and(|a| a == "setup") {
        return ludius::setup::main(&args[1..], &root());
    }
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
    let persona_flag = flag("--persona")?;
    let persona_path = persona_flag.clone().or_else(|| Some(env("LUDIUS_PERSONA")).filter(|p| !p.is_empty())).map(|p| if p.ends_with(".yaml") { PathBuf::from(p) } else { ludius::paths::personas().join(format!("{p}.yaml")) });
    let persona_file = || persona_path.as_deref().context("--persona or LUDIUS_PERSONA is required");
    let clock_flag = flag("--clock")?;
    let clock: Clock = clock_flag.as_deref().unwrap_or("compressed").parse()?;
    // new schedules default to realtime days (cli.py range/schedule --clock)
    let sched_clock: Clock = clock_flag.as_deref().unwrap_or("realtime").parse()?;
    if clock == Clock::TimeJump || sched_clock == Clock::TimeJump {
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
    let (start, days, plan_at) = (flag("--start")?, flag("--days")?, flag("--plan-at")?);
    let limit: usize = flag("--limit")?.as_deref().unwrap_or("60").parse().context("--limit")?;
    let (day_at, at, date, confirm_vm, confirm_owner) = (flag("--day-at")?, flag("--at")?, flag("--date")?, flag("--confirm-vm")?, flag("--confirm-owner")?);
    let mut switch = |name: &str| args.iter().position(|a| a == name).map(|i| args.remove(i)).is_some();
    let (today, canary, nightly, unpause) = (switch("--today"), switch("--canary"), switch("--nightly"), switch("--unpause"));
    let tz_name = tz.clone().unwrap_or_else(|| "Europe/Stockholm".into());
    let temporal_addr = temporal.clone().unwrap_or_else(|| "127.0.0.1:7233".into());
    let planner_model = || model_flag.clone().unwrap_or_else(|| std::env::var("LUDIUS_PLANNER_MODEL").unwrap_or_else(|_| PLANNER_MODEL.into()));
    if args.first().is_some_and(|a| a == "plan") {
        let date = match &date {
            Some(d) => chrono::NaiveDate::parse_from_str(d, "%Y-%m-%d").context("--date YYYY-MM-DD")?,
            None => chrono::Local::now().date_naive(),
        };
        let key = env("OPENROUTER_API_KEY");
        match args.get(1).map(String::as_str) {
            Some("narrative") => {
                let llm = LlmClient::new(&key, Some(&planner_model()))?;
                let n = ludius::planner::make_narrative(&llm, &ludius::nightly::worker::personas()?, date, "")?;
                let path = ludius::planner::World::new(&runs()).save_narrative(&n)?;
                println!("{} ({}): {} people, {} events -> {}\nplanner cost ${:.4}", n.company, n.city, n.people.len(), n.events.len(), path.display(), llm.cost.get());
            }
            Some("day") => {
                // ponytail: plans the day only when it is missing (nightly.ensure_days); --scenario is not ported
                let Some(user) = persona_flag.as_deref().map(|p| p.trim_end_matches(".yaml").rsplit(['/', '\\']).next().unwrap_or(p).to_string()) else { bail!("plan day needs --persona USER") };
                for (u, got) in ludius::warp::ensure(&runs(), std::slice::from_ref(&user), &key, Some(&planner_model()), date) {
                    let p = got.with_context(|| format!("{u}: no plan for {date}"))?;
                    println!("{u} {}: {} items{}{} -> {}", p.date, p.items, if p.generated { "" } else { " (existing)" }, if p.note.is_empty() { String::new() } else { format!(", {}", p.note) }, p.path);
                    for r in p.rejects {
                        println!("  rejected {r}");
                    }
                }
            }
            // ponytail: `plan revise` (the nightly revises when due) and `plan template` (needs the dropped validate) are not ported
            _ => bail!(USAGE),
        }
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "day") {
        let [_, path] = args.as_slice() else { bail!(USAGE) };
        let plan = ludius::planner::load_plan(Path::new(path), false)?;
        let tz: chrono_tz::Tz = tz_name.parse().map_err(|e| anyhow::anyhow!("--timezone: {e}"))?;
        let (persona, day) = (plan.persona.clone(), plan.date.clone());
        // the persona's range worker plays it (it holds the desk); a running day of that date is joined
        let got = tokio::runtime::Runtime::new()?.block_on(async { ludius::schedules::play(&ludius::schedules::connect(&temporal_addr, "default").await?, plan, tz, clock, canary).await })?;
        let done = got.map_err(|e| anyhow::anyhow!("day {day} {persona}: {e}"))?;
        for d in &done {
            println!("  {} {}: {}", d.at, d.template, d.status);
        }
        let ok = done.iter().filter(|d| d.status == "done").count();
        println!("day {day} {persona}: {ok}/{} done\nground truth: {}", done.len(), ludius::planner::World::new(&runs()).day_log(&persona, &day).display());
        std::process::exit(i32::from(ok != done.len()));
    }
    if args.first().is_some_and(|a| a == "schedule") {
        use ludius::schedules;
        let [_, what] = args.as_slice() else { bail!(USAGE) };
        let Some(user) = persona_flag.as_deref() else { bail!("schedule needs --persona USER") };
        let def = if nightly {
            if canary || clock_flag.is_some() {
                bail!("--clock/--canary configure the day schedule, not --nightly planning");
            }
            schedules::plan(user, at.as_deref().unwrap_or("22:00"), &tz_name, !unpause)?
        } else {
            schedules::day(user, at.as_deref().unwrap_or("08:30"), &tz_name, sched_clock, !unpause, canary, 0)?
        };
        let id = def.id.clone();
        let out = tokio::runtime::Runtime::new()?.block_on(tokio::time::timeout(std::time::Duration::from_secs(30), async {
            let client = schedules::connect(&temporal_addr, "default").await?;
            let h = client.get_schedule_handle(&id);
            match what.as_str() {
                "create" if !schedules::create(&client, def).await? => bail!("{id}: exists"),
                "create" | "describe" => {}
                "pause" => h.pause(Some("paused by ludius schedule"), Default::default()).await?,
                "unpause" => h.unpause(Some("unpaused by ludius schedule"), Default::default()).await?,
                "delete" => {
                    h.delete(Default::default()).await?;
                    return Ok(None);
                }
                _ => bail!(USAGE),
            }
            Ok(Some(schedules::describe(&client, &id).await?))
        }))??;
        match out {
            Some(v) => println!("{}", serde_json::to_string_pretty(&v)?),
            None => println!("{id}: deleted (does not cancel a running day)"),
        }
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "reservation") {
        if vm.is_empty() {
            bail!("--vm or LUDIUS_VM is required");
        }
        let confirm = match args.get(1).map(String::as_str) {
            None | Some("inspect") => None,
            Some("clear") => match (confirm_vm.as_deref(), confirm_owner.as_deref()) {
                (Some(v), Some(o)) if !v.is_empty() && !o.is_empty() => Some((v, o)),
                _ => bail!("clear needs --confirm-vm and --confirm-owner repeating the reservation"),
            },
            _ => bail!(USAGE),
        };
        let rt = tokio::runtime::Runtime::new()?;
        let status = |ns: &str, wid: &str| {
            rt.block_on(tokio::time::timeout(std::time::Duration::from_secs(30), async {
                let client = ludius::schedules::connect(&temporal_addr, ns).await?;
                Ok(client.get_workflow_handle::<temporalio_client::UntypedWorkflow>(wid).describe(Default::default()).await?.status())
            }))?
        };
        let out = ludius::schedules::reconcile(&runs().join("locks").join(format!("{vm}.lock")), &status, confirm)?;
        println!("{}", serde_json::to_string_pretty(&out)?);
        return Ok(());
    }
    #[cfg(feature = "intranet")]
    if args.first().is_some_and(|a| a == "intranet") {
        let [_, check, rest @ ..] = args.as_slice() else { bail!(USAGE) };
        if check != "check" || rest.len() > 1 {
            bail!(USAGE);
        }
        let dir = rest.first().map(PathBuf::from).unwrap_or_else(ludius::intranet::dir);
        let pages = ludius::intranet::load(&dir).unwrap_or_else(|e| {
            eprintln!("intranet: {e:#}");
            std::process::exit(1)
        });
        let pages: Vec<_> = pages.iter().map(|p| serde_json::json!({"id": p.id, "title": p.title, "section": p.section, "owner": p.owner, "updated": p.updated, "popular": p.popular, "tags": p.tags, "body": p.body, "files": p.files})).collect();
        println!("{}", serde_json::Value::from(pages));
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "chat") {
        // One read of the user's unread posts: pre-flight proof that chat outcome checks can read (setup verify).
        if let [_, check, user] = args.as_slice()
            && check == "check"
        {
            match ludius::chat::unread(user) {
                Some(p) => println!("{user}: chat read, {} unread", p.len()),
                None => {
                    println!("{user}: chat unreadable (no chat backend, or the read failed)");
                    std::process::exit(1);
                }
            }
            return Ok(());
        }
        let [_, reset, users @ ..] = args.as_slice() else { bail!(USAGE) };
        let mut seen = std::collections::BTreeSet::new();
        let users: Vec<&String> = users.iter().filter(|u| seen.insert(u.as_str())).collect();
        if reset != "reset" || users.len() < 2 {
            bail!(USAGE);
        }
        let mut bad = 0;
        for (i, a) in users.iter().enumerate() {
            for b in &users[i + 1..] {
                let n = ludius::chat::reset(a, b);
                println!("{a} <-> {b}: {}", n.map_or("unknown (chat01 unreachable or refused)".into(), |n| format!("{n} posts deleted")));
                bad += usize::from(n.is_none());
            }
        }
        std::process::exit(i32::from(bad > 0));
    }
    if args.first().is_some_and(|a| a == "mail") {
        // One inbox read through the selected backend: pre-flight proof that outcome checks can read (warp9: EWS CA path broken, every read unknown).
        let [_, check, user] = args.as_slice() else { bail!(USAGE) };
        if check != "check" {
            bail!(USAGE);
        }
        // an empty mail domain would make every address `user@`: no colleague passes, every mail task fails
        if ludius::mail::domain().is_empty() {
            bail!("LUDIUS_MAIL_DOMAIN is not set");
        }
        match ludius::mail::newest(user, "inbox") {
            Some(m) => println!("{user}@{}: inbox read, {} newest", ludius::mail::domain(), m.len()),
            None => {
                println!("{user}@{}: inbox unreadable (no mail backend, or the read failed)", ludius::mail::domain());
                std::process::exit(1);
            }
        }
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "observe") {
        if vm.is_empty() {
            bail!("--vm or LUDIUS_VM is required");
        }
        // ponytail: --record (fixture capture) is not ported; nothing reads fixtures/ any more.
        let res = HostAgent::new(&agent_addr, &vm)?.observe(true, 0, 0)?;
        let s = res.snapshot.context("observer answered without a snapshot")?;
        println!("settled={} {}ms locked={} elements={} ui_access={}", s.settled, s.settle_ms, s.locked, s.elements.len(), res.hello.is_some_and(|h| h.ui_access));
        for w in &s.windows {
            println!("  window {} {} {:?}", w.kind().as_str_name().to_lowercase(), w.process_name, w.title);
        }
        for e in s.elements.iter().take(limit) {
            println!("  {}", ludius::options::describe(e));
        }
        return Ok(());
    }
    if args.first().is_some_and(|a| a == "metrics") && args.len() > 1 {
        let paths = |xs: &[String]| xs.iter().map(PathBuf::from).collect::<Vec<_>>();
        let cut = args.iter().position(|a| a == "--human").unwrap_or(args.len());
        let (logs, human) = (paths(&args[1..cut]), paths(args.get(cut + 1..).unwrap_or_default()));
        let mut out = ludius::metrics::report(&logs, &human, features.as_deref().map(Path::new))?;
        if let Some(t) = timeline {
            let mut files: Vec<PathBuf> = std::fs::read_dir(ludius::paths::personas())?.filter_map(|e| e.ok().map(|e| e.path())).filter(|p| p.extension().is_some_and(|x| x == "yaml")).collect();
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
        let lease = ludius::warp::lease_path(&runs());
        let clock = |strict: bool| ludius::rangeclock::range(strict, &agent_addr);
        match args.get(1).map(String::as_str) {
            Some("unwarp") => std::process::exit(ludius::warp::unwarp(|| clock(false), &lease)),
            Some("sweep") => std::process::exit(ludius::warp::sweep(|| clock(false), &lease, None)?),
            Some("warp") => {
                let why = rangerun::refusals(&users, &env, &ludius::paths::personas());
                if !why.is_empty() {
                    eprintln!("range not started:
  {}", why.join("
  "));
                    std::process::exit(2);
                }
                let days: usize = days.as_deref().unwrap_or("3").parse().context("--days")?;
                let dates = ludius::warp::dates(start.as_deref(), days, chrono::Utc::now().with_timezone(&tz).date_naive()).unwrap_or_else(|m| {
                    eprintln!("{m}");
                    std::process::exit(2)
                });
                let plan_at = chrono::NaiveTime::parse_from_str(plan_at.as_deref().unwrap_or("22:00"), "%H:%M").context("--plan-at HH:MM")?;
                let clock = clock(true)?;
                let temporal = temporal.unwrap_or_else(|| "127.0.0.1:7233".into());
                let specs = rangerun::specs(&users, &agent_addr, &temporal, &model, &env);
                let sup = rangerun::Supervisor::new(specs.into_iter().map(rangerun::Child::new).collect(), &state, Box::new(rangerun::spawn));
                let rt = tokio::runtime::Runtime::new()?;
                let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
                let raised = stop.clone();
                rt.spawn(async move {
                    #[cfg(unix)]
                    let term = async { tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).ok()?.recv().await };
                    #[cfg(not(unix))]
                    let term = std::future::pending::<Option<()>>();
                    tokio::select! { _ = tokio::signal::ctrl_c() => {}, _ = term => {} }
                    raised.store(true, std::sync::atomic::Ordering::SeqCst);
                });
                let model = Some(std::env::var("LUDIUS_PLANNER_MODEL").unwrap_or_else(|_| PLANNER_MODEL.into()));
                let live = ludius::warp::Live { rt, temporal, runs: runs(), users: users.clone(), tz, key: env("OPENROUTER_API_KEY"), model };
                let lease = ludius::clocklease::Lease::new(&lease, "range");
                let w = ludius::warp::Warp { runs: runs(), users, tz, plan_at, clock, lease, sup, days: &live, stop: &stop };
                std::process::exit(w.run(&dates)?);
            }
            Some("status") => println!("{}", rangerun::summary(&ludius::planner::World::new(&runs()), &users, chrono::Utc::now().with_timezone(&tz).date_naive(), &state, &env)),
            Some("up") => {
                let why = rangerun::refusals(&users, &env, &ludius::paths::personas());
                if !why.is_empty() {
                    bail!("range not started:
  {}", why.join("
  "));
                }
                use ludius::schedules;
                let temporal = temporal_addr.clone();
                let (plan_at, day_at) = (plan_at.as_deref().unwrap_or("22:00"), day_at.as_deref().unwrap_or("06:30"));
                let rt = tokio::runtime::Runtime::new()?;
                let client = rt.block_on(tokio::time::timeout(std::time::Duration::from_secs(60), async {
                    let client = schedules::connect(&temporal, "default").await?;
                    for u in &users {
                        for d in [schedules::plan(u, plan_at, &tz_name, false)?, schedules::day(u, day_at, &tz_name, sched_clock, false, false, 0)?] {
                            let id = d.id.clone();
                            println!("{id}: {}", if schedules::create(&client, d).await? { "created" } else { "exists, kept as it is" });
                        }
                    }
                    anyhow::Ok(client)
                }))??;
                if today {
                    let date = chrono::Utc::now().with_timezone(&tz).date_naive();
                    for (u, got) in ludius::warp::ensure(&runs(), &users, &env("OPENROUTER_API_KEY"), Some(&planner_model()), date) {
                        let p = match got {
                            Ok(p) => p,
                            Err(e) => {
                                eprintln!("{u}: no plan for {date}, its day will fail to load: {e:#}");
                                continue;
                            }
                        };
                        println!("{u}: plan {}", p.path);
                        let h = client.get_schedule_handle(schedules::day_id(&u)?);
                        rt.block_on(tokio::time::timeout(std::time::Duration::from_secs(30), h.trigger(Default::default(), Default::default())))??;
                        println!("{u}: today's day started");
                    }
                }
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
        let persona: Persona = load_yaml(persona_file()?)?;
        let cfg = ludius::durable::worker::Config { runs: runs(), persona, vm, agent: agent_addr, key: env("OPENROUTER_API_KEY"), model };
        return tokio::runtime::Runtime::new()?.block_on(ludius::durable::worker::serve(&address, "default", cfg));
    }
    let (task_path, apps) = match args.as_slice() {
        [c, t] if c == "run" => (Some(t.clone()), vec![]),
        [c, apps @ ..] if c == "canary" => (None, apps.to_vec()),
        _ => bail!(USAGE),
    };
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

    let persona: Persona = load_yaml(persona_file()?)?;
    let task = match &task_path {
        Some(p) => {
            let mut inst: TaskInstance = load_yaml(Path::new(p))?;
            if variant.is_some() {
                inst.variant = variant;
            }
            let template = load_template(&data().join("templates").join(format!("{}.yaml", inst.template)))?;
            Some(Task::new(template, inst, &mut rand::rngs::StdRng::seed_from_u64(seed))?)
        }
        None => None,
    };
    let agent = HostAgent::new(&agent_addr, &vm)?;
    let key = env("OPENROUTER_API_KEY");
    let decider = DecisionsClient::new(&key, Some(model), None)?;
    let stamp = chrono::Local::now().format("%Y%m%d-%H%M%S");
    let log_path = match &task {
        Some(t) => log_flag.map(PathBuf::from).unwrap_or_else(|| runs().join(format!("{stamp}-{}.jsonl", t.template.task))),
        None => runs().join("canary").join(format!("{stamp}.jsonl")),
    };
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
    let Some(mut task) = task else {
        // the drift canaries: one admitted task per app (only `apps` when given), results to runs/health.json
        let mut runner = Runner::new(&agent, &decider, persona, &log, Clock::Compressed, Policy::from_env()?, Some(0), assist, None);
        let mut desk = ludius::day::Live { runner: &mut runner, session: &session };
        let health = runs().join("health.json");
        let apps = apps.into_iter().collect();
        let states = ludius::canary::run(&ludius::planner::templates()?, &data().join("admission.yaml"), &mut desk, &log, &health, &apps, &mut |s| println!("{s}"))?;
        println!("health: {}\nlog: {}", health.display(), log_path.display());
        std::process::exit(i32::from(states.values().any(|s| s == "drift")));
    };
    let name = task.template.task.clone();
    let composer = if key.is_empty() { None } else { Some(Composer { llm: planner_llm(&key)?, narrative: runs().join("world").join("narrative.yaml"), confirm: true }) };
    let mut runner = Runner::new(&agent, &decider, persona, &log, clock, Policy::from_env()?, Some(seed), assist, composer);
    let out = run_task(&mut runner, &session, &mut task, None, true, &Default::default())?;
    let reason = if out.reason.is_empty() { String::new() } else { format!(" - {}", out.reason) };
    println!("{name} (variant {}, seed {seed}): {} in {} steps{reason}", task.variant, out.status, out.steps);
    println!("ground truth: {}", log_path.display());
    std::process::exit(if out.status == "done" { 0 } else { 1 });
}
