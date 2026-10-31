mod error;
mod guest;
mod proxmox;
mod pve;
mod qmp;

use error::{E, R};
use ludius_proto::v1::host_agent_server::{HostAgent, HostAgentServer};
use ludius_proto::v1::*;
use proxmox::{Options, Proxmox};
use std::collections::HashSet;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tonic::{Request, Response, Status};

type Res<T> = Result<Response<T>, Status>;

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
}

struct Agent {
    pm: Arc<Proxmox>,
}

#[tonic::async_trait]
impl HostAgent for Agent {
    async fn input(&self, r: Request<InputRequest>) -> Res<InputResult> {
        use input_request::Action;
        let r = r.into_inner();
        let vm = self.pm.get(&r.vm).await?;
        let _gate = vm.input.lock().await;
        let pm = &self.pm;
        match r.action.ok_or(E::Arg("no action".into()))? {
            Action::Move(m) => pm.move_to(&vm, m.x, m.y, m.duration_ms).await,
            Action::Click(c) => pm.click(&vm, c.button + 1, c.count).await,
            Action::Key(k) => pm.key(&vm, k.vk, k.down).await,
            Action::Chord(c) => pm.chord(&vm, &c.vk).await,
            Action::Type(t) => pm.type_text(&vm, &t.text, t.mean_ms, t.jitter_ms).await,
            Action::CtrlAltDel(_) => pm.ctrl_alt_del(&vm).await,
            Action::Scroll(s) => pm.scroll(&vm, s.notches).await,
        }?;
        Ok(Response::new(InputResult { host_time_ms: now_ms() }))
    }

    async fn observe(&self, r: Request<ObserveRequest>) -> Res<ObserveResult> {
        let r = r.into_inner();
        let vm = self.pm.get(&r.vm).await?;
        let req = r.request.unwrap_or(SnapshotRequest { wait_settle: true, ..Default::default() });
        let timeout = Duration::from_millis((if req.timeout_ms == 0 { 5000 } else { req.timeout_ms }) as u64 + 10_000);
        let ask = ObserverRequest { id: 0, body: Some(observer_request::Body::Snapshot(req)) };
        let (res, hello) = vm.guest.request(ask, timeout).await?;
        let snapshot = match res.body {
            Some(observer_response::Body::Snapshot(s)) => s,
            Some(observer_response::Body::Error(e)) => return Err(E::Failed(format!("observer: {e}")).into()),
            _ => return Err(E::Failed("observer: no snapshot in the answer".into()).into()),
        };
        if snapshot.screen_width > 0 && snapshot.screen_height > 0 {
            *vm.screen.lock().unwrap() = Some((snapshot.screen_width, snapshot.screen_height));
        }
        Ok(Response::new(ObserveResult { snapshot: Some(snapshot), hello, host_time_ms: now_ms() }))
    }

    async fn lifecycle(&self, r: Request<LifecycleRequest>) -> Res<LifecycleResult> {
        let r = r.into_inner();
        let op = lifecycle_request::Op::try_from(r.op).map_err(|_| E::Arg("no such op".into()))?;
        let state = self.pm.lifecycle(&r.vm, op, &r.checkpoint, &r.target).await?;
        Ok(Response::new(LifecycleResult { state }))
    }

    async fn screenshot(&self, r: Request<ScreenshotRequest>) -> Res<ScreenshotResult> {
        let vm = self.pm.get(&r.into_inner().vm).await?;
        let (png, width, height) = self.pm.screenshot(&vm).await?;
        Ok(Response::new(ScreenshotResult { png, width, height }))
    }

    async fn read_guest_file(&self, r: Request<GuestFileRequest>) -> Res<FileContent> {
        let r = r.into_inner();
        let vm = self.pm.get(&r.vm).await?;
        let file = r.file.ok_or(E::Arg("no file".into()))?;
        let ask = ObserverRequest { id: 0, body: Some(observer_request::Body::ReadFile(file)) };
        let (res, _) = vm.guest.request(ask, Duration::from_secs(30)).await?;
        match res.body {
            Some(observer_response::Body::File(f)) => Ok(Response::new(f)),
            Some(observer_response::Body::Error(e)) if e == "unknown request" => Err(Status::unimplemented("observer has no read_file")),
            Some(observer_response::Body::Error(e)) => Err(E::Failed(format!("observer: {e}")).into()),
            _ => Err(E::Failed("observer: no file in the answer".into()).into()),
        }
    }

    async fn info(&self, _: Request<InfoRequest>) -> Res<AgentInfo> {
        Ok(Response::new(AgentInfo { backend: "proxmox".into(), host: self.pm.o.node.clone() }))
    }

