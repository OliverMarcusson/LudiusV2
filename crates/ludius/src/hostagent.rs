//! Blocking client for the host agent (gRPC); one small runtime per client.
use std::time::Duration;

use anyhow::Result;
use ludius_proto::v1::host_agent_client::HostAgentClient;
use ludius_proto::v1::{self as pb, input_request::Action};
use tonic::transport::Channel;
use tonic::{Code, Request, Status};

pub struct HostAgent {
    pub address: String,
    pub vm: String,
    rt: tokio::runtime::Runtime,
    client: HostAgentClient<Channel>,
}

fn req<T>(msg: T, secs: u64) -> Request<T> {
    let mut r = Request::new(msg);
    r.set_timeout(Duration::from_secs(secs));
    r
}

pub fn code(e: &anyhow::Error) -> Option<Code> {
    e.downcast_ref::<Status>().map(Status::code)
}

impl HostAgent {
    pub fn new(address: &str, vm: &str) -> Result<Self> {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build()?;
        let url = if address.contains("://") { address.to_string() } else { format!("http://{address}") };
        let channel = {
            let _g = rt.enter();
            Channel::from_shared(url)?.connect_lazy()
        };
        let client = HostAgentClient::new(channel).max_decoding_message_size(64 << 20);
        Ok(Self { address: address.into(), vm: vm.into(), rt, client })
    }

    pub fn from_env(vm: &str) -> Result<Self> {
        Self::new(&std::env::var("LUDIUS_AGENT").unwrap_or_else(|_| "127.0.0.1:7451".into()), vm)
    }

    fn input(&self, action: Action) -> Result<i64> {
        let mut c = self.client.clone();
        let r = self.rt.block_on(c.input(req(pb::InputRequest { vm: self.vm.clone(), action: Some(action) }, 120)))?;
        Ok(r.into_inner().host_time_ms)
    }

    pub fn move_to(&self, x: i32, y: i32, duration_ms: u32) -> Result<i64> {
        self.input(Action::Move(pb::Move { x, y, duration_ms }))
    }

    pub fn click(&self, button: pb::click::Button, count: u32) -> Result<i64> {
        self.input(Action::Click(pb::Click { button: button as i32, count }))
    }

    pub fn key(&self, vk: u32, down: bool) -> Result<i64> {
        self.input(Action::Key(pb::Key { vk, down }))
    }

    pub fn chord(&self, vk: Vec<u32>) -> Result<i64> {
        self.input(Action::Chord(pb::Chord { vk }))
    }

    pub fn type_text(&self, text: &str, mean_ms: u32, jitter_ms: u32) -> Result<i64> {
        self.input(Action::Type(pb::TypeText { text: text.into(), mean_ms, jitter_ms }))
    }

    pub fn scroll(&self, notches: i32) -> Result<i64> {
        self.input(Action::Scroll(pb::Scroll { notches }))
    }

    pub fn ctrl_alt_del(&self) -> Result<i64> {
        self.input(Action::CtrlAltDel(pb::CtrlAltDel {}))
    }

    /// Retries INTERNAL (0.5 s) and UNAVAILABLE (3 s) up to three attempts, like the Python client.
    pub fn observe(&self, wait_settle: bool, quiet_ms: u32, timeout_ms: u32) -> Result<pb::ObserveResult> {
        let request = pb::SnapshotRequest { wait_settle, quiet_ms, timeout_ms };
        for attempt in 0..3 {
            let mut c = self.client.clone();
            match self.rt.block_on(c.observe(req(pb::ObserveRequest { vm: self.vm.clone(), request: Some(request) }, 60))) {
                Ok(r) => return Ok(r.into_inner()),
                Err(s) if attempt < 2 && matches!(s.code(), Code::Internal | Code::Unavailable) => {
                    std::thread::sleep(Duration::from_millis(if s.code() == Code::Internal { 500 } else { 3000 }));
                }
                Err(s) => return Err(s.into()),
            }
        }
        unreachable!()
    }

    pub fn lifecycle(&self, op: pb::lifecycle_request::Op, checkpoint: &str, target: &str) -> Result<String> {
        let mut c = self.client.clone();
        let m = pb::LifecycleRequest { vm: self.vm.clone(), op: op as i32, checkpoint: checkpoint.into(), target: target.into() };
        Ok(self.rt.block_on(c.lifecycle(req(m, 1800)))?.into_inner().state)
    }

    pub fn read_file(&self, path: &str, max_bytes: u32) -> Result<pb::FileContent> {
        let mut c = self.client.clone();
        let m = pb::GuestFileRequest { vm: self.vm.clone(), file: Some(pb::ReadFile { path: path.into(), max_bytes }) };
        Ok(self.rt.block_on(c.read_guest_file(req(m, 60)))?.into_inner())
    }

    pub fn info(&self) -> Result<pb::AgentInfo> {
        let mut c = self.client.clone();
        Ok(self.rt.block_on(c.info(req(pb::InfoRequest {}, 10)))?.into_inner())
    }

    pub fn guest_exec(&self, command: Vec<String>, timeout_s: u32) -> Result<pb::GuestExecResult> {
        let mut c = self.client.clone();
        let m = pb::GuestExecRequest { vm: self.vm.clone(), command, timeout_s };
        Ok(self.rt.block_on(c.guest_exec(req(m, u64::from(timeout_s) + 30)))?.into_inner())
    }

    pub fn resources(&self) -> Result<(serde_json::Value, String)> {
        let mut c = self.client.clone();
        let r = self.rt.block_on(c.resources(req(pb::ResourcesRequest {}, 60)))?.into_inner();
        Ok((serde_json::from_str(&r.json)?, r.node))
    }

    pub fn screenshot(&self) -> Result<Vec<u8>> {
        let mut c = self.client.clone();
        let m = pb::ScreenshotRequest { vm: self.vm.clone() };
        Ok(self.rt.block_on(c.screenshot(req(m, 60)))?.into_inner().png)
    }
}
