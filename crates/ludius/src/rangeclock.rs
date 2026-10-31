//! The range clock (port of rangeclock.py): jumps every range VM's Windows clock forward, DC first, and back
//! to real time, through the host agent's guest exec. Refuses VMs that are not range VMs.
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use anyhow::{Context, Result, anyhow, bail};
use chrono::{DateTime, Utc};
use serde_json::Value;

use crate::hostagent::HostAgent;
use crate::rangerun;

pub const MAX_SKEW_S: f64 = 60.0;
pub const POOL: &str = "ludius";
const RETRIES: usize = 6;
const RETRY_S: f64 = 10.0;

/// The guest agent cannot be reached (yet); retried with a fresh target.
#[derive(Debug)]
pub struct AgentDown(pub String);

impl std::fmt::Display for AgentDown {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for AgentDown {}

/// VM id -> why it is not a range VM.
pub type Bad = BTreeMap<u32, String>;
pub type Exec = Box<dyn Fn(u32, &str) -> Result<String> + Send + Sync>;

// A guest exec that timed out can still run later: the sequence file makes the guest skip a Set-Date older than one
// it already applied; the mutex keeps check and set together across guest processes.
fn set_script(seq: u128, ms: i64) -> String {
    format!(
        concat!(
            r#"$m = New-Object Threading.Mutex($false, 'Global\ludius-clock'); "#,
            r#"if (-not $m.WaitOne(30000)) {{ throw 'clock mutex busy' }}; try {{ "#,
            r#"$q = "$env:ProgramData\ludius-clock.seq"; $o = 0L; "#,
            r#"if (Test-Path $q) {{ [void][long]::TryParse((Get-Content -Raw $q).Trim(), [ref]$o) }}; "#,
            r#"if ($o -lt {seq}) {{ [IO.File]::WriteAllText($q, '{seq}'); "#,
            r#"$e = (Get-Date) - [Diagnostics.Process]::GetCurrentProcess().StartTime; "#,
            r#"if ($e.TotalSeconds -lt 0 -or $e.TotalSeconds -gt 30) {{ $e = [TimeSpan]::Zero }}; "#,
            r#"Set-Date ([DateTimeOffset]::FromUnixTimeMilliseconds({ms}).LocalDateTime + $e) | Out-Null }} "#,
            r#"}} finally {{ $m.ReleaseMutex() }}; "#,
        ),
        seq = seq,
        ms = ms
    )
}
const PURGE: &str = "klist -li 0x3e7 purge | Out-Null; klist -li 0x3e4 purge | Out-Null; ";
const NOW: &str = "[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()";

fn now_s() -> f64 {
    Utc::now().timestamp_micros() as f64 / 1e6
}

fn iso(t: f64) -> String {
    DateTime::from_timestamp(t as i64, 0).map(|d| d.to_rfc3339_opts(chrono::SecondsFormat::Secs, false)).unwrap_or_default()
}

fn listed(bad: &Bad) -> String {
    bad.iter().map(|(k, v)| format!("{k}: {v}")).collect::<Vec<_>>().join("; ")
}

pub struct RangeClock {
    pub dc: u32,
    pub members: Vec<u32>,
    pub run: Exec,
    pub now: Box<dyn Fn() -> f64 + Send + Sync>,
    /// Re-checked before every set and reset: the VMs that are no longer range VMs.
    pub verify: Box<dyn Fn() -> Result<Bad> + Send + Sync>,
    pub sleep: Box<dyn Fn(f64) + Send + Sync>,
}

impl RangeClock {
    pub fn new(dc: u32, members: Vec<u32>, run: Exec) -> Self {
        Self { dc, members, run, now: Box::new(now_s), verify: Box::new(|| Ok(Bad::new())), sleep: Box::new(|s| std::thread::sleep(std::time::Duration::from_secs_f64(s))) }
    }

    fn retry<T>(&self, act: impl Fn() -> Result<T>) -> Result<T> {
        for _ in 0..RETRIES {
            match act() {
                Err(e) if e.is::<AgentDown>() => (self.sleep)(RETRY_S),
                r => return r,
            }
        }
        act()
    }

    fn offset(&self, vmid: u32) -> Result<f64> {
        self.retry(|| {
            let t0 = (self.now)();
            let out = (self.run)(vmid, NOW)?;
            let guest = out.trim().parse::<i64>().with_context(|| format!("VM {vmid}: guest time {out:?}"))? as f64 / 1000.0;
            Ok(guest - (t0 + (self.now)()) / 2.0)
        })
    }

