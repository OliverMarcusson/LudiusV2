use crate::error::{E, R, down};
use crate::guest::Guest;
use crate::pve::Pve;
use crate::qmp::{self, Qmp};
use ludius_proto::v1::GuestExecResult;
use ludius_proto::v1::lifecycle_request::Op;
use regex::Regex;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

const ABS_MAX: i32 = 0x7FFF;

pub struct Options {
    pub node: String,
    pub pool: String,
    pub run_dir: PathBuf,
    pub scratch_dir: PathBuf,
    pub vsock_port: u32,
    pub screen: (i32, i32),
    pub poll: Duration,
    pub recheck: Duration,
    pub max_running: usize,
    pub cap_exempt: HashSet<(u32, String)>,
    pub reservation_ttl: Duration,
}

pub struct Vm {
    pub name: String,
    pub id: u32,
    identity: String,
    pub qmp: Qmp,
    pub guest: Guest,
    checked_at: Mutex<Instant>,
    pub input: tokio::sync::Mutex<()>,
    pointer: Mutex<Option<(i32, i32)>>,
    pub screen: Mutex<Option<(i32, i32)>>,
}

#[derive(Default)]
struct Cache {
    vms: HashMap<String, Arc<Vm>>,
    clock: u64,
    invalidated: HashMap<u32, u64>,
    configuring: HashMap<u32, u32>,
}

impl Cache {
    fn changed_since(&self, id: u32, seen: u64) -> bool {
        self.configuring.contains_key(&id) || self.invalidated.get(&id).is_some_and(|&c| c > seen)
    }

    fn invalidate(&mut self, id: u32) {
        self.clock += 1;
        self.invalidated.insert(id, self.clock);
        self.vms.retain(|_, v| v.id != id);
    }
}

#[derive(Debug, Clone)]
struct PveVm {
    id: u32,
    name: String,
    status: String,
    template: bool,
    pool: String,
}

pub struct Proxmox {
    api: Pve,
    pub o: Options,
    cache: Mutex<Cache>,
    /// Held while activating a VM; maps VMs being started to when, so the cap counts them.
    activation: tokio::sync::Mutex<HashMap<u32, Instant>>,
}

pub fn valid_name(name: &str) -> bool {
    static SAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[\w .-]{1,64}$").unwrap());
    SAFE.is_match(name)
}

fn raced(name: &str, id: u32) -> E {
    E::Down(format!("{name} (VM {id}) was deleted, cloned or reconfigured during the lookup: try again"))
}

pub fn cid(args: Option<&str>) -> Option<u32> {
    static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"vhost-vsock-pci\b[^ ]*\bguest-cid=(\d+)").unwrap());
    RE.captures(args?)?[1].parse().ok()
}

pub fn with_cid(args: Option<&str>, id: u32) -> String {
    static RE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(vhost-vsock-pci\b[^ ]*\bguest-cid=)\d+").unwrap());
    match args {
        Some(a) if cid(Some(a)).is_some() => RE.replace(a, format!("${{1}}{id}")).into_owned(),
        _ => format!("{} -device vhost-vsock-pci,guest-cid={id}", args.unwrap_or("")).trim().to_string(),
    }
}

fn own_cid(name: &str, id: u32, cfg: &Value) -> R<Option<u32>> {
    match cid(cfg["args"].as_str()) {
        Some(c) if c != id => Err(E::Down(format!("{name} (VM {id}) has vsock CID {c}, not its VMID: refused until its CID is set"))),
        c => Ok(c),
    }
}

pub fn identity_of(cfg: &Value) -> String {
    static UUID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\buuid=([0-9A-Fa-f-]+)").unwrap());
    let uuid = cfg["smbios1"].as_str().and_then(|s| UUID.captures(s)).map(|c| c[1].to_string()).unwrap_or_default();
    format!("{}|{uuid}", cfg["meta"].as_str().unwrap_or(""))
}