    async fn guest_exec(&self, r: Request<GuestExecRequest>) -> Res<GuestExecResult> {
        let r = r.into_inner();
        let timeout = Duration::from_secs(if r.timeout_s == 0 { 60 } else { r.timeout_s.min(600) } as u64);
        Ok(Response::new(self.pm.guest_exec(&r.vm, &r.command, timeout).await?))
    }

    async fn resources(&self, _: Request<ResourcesRequest>) -> Res<ResourcesResult> {
        let json = self.pm.resources().await?.to_string();
        Ok(Response::new(ResourcesResult { node: self.pm.o.node.clone(), json }))
    }

    async fn list_vms(&self, _: Request<ListVmsRequest>) -> Res<ListVmsResult> {
        Err(Status::unimplemented("list_vms: not in this build yet"))
    }

    async fn frame(&self, _: Request<FrameRequest>) -> Res<FrameResult> {
        Err(Status::unimplemented("frame: not in this build yet"))
    }
}

fn env(k: &str, default: &str) -> String {
    std::env::var(k).unwrap_or_else(|_| default.into())
}

fn secret(path: &str) -> Option<String> {
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

fn parse_exempt(s: &str) -> R<HashSet<(u32, String)>> {
    s.split(',')
        .map(str::trim)
        .filter(|e| !e.is_empty())
        .map(|e| match e.split_once(':') {
            Some((id, name)) if name.starts_with("ludius-") && id.parse::<u32>().is_ok() => Ok((id.parse().unwrap(), name.to_string())),
            _ => Err(E::Arg(format!("PVE_CAP_EXEMPT entry {e} is not <vmid>:ludius-<name>"))),
        })
        .collect()
}

fn options() -> R<Options> {
    let max_running: usize = env("AGENT_MAX_RUNNING", "1").parse().map_err(|_| E::Arg("AGENT_MAX_RUNNING: not a number".into()))?;
    if max_running < 1 {
        return Err(E::Arg("AGENT_MAX_RUNNING must be at least 1".into()));
    }
    let res = env("PVE_RESOLUTION", "1920x1080");
    let screen = res
        .split_once('x')
        .and_then(|(w, h)| Some((w.parse().ok()?, h.parse().ok()?)))
        .ok_or_else(|| E::Arg(format!("PVE_RESOLUTION {res} is not WxH")))?;
    Ok(Options {
        node: std::env::var("PVE_NODE").map_err(|_| E::Arg("PVE_NODE is not set".into()))?,
        pool: env("PVE_POOL", ""),
        run_dir: env("PVE_RUN_DIR", "/var/run/qemu-server").into(),
        scratch_dir: env("AGENT_SCRATCH_DIR", "/run/ludius-agent").into(),
        vsock_port: env("AGENT_VSOCK_PORT", "4555").parse().map_err(|_| E::Arg("AGENT_VSOCK_PORT: not a number".into()))?,
        screen,
        poll: Duration::from_secs(1),
        recheck: Duration::from_secs(5),
        max_running,
        cap_exempt: parse_exempt(&env("PVE_CAP_EXEMPT", ""))?,
        reservation_ttl: Duration::from_secs(300),
    })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let o = options().map_err(|e| e.to_string())?;
    let token = secret(&env("PVE_TOKEN_FILE", "/run/secrets/pve_token")).ok_or("no PVE API token (PVE_TOKEN_FILE)")?;
    let ca = std::fs::read(env("PVE_CA", "/etc/pve/pve-root-ca.pem")).ok();
    let root = secret(&env("PVE_ROOT_PASSWORD_FILE", "/run/secrets/pve_root_password"));
    let api = pve::Pve::new(&env("PVE_URL", "https://127.0.0.1:8006"), token, ca.as_deref(), root).map_err(|e| e.to_string())?;
    let addr = env("AGENT_LISTEN", "127.0.0.1:7451").parse()?;
    eprintln!("ludius-host-agent: proxmox node {} pool {} on {addr}", o.node, o.pool);
    let agent = Agent { pm: Arc::new(Proxmox::new(api, o)) };
    tonic::transport::Server::builder()
        .add_service(HostAgentServer::new(agent))
        .serve_with_shutdown(addr, async {
            // PID 1 in a container gets no default SIGTERM action.
            let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
            tokio::select! { _ = term.recv() => {}, _ = tokio::signal::ctrl_c() => {} }
        })
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exempt() {
        let e = parse_exempt("110:ludius-srv01, 111:ludius-ex01,").unwrap();
        assert!(e.contains(&(110, "ludius-srv01".into())) && e.len() == 2);
        assert!(parse_exempt("110:srv01").is_err());
        assert!(parse_exempt("x:ludius-a").is_err());
    }
}