    /// Jump every clock forward to `at`; never backwards from the DC's current time.
    pub fn set(&self, at: DateTime<Utc>) -> Result<BTreeMap<u32, f64>> {
        let bad = (self.verify)()?;
        if !bad.is_empty() {
            bail!("range clock refused, not range VMs: {}", listed(&bad));
        }
        let target = at.timestamp_micros() as f64 / 1e6;
        // the offset first: its retries take time, and "now" must be read after them
        let dc_off = self.offset(self.dc)?;
        let dc_now = (self.now)() + dc_off;
        if target <= dc_now {
            bail!("range clock is forward only: DC at {}, asked {}", iso(dc_now), iso(target));
        }
        self.apply(target - (self.now)(), false, &Bad::new())
    }

    /// Back to real time on every VM that is still a range VM; tries all of them and reports every failure.
    pub fn reset(&self) -> Result<BTreeMap<u32, f64>> {
        let bad = (self.verify)().map_err(|e| anyhow!("range check failed, no clock reset: {e:#}"))?;
        self.apply(0.0, true, &bad)
    }

    fn apply(&self, offset: f64, keep_going: bool, skip: &Bad) -> Result<BTreeMap<u32, f64>> {
        let errors = Mutex::new(skip.iter().map(|(k, v)| format!("VM {k} skipped, not a range VM: {v}")).collect::<Vec<_>>());
        let got = Mutex::new(BTreeMap::new());
        let members: Vec<u32> = self.members.iter().copied().filter(|v| !skip.contains_key(v)).collect();
        let guarded = |vmid: u32, r: Result<()>| -> Result<()> {
            match r {
                Err(e) if keep_going => {
                    errors.lock().unwrap().push(format!("VM {vmid}: {e:#}"));
                    Ok(())
                }
                r => r,
            }
        };
        let set_one = |vmid: u32| -> Result<()> {
            self.retry(|| {
                let ms = (((self.now)() + offset) * 1000.0) as i64;
                let seq = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_nanos();
                (self.run)(vmid, &(set_script(seq, ms) + PURGE + NOW))
            })
            .map(drop)
        };
        let measure = |vmid: u32| -> Result<()> { self.offset(vmid).map(|o| { got.lock().unwrap().insert(vmid, o); }) };
        let dc: Vec<u32> = if skip.contains_key(&self.dc) { vec![] } else { vec![self.dc] };
        for &v in &dc {
            guarded(v, set_one(v))?;
        }
        let each = |vms: &[u32], act: &(dyn Fn(u32) -> Result<()> + Sync)| -> Result<()> {
            std::thread::scope(|s| {
                let hs: Vec<_> = vms.iter().map(|&v| s.spawn(move || guarded(v, act(v)))).collect();
                hs.into_iter().map(|h| h.join().unwrap_or_else(|_| Err(anyhow!("clock thread panicked")))).collect::<Vec<_>>().into_iter().collect()
            })
        };
        each(&members, &set_one)?;
        each(&[dc, members].concat(), &measure)?;
        let got = got.into_inner().unwrap();
        let mut errors = errors.into_inner().unwrap();
        let (lo, hi) = got.values().fold((f64::MAX, f64::MIN), |(lo, hi), &v| (lo.min(v), hi.max(v)));
        let skew = if got.is_empty() { 0.0 } else { hi - lo };
        if skew > MAX_SKEW_S || got.get(&self.dc).is_some_and(|o| (o - offset).abs() > MAX_SKEW_S) {
            errors.push(format!("range clock not converged (skew {skew:.1} s): {got:?}"));
        }
        if !errors.is_empty() {
            bail!(errors.join("; "));
        }
        Ok(got)
    }
}

fn num(v: &Value) -> Option<u64> {
    v.as_u64().or_else(|| v.as_str().and_then(|s| s.parse().ok()))
}

/// The expected VMs that are missing or not range VMs (name, pool, node, not a template) in /cluster/resources.
pub fn violations(expected: &BTreeMap<u32, String>, resources: &Value, node: &str) -> Bad {
    let seen: BTreeMap<u64, &Value> = resources.as_array().into_iter().flatten().filter_map(|r| Some((num(&r["vmid"])?, r))).collect();
    expected
        .iter()
        .filter_map(|(&vmid, name)| {
            let Some(r) = seen.get(&u64::from(vmid)) else { return Some((vmid, "not found".into())) };
            let template = num(&r["template"]).unwrap_or(0) != 0 || r["template"] == true;
            let s = |k: &str| r[k].as_str().unwrap_or("None").to_string();
            if s("name") == *name && s("pool") == POOL && s("node") == node && !template {
                return None;
            }
            Some((vmid, format!("{} in pool {} on {}, expected {name} in {POOL} on {node}{}", s("name"), s("pool"), s("node"), if template { " (a template)" } else { "" })))
        })
        .collect()
}

/// A PowerShell script in a range VM through the host agent (QEMU guest agent exec).
pub fn agent_exec(address: &str, vm: &str, script: &str) -> Result<String> {
    let cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script].map(String::from).to_vec();
    let res = HostAgent::new(address, vm)?.guest_exec(cmd, 60).map_err(|e| {
        if crate::hostagent::code(&e) == Some(tonic::Code::Unavailable) { anyhow!(AgentDown(format!("{vm}: guest exec: {e:#}"))) } else { e }
    })?;
    if res.exitcode != 0 || !res.exited {
        let err: String = res.err_data.chars().rev().take(300).collect::<Vec<_>>().into_iter().rev().collect();
        bail!("{vm}: exit {}: {err}", if res.exited { res.exitcode.to_string() } else { "none (timed out)".into() });
    }
    Ok(res.out_data)
}