pub fn abs(pixel: i32, size: i32) -> i32 {
    ((pixel as f64 * ABS_MAX as f64 / (size - 1).max(1) as f64).round() as i32).clamp(0, ABS_MAX)
}

/// A human-ish pointer path: smoothstep easing along a slight arc, never overshooting either end.
pub fn path(from: Option<(i32, i32)>, x: i32, y: i32, steps: i32, bend: f64) -> Vec<(i32, i32)> {
    (1..=steps)
        .map(|i| {
            let t = i as f64 / steps as f64;
            let e = t * t * (3.0 - 2.0 * t);
            match from {
                Some((fx, fy)) if i < steps => {
                    let (dx, dy) = ((x - fx) as f64, (y - fy) as f64);
                    let arc = (std::f64::consts::PI * t).sin() * bend;
                    let px = ((fx as f64 + dx * e - dy * arc).round() as i32).clamp(fx.min(x), fx.max(x)).max(1);
                    let py = ((fy as f64 + dy * e + dx * arc).round() as i32).clamp(fy.min(y), fy.max(y)).max(1);
                    (px, py)
                }
                _ => (x, y),
            }
        })
        .collect()
}

pub fn steps(from: Option<(i32, i32)>, duration_ms: u32) -> i32 {
    if from.is_none() || duration_ms < 30 { 1 } else { (duration_ms / 15).clamp(2, 60) as i32 }
}

pub fn key_delay(mean_ms: u32, jitter_ms: u32) -> u64 {
    let mut d = mean_ms as f64 + (rand::random::<f64>() * 2.0 - 1.0) * jitter_ms as f64;
    if rand::random::<f64>() < 0.04 {
        d += mean_ms as f64 * (2.0 + rand::random::<f64>() * 4.0);
    }
    d.max(0.0) as u64
}

async fn pause(lo: u64, hi: u64) {
    tokio::time::sleep(Duration::from_millis(rand::random_range(lo..hi))).await;
}

impl Proxmox {
    pub fn new(api: Pve, o: Options) -> Self {
        Proxmox { api, o, cache: Mutex::default(), activation: tokio::sync::Mutex::default() }
    }