/// The range's clock (cli._range_clock): DC and Exchange plus every persona VM of range-vms.yml. `strict`
/// refuses at once unless every one is a range VM; set and reset re-check either way.
pub fn range(strict: bool, agent: &str) -> Result<RangeClock> {
    if std::env::var("LUDIUS_MAIL_BACKEND").as_deref() != Ok("exchange") {
        bail!("range clock: Proxmox Exchange range only (dc01 is its time source; LUDIUS_MAIL_BACKEND)");
    }
    let vms = rangerun::vms();
    let main_yml = vms.parent().unwrap_or(std::path::Path::new(".")).join("main.yml");
    let main: Value = serde_yaml::from_str(&std::fs::read_to_string(&main_yml).with_context(|| main_yml.display().to_string())?)?;
    let id = |k: &str| num(&main[k]).and_then(|v| u32::try_from(v).ok()).with_context(|| format!("{}: {k}", main_yml.display()));
    let (dc, ex) = (id("srv01_vmid")?, id("ex01_vmid")?);
    let mut names = BTreeMap::from([(dc, "ludius-dc01".to_string()), (ex, "ludius-ex01".to_string())]);
    for (u, v) in rangerun::vmids(&vms)? {
        names.insert(v, rangerun::vm_of(&u));
    }
    let names = Arc::new(names);
    let (n, a) = (names.clone(), agent.to_string());
    let verify = move || -> Result<Bad> {
        let (res, node) = HostAgent::new(&a, "")?.resources()?;
        Ok(violations(&n, &res, &node))
    };
    if strict {
        let bad = verify()?;
        if !bad.is_empty() {
            bail!("range clock refused, not range VMs: {}", listed(&bad));
        }
    }
    let (n, a) = (names.clone(), agent.to_string());
    let run = move |vmid: u32, script: &str| agent_exec(&a, n.get(&vmid).with_context(|| format!("VM {vmid}: not in the range"))?, script);
    let mut clock = RangeClock::new(dc, names.keys().copied().filter(|&v| v != dc).collect(), Box::new(run));
    clock.verify = Box::new(verify);
    Ok(clock)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::Duration;

    /// Fake guests: a clock per VM, offsets set by the script's FromUnixTimeMilliseconds.
    #[derive(Default)]
    struct Range {
        t: f64,
        off: BTreeMap<u32, f64>,
        sets: Vec<u32>,
        stuck: Option<u32>,
        down: BTreeMap<u32, u32>,
        dead: Option<u32>,
    }

    type R = Arc<Mutex<Range>>;

    fn range() -> R {
        Arc::new(Mutex::new(Range { t: 1_800_000_000.0, ..Default::default() }))
    }

    fn clock(r: &R, dc: u32, members: &[u32]) -> RangeClock {
        let (a, b, c) = (r.clone(), r.clone(), r.clone());
        let mut clock = RangeClock::new(
            dc,
            members.to_vec(),
            Box::new(move |vmid, script| {
                let mut r = a.lock().unwrap();
                if r.dead == Some(vmid) {
                    bail!("guest agent not running");
                }
                if let Some(n) = r.down.get_mut(&vmid).filter(|n| **n > 0) {
                    *n -= 1;
                    bail!(AgentDown("QEMU guest agent is not running".into()));
                }
                if let Some(ms) = regex::Regex::new(r"FromUnixTimeMilliseconds\((\d+)\)").unwrap().captures(script) {
                    r.sets.push(vmid);
                    if r.stuck != Some(vmid) {
                        let o = ms[1].parse::<f64>().unwrap() / 1000.0 - r.t;
                        r.off.insert(vmid, o);
                    }
                }
                Ok(((r.t + r.off.get(&vmid).copied().unwrap_or(0.0)) * 1000.0).to_string())
            }),
        );
        clock.now = Box::new(move || b.lock().unwrap().t);
        clock.sleep = Box::new(move |s| c.lock().unwrap().t += s);
        clock
    }

    fn at(r: &R, d: Duration) -> DateTime<Utc> {
        DateTime::from_timestamp(r.lock().unwrap().t as i64, 0).unwrap() + d
    }

    fn sets(r: &R) -> Vec<u32> {
        let mut s = r.lock().unwrap().sets.clone();
        s.sort();
        s
    }

    #[test]
    fn set_dc_first_then_reset() {
        let r = range();
        let c = clock(&r, 110, &[111, 121, 122]);
        let got = c.set(at(&r, Duration::days(4))).unwrap();
        assert_eq!(r.lock().unwrap().sets[0], 110);
        assert_eq!(sets(&r), [110, 111, 121, 122]);
        assert!(got.values().all(|v| (v - 4.0 * 86400.0).abs() < 1.0));
        assert!(c.set(at(&r, Duration::days(4) - Duration::hours(1))).unwrap_err().to_string().contains("forward only"));
        assert!(c.reset().unwrap().values().all(|v| v.abs() < 1.0));
    }

    #[test]
    fn a_guest_agent_that_is_down_for_a_while_is_retried() {
        let r = range();
        r.lock().unwrap().down.insert(111, 3);
        let got = clock(&r, 110, &[111]).set(at(&r, Duration::days(1))).unwrap();
        assert!(got.values().all(|v| (v - 86400.0).abs() < 1.0));
    }

    #[test]
    fn forward_only_holds_when_the_dc_check_is_retried_and_has_no_tolerance() {
        let r = range();
        r.lock().unwrap().down.insert(110, 3);
        let c = clock(&r, 110, &[111]);
        assert!(c.set(at(&r, Duration::seconds(5))).unwrap_err().to_string().contains("forward only"));
        for behind in [Duration::zero(), Duration::seconds(30)] {
            assert!(c.set(at(&r, -behind)).unwrap_err().to_string().contains("forward only"));
        }
        assert!(r.lock().unwrap().sets.is_empty());
    }

    #[test]
    fn member_that_does_not_move_fails() {
        let r = range();
        r.lock().unwrap().stuck = Some(122);
        assert!(clock(&r, 110, &[111, 122]).set(at(&r, Duration::days(1))).unwrap_err().to_string().contains("not converged"));
    }

    #[test]
    fn reset_tries_every_vm_when_the_dc_fails() {
        let r = range();
        let c = clock(&r, 110, &[111, 121]);
        c.set(at(&r, Duration::days(1))).unwrap();
        r.lock().unwrap().sets.clear();
        r.lock().unwrap().dead = Some(110);
        assert!(c.reset().unwrap_err().to_string().contains("VM 110: guest agent not running"));
        assert_eq!(sets(&r), [111, 121]);
        let g = r.lock().unwrap();
        assert!(g.off[&111].abs() < 1.0 && g.off[&121].abs() < 1.0);
    }

    #[test]
    fn set_and_reset_recheck_the_range() {
        let r = range();
        let bad = Arc::new(Mutex::new(Bad::new()));
        let mut c = clock(&r, 110, &[111, 121]);
        let b = bad.clone();
        c.verify = Box::new(move || Ok(b.lock().unwrap().clone()));
        c.set(at(&r, Duration::days(1))).unwrap();
        r.lock().unwrap().sets.clear();
        bad.lock().unwrap().insert(121, "web01 in pool other".into());
        assert!(c.set(at(&r, Duration::days(2))).unwrap_err().to_string().contains("121: web01"));
        assert!(r.lock().unwrap().sets.is_empty());
        assert!(c.reset().unwrap_err().to_string().contains("VM 121 skipped"));
        assert_eq!(sets(&r), [110, 111]);
        let g = r.lock().unwrap();
        assert!(g.off[&110].abs() < 1.0 && g.off[&111].abs() < 1.0);
    }

    #[test]
    fn violations_accept_range_vms_only() {
        let res = serde_json::json!([
            {"vmid": 110, "name": "ludius-dc01", "pool": "ludius", "node": "pve", "template": 0},
            {"vmid": 121, "name": "ludius-aberg", "pool": "ludius", "node": "pve", "template": 0},
        ]);
        let want = BTreeMap::from([(110, "ludius-dc01".to_string()), (121, "ludius-aberg".to_string())]);
        assert!(violations(&want, &res, "pve").is_empty());
        for (k, v) in [("pool", serde_json::json!("other")), ("name", "web01".into()), ("node", "pve2".into()), ("template", 1.into()), ("vmid", 999.into())] {
            let mut bad = res.clone();
            bad[1][k] = v;
            assert!(violations(&want, &bad, "pve").contains_key(&121), "{k}");
        }
    }
}