    fn cache(&self) -> std::sync::MutexGuard<'_, Cache> {
        self.cache.lock().unwrap()
    }

    pub async fn get(&self, name: &str) -> R<Arc<Vm>> {
        if !valid_name(name) {
            return Err(E::Arg("invalid VM name".into()));
        }
        let (cached, seen) = {
            let c = self.cache();
            (c.vms.get(name).cloned(), c.clock)
        };
        let Some(vm) = cached else { return self.resolve(name, seen).await };
        if vm.checked_at.lock().unwrap().elapsed() < self.o.recheck {
            return Ok(vm);
        }
        if let Err(e) = self.recheck(&vm).await {
            self.forget(name);
            return Err(e);
        }
        {
            let c = self.cache();
            if !c.changed_since(vm.id, seen) && c.vms.get(name).is_some_and(|v| Arc::ptr_eq(v, &vm)) {
                return Ok(vm);
            }
        }
        self.forget(name);
        Err(raced(name, vm.id))
    }

    async fn resolve(&self, name: &str, seen: u64) -> R<Arc<Vm>> {
        let id = self.find(name).await?.id;
        let cfg = self.config(id).await?;
        let vm = Arc::new(Vm {
            name: name.into(),
            id,
            identity: identity_of(&cfg),
            qmp: Qmp { path: self.o.run_dir.join(format!("{id}.qmp")) },
            guest: Guest::new(name, own_cid(name, id, &cfg).unwrap_or(None), self.o.vsock_port),
            checked_at: Mutex::new(Instant::now()),
            input: Default::default(),
            pointer: Mutex::new(None),
            screen: Mutex::new(None),
        });
        let mut c = self.cache();
        if c.changed_since(id, seen) {
            return Err(raced(name, id));
        }
        own_cid(name, id, &cfg)?;
        Ok(c.vms.entry(name.into()).or_insert(vm).clone())
    }

    async fn recheck(&self, vm: &Vm) -> R<()> {
        let now: Vec<PveVm> = self.list().await?.into_iter().filter(|v| v.id == vm.id).collect();
        let [v] = now.as_slice() else {
            return Err(E::NotFound(format!("{} (VM {}) is gone", vm.name, vm.id)));
        };
        if v.name != vm.name || !self.in_range(v) {
            return Err(E::Denied(format!("VM {} is no longer the range VM {} (pool {})", vm.id, vm.name, self.o.pool)));
        }
        let cfg = self.config(vm.id).await?;
        if identity_of(&cfg) != vm.identity {
            return Err(E::Denied(format!("VM {} named {} is a different VM than before (VMID reused)", vm.id, vm.name)));
        }
        own_cid(&vm.name, vm.id, &cfg)?;
        *vm.checked_at.lock().unwrap() = Instant::now();
        Ok(())
    }

    pub fn forget(&self, name: &str) {
        self.cache().vms.remove(name);
    }

    async fn config(&self, id: u32) -> R<Value> {
        self.api.get(&format!("/nodes/{}/qemu/{id}/config", self.o.node), &[]).await
    }

    async fn list(&self) -> R<Vec<PveVm>> {
        let all = self.api.get("/cluster/resources", &[("type", "vm")]).await?;
        Ok(all
            .as_array()
            .into_iter()
            .flatten()
            .filter(|v| v["node"] == self.o.node.as_str() && v["type"] == "qemu")
            .filter_map(|v| {
                Some(PveVm {
                    id: v["vmid"].as_u64()? as u32,
                    name: v["name"].as_str().unwrap_or("").into(),
                    status: v["status"].as_str().unwrap_or("").into(),
                    template: v["template"] == 1 || v["template"] == true || v["template"] == "1",
                    pool: v["pool"].as_str().unwrap_or("").into(),
                })
            })
            .collect())
    }

    /// The raw /cluster/resources list, for the controller's own range checks.
    pub async fn resources(&self) -> R<Value> {
        self.api.get("/cluster/resources", &[("type", "vm")]).await
    }

    fn in_range(&self, v: &PveVm) -> bool {
        !self.o.pool.is_empty() && v.pool == self.o.pool
    }

    fn exempt(&self, v: &PveVm) -> bool {
        self.o.cap_exempt.contains(&(v.id, v.name.clone()))
    }

    async fn find(&self, name: &str) -> R<PveVm> {
        let named: Vec<PveVm> = self.list().await?.into_iter().filter(|v| v.name == name).collect();
        if named.is_empty() {
            return Err(E::NotFound(format!("no VM named {name}")));
        }
        if self.o.pool.is_empty() {
            return Err(E::Denied("the pool is not set: no VM is in the range".into()));
        }
        if !named.iter().all(|v| self.in_range(v)) {
            return Err(E::Denied(format!("{name} is not a range VM (pool {})", self.o.pool)));
        }
        if named.len() > 1 {
            return Err(E::Failed(format!("more than one VM named {name}")));
        }
        Ok(named.into_iter().next().unwrap())
    }

    // ---- input ----

    fn screen_of(&self, vm: &Vm) -> (i32, i32) {
        vm.screen.lock().unwrap().unwrap_or(self.o.screen)
    }

    async fn send(vm: &Vm, events: Vec<Value>) -> R<()> {
        vm.qmp.execute("input-send-event", Some(json!({ "events": events }))).await.map(|_| ())
    }

    pub async fn move_to(&self, vm: &Vm, x: i32, y: i32, duration_ms: u32) -> R<()> {
        let (w, h) = self.screen_of(vm);
        if x < 0 || y < 0 || x >= w || y >= h {
            return Err(E::Arg(format!("({x}, {y}) is off the {w}x{h} screen")));
        }
        let from = *vm.pointer.lock().unwrap();
        let n = steps(from, duration_ms);
        let bend = (rand::random::<f64>() - 0.5) * 0.25;
        for (i, (px, py)) in path(from, x, y, n, bend).into_iter().enumerate() {
            Self::send(vm, vec![qmp::abs("x", abs(px, w)), qmp::abs("y", abs(py, h))]).await?;
            if (i as i32) + 1 < n {
                tokio::time::sleep(Duration::from_millis((duration_ms / n as u32) as u64)).await;
            }
        }
        *vm.pointer.lock().unwrap() = Some((x, y));
        Ok(())
    }

    /// `button`: 1 left, 2 right, 3 middle.
    pub async fn click(&self, vm: &Vm, button: i32, count: u32) -> R<()> {
        let name = match button {
            1 => "left",
            2 => "right",
            3 => "middle",
            _ => return Err(E::Arg("no such button".into())),
        };
        for i in 0..count.max(1) {
            Self::send(vm, vec![qmp::button(name, true)]).await?;
            pause(45, 95).await;
            Self::send(vm, vec![qmp::button(name, false)]).await?;
            if i + 1 < count {
                pause(70, 130).await;
            }
        }
        Ok(())
    }

    pub async fn key(&self, vm: &Vm, vk: u32, down: bool) -> R<()> {
        Self::send(vm, vec![qmp::key(&qmp::qcode(vk)?, down)]).await
    }

    pub async fn chord(&self, vm: &Vm, keys: &[u32]) -> R<()> {
        let codes = keys.iter().map(|&k| qmp::qcode(k)).collect::<R<Vec<_>>>()?;
        for k in &codes {
            Self::send(vm, vec![qmp::key(k, true)]).await?;
            pause(20, 50).await;
        }
        for k in codes.iter().rev() {
            Self::send(vm, vec![qmp::key(k, false)]).await?;
            pause(15, 35).await;
        }
        Ok(())
    }

    pub async fn type_text(&self, vm: &Vm, text: &str, mean_ms: u32, jitter_ms: u32) -> R<()> {
        let keys = text.chars().map(qmp::for_char).collect::<R<Vec<_>>>()?;
        for (q, shift) in keys {
            if shift {
                Self::send(vm, vec![qmp::key("shift", true)]).await?;
            }
            Self::send(vm, vec![qmp::key(&q, true)]).await?;
            pause(25, 60).await;
            Self::send(vm, vec![qmp::key(&q, false)]).await?;
            if shift {
                Self::send(vm, vec![qmp::key("shift", false)]).await?;
            }
            let d = key_delay(mean_ms, jitter_ms);
            if d > 0 {
                tokio::time::sleep(Duration::from_millis(d)).await;
            }
        }
        Ok(())
    }

    pub async fn ctrl_alt_del(&self, vm: &Vm) -> R<()> {
        let keys: Vec<Value> = ["ctrl", "alt", "delete"].iter().map(|k| json!({ "type": "qcode", "data": k })).collect();
        vm.qmp.execute("send-key", Some(json!({ "keys": keys }))).await.map(|_| ())
    }

    pub async fn scroll(&self, vm: &Vm, notches: i32) -> R<()> {
        let wheel = if notches > 0 { "wheel-up" } else { "wheel-down" };
        for _ in 0..notches.unsigned_abs() {
            Self::send(vm, vec![qmp::button(wheel, true), qmp::button(wheel, false)]).await?;
            pause(40, 110).await;
        }
        Ok(())
    }

    /// QEMU writes the PNG itself, so the scratch dir must be the same path on the host and here.
    pub async fn screenshot(&self, vm: &Vm) -> R<(Vec<u8>, i32, i32)> {
        use std::os::unix::fs::DirBuilderExt;
        std::fs::DirBuilder::new().recursive(true).mode(0o700).create(&self.o.scratch_dir).map_err(down)?;
        let file = self.o.scratch_dir.join(format!("{:032x}.png", rand::random::<u128>()));
        let res = async {
            vm.qmp.execute("screendump", Some(json!({ "filename": file, "format": "png" }))).await?;
            let png = tokio::fs::read(&file).await.map_err(down)?;
            if png.len() < 24 {
                return Err(down("screendump: short PNG"));
            }
            let be = |o: usize| i32::from_be_bytes(png[o..o + 4].try_into().unwrap());
            let (w, h) = (be(16), be(20));
            Ok((png, w, h))
        }
        .await;
        let _ = std::fs::remove_file(&file);
        res
    }

    // ---- guest agent ----

    /// Runs argv in the guest through the QEMU guest agent (PVE agent/exec); range VMs only.
    pub async fn guest_exec(&self, name: &str, command: &[String], timeout: Duration) -> R<GuestExecResult> {
        if !valid_name(name) {
            return Err(E::Arg("invalid VM name".into()));
        }
        if command.is_empty() {
            return Err(E::Arg("no command".into()));
        }
        let vm = self.find(name).await?;
        let at = format!("/nodes/{}/qemu/{}/agent", self.o.node, vm.id);
        let form: Vec<(&str, &str)> = command.iter().map(|c| ("command", c.as_str())).collect();
        let started = self.api.post(&format!("{at}/exec"), &form).await.map_err(agent_down)?;
        let pid = started["pid"].as_u64().ok_or_else(|| down("agent/exec: no pid"))?.to_string();
        let deadline = Instant::now() + timeout;
        loop {
            let s = self.api.get(&format!("{at}/exec-status"), &[("pid", &pid)]).await.map_err(agent_down)?;
            if s["exited"] == 1 || s["exited"] == true {
                return Ok(GuestExecResult {
                    exited: true,
                    exitcode: s["exitcode"].as_i64().unwrap_or(-1) as i32,
                    out_data: s["out-data"].as_str().unwrap_or("").into(),
                    err_data: s["err-data"].as_str().unwrap_or("").into(),
                });
            }
            if Instant::now() >= deadline {
                return Ok(GuestExecResult { exited: false, exitcode: -1, ..Default::default() });
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }

    // ---- lifecycle ----

    pub async fn lifecycle(&self, name: &str, op: Op, checkpoint: &str, target: &str) -> R<String> {
        if !valid_name(name) || (!checkpoint.is_empty() && !valid_name(checkpoint)) || (!target.is_empty() && !valid_name(target)) {
            return Err(E::Arg("invalid name".into()));
        }
        if op == Op::Clone && !target.starts_with("ludius-") {
            return Err(E::Arg("clone needs a target named ludius-*".into()));
        }
        if matches!(op, Op::Checkpoint | Op::Restore) && checkpoint.is_empty() {
            return Err(E::Arg("checkpoint and restore need a checkpoint name".into()));
        }
        if op == Op::Delete && (!name.starts_with("ludius-") || target != name) {
            return Err(E::Arg("delete is limited to ludius-* VMs and needs the VM name repeated as confirmation".into()));
        }
        let vm = self.find(name).await?;
        if op != Op::State {
            self.forget(name);
            self.cache().invalidate(vm.id);
        }
        let at = format!("/nodes/{}/qemu/{}", self.o.node, vm.id);
        match op {
            Op::State => {}
            Op::Start => {
                if vm.status != "running" {
                    self.capped(&vm, self.api.post(&format!("{at}/status/start"), &[])).await?;
                }
            }
            Op::Stop => {
                self.complete(self.api.post(&format!("{at}/status/shutdown"), &[("forceStop", "1"), ("timeout", "120")]).await?)
                    .await?;
            }
            Op::Checkpoint => {
                let snaps = self.api.get(&format!("{at}/snapshot"), &[]).await?;
                if snaps.as_array().into_iter().flatten().any(|s| s["name"] == checkpoint) {
                    return Err(E::Failed(format!(
                        "checkpoint '{checkpoint}' exists on {name} and is never duplicated or replaced: use a new name"
                    )));
                }
                let vmstate = if vm.status == "running" { "1" } else { "0" };
                self.complete(self.api.post(&format!("{at}/snapshot"), &[("snapname", checkpoint), ("vmstate", vmstate)]).await?)
                    .await?;
            }
            Op::Restore => {
                self.capped(&vm, self.api.post(&format!("{at}/snapshot/{checkpoint}/rollback"), &[])).await?;
            }
            Op::Clone => return self.clone_vm(&vm, checkpoint, target).await,
            Op::Delete => {
                if vm.status == "running" {
                    self.complete(self.api.post(&format!("{at}/status/stop"), &[]).await?).await?;
                }
                self.complete(self.api.delete(&at, &[("purge", "1"), ("destroy-unreferenced-disks", "1")]).await?).await?;
                self.cache().invalidate(vm.id);
                return Ok("Deleted".into());
            }
        }
        self.state(vm.id).await
    }

    /// Activates `vm` under the running-VM cap; a VM whose start is in flight counts until the reservation expires.
    async fn capped(&self, vm: &PveVm, activate: impl Future<Output = R<Value>>) -> R<()> {
        let mut reserved = self.activation.lock().await;
        if !self.exempt(vm) {
            let mut up = vec![];
            for v in self.list().await?.iter().filter(|v| self.in_range(v) && !v.template && v.id != vm.id && !self.exempt(v)) {
                let off = self.state(v.id).await? == "Off";
                if !off {
                    reserved.remove(&v.id);
                }
                if !off || reserved.get(&v.id).is_some_and(|at| at.elapsed() < self.o.reservation_ttl) {
                    up.push(v.name.clone());
                }
            }
            if up.len() >= self.o.max_running {
                return Err(E::Failed(format!(
                    "VM cap {} (max running): {} up; {} not activated",
                    self.o.max_running,
                    up.join(", "),
                    vm.name
                )));
            }
        }
        reserved.insert(vm.id, Instant::now());
        let res = match activate.await {
            Ok(upid) => self.complete(upid).await,
            Err(e) => Err(e),
        };
        if matches!(res, Ok(()) | Err(E::Task(_))) {
            reserved.remove(&vm.id);
        }
        res
    }

    async fn free_id(&self) -> R<u32> {
        let parse = |v: Value| -> R<u32> {
            v.as_str().and_then(|s| s.parse().ok()).or(v.as_u64().map(|n| n as u32)).ok_or_else(|| down("nextid: not a number"))
        };
        let mut id = parse(self.api.get("/cluster/nextid", &[]).await?)?;
        let exempt: HashSet<u32> = self.o.cap_exempt.iter().map(|e| e.0).collect();
        while exempt.contains(&id) {
            id = exempt.iter().filter(|&&e| e >= id).max().unwrap() + 1;
            id = parse(self.api.get("/cluster/nextid", &[("vmid", &id.to_string())]).await?)?;
        }
        Ok(id)
    }

    async fn clone_vm(&self, src: &PveVm, checkpoint: &str, target: &str) -> R<String> {
        if !src.template && checkpoint.is_empty() {
            return Err(E::Arg("clone of a VM (not a template) needs a checkpoint".into()));
        }
        if self.list().await?.iter().any(|v| v.name == target) {
            return Err(E::Failed(format!("a VM named {target} exists")));
        }
        let id = self.free_id().await?;
        let new_id = id.to_string();
        let full = if src.template { "0" } else { "1" };
        let mut args = vec![("newid", new_id.as_str()), ("name", target), ("full", full), ("pool", self.o.pool.as_str())];
        if !checkpoint.is_empty() {
            args.push(("snapname", checkpoint));
        }
        {
            let mut c = self.cache();
            *c.configuring.entry(id).or_default() += 1;
            c.invalidate(id);
            c.vms.remove(target);
        }
        let res = async {
            self.complete(self.api.post(&format!("/nodes/{}/qemu/{}/clone", self.o.node, src.id), &args).await?).await?;
            let cfg = self.config(id).await?;
            let path = format!("/nodes/{}/qemu/{id}/config", self.o.node);
            self.api.put_as_root(&path, &[("args", &with_cid(cfg["args"].as_str(), id))]).await?;
            if cid(self.config(id).await?["args"].as_str()) != Some(id) {
                return Err(E::Down(format!("{target} (VM {id}) did not take its vsock CID: refused until it is set")));
            }
            Ok(())
        }
        .await;
        {
            let mut c = self.cache();
            let n = c.configuring.get_mut(&id).unwrap();
            *n -= 1;
            if *n == 0 {
                c.configuring.remove(&id);
            }
            c.invalidate(id);
            c.vms.remove(target);
        }
        res?;
        self.state(id).await
    }

    async fn state(&self, id: u32) -> R<String> {
        let s = self.api.get(&format!("/nodes/{}/qemu/{id}/status/current", self.o.node), &[]).await?;
        let st = s["qmpstatus"].as_str().or(s["status"].as_str());
        Ok(match st {
            Some("running") => "Running",
            Some("stopped") => "Off",
            Some("paused" | "suspended") => "Paused",
            Some(other) => other,
            None => "Unknown",
        }
        .into())
    }

    /// Waits for a PVE task (UPID) to stop; a failed task is `E::Task`.
    async fn complete(&self, upid: Value) -> R<()> {
        let Some(upid) = upid.as_str().filter(|u| u.starts_with("UPID:")) else { return Ok(()) };
        loop {
            let s = self.api.get(&format!("/nodes/{}/tasks/{upid}/status", self.o.node), &[]).await?;
            if s["status"] == "stopped" {
                return match s["exitstatus"].as_str() {
                    Some("OK") => Ok(()),
                    exit => Err(E::Task(format!("{upid}: {}", exit.unwrap_or("?")))),
                };
            }
            tokio::time::sleep(self.o.poll).await;
        }
    }
}

fn agent_down(e: E) -> E {
    match e {
        E::Failed(m) if m.contains("agent") => E::Down(m),
        e => e,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cid_args() {
        assert_eq!(cid(Some("-device vhost-vsock-pci,guest-cid=121")), Some(121));
        assert_eq!(cid(Some("-cpu host")), None);
        assert_eq!(cid(None), None);
        assert_eq!(with_cid(None, 7), "-device vhost-vsock-pci,guest-cid=7");
        assert_eq!(with_cid(Some("-cpu host"), 7), "-cpu host -device vhost-vsock-pci,guest-cid=7");
        assert_eq!(with_cid(Some("-device vhost-vsock-pci,guest-cid=121 -x"), 130), "-device vhost-vsock-pci,guest-cid=130 -x");
        let cfg = json!({ "args": "-device vhost-vsock-pci,guest-cid=5" });
        assert!(own_cid("a", 6, &cfg).is_err());
        assert_eq!(own_cid("a", 5, &cfg).unwrap(), Some(5));
    }

    #[test]
    fn identity() {
        let cfg = json!({ "meta": "creation-qemu=9.0,ctime=1", "smbios1": "uuid=ab-12,manufacturer=x" });
        assert_eq!(identity_of(&cfg), "creation-qemu=9.0,ctime=1|ab-12");
        assert_eq!(identity_of(&json!({})), "|");
    }

    #[test]
    fn pointer_path() {
        assert_eq!(steps(None, 500), 1);
        assert_eq!(steps(Some((0, 0)), 20), 1);
        assert_eq!(steps(Some((0, 0)), 3000), 60);
        let p = path(Some((10, 10)), 200, 100, 20, 0.12);
        assert_eq!(p.len(), 20);
        assert_eq!(*p.last().unwrap(), (200, 100));
        assert!(p.iter().all(|&(x, y)| (10..=200).contains(&x) && (10..=100).contains(&y)));
        assert_eq!(path(None, 5, 6, 1, 0.0), vec![(5, 6)]);
        assert_eq!(abs(0, 1920), 0);
        assert_eq!(abs(1919, 1920), ABS_MAX);
    }

    #[test]
    fn names() {
        assert!(valid_name("ludius-ws01"));
        assert!(!valid_name("a/b"));
        assert!(!valid_name(""));
    }
}
