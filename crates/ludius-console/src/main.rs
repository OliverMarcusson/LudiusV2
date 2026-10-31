//! The monitor console: serves web/console/dist and its /api. VM lists and console frames come from
//! the host agent over gRPC; everything else is read from the runs directory and controller data.
mod console;
mod portraits;

use axum::Router;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, Query, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use ludius_proto::v1::host_agent_client::HostAgentClient;
use ludius_proto::v1::{FrameRequest, ListVmsRequest};
use regex::Regex;
use serde_json::{Value, json};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{Semaphore, watch};
use tonic::transport::Channel;

pub struct Config {
    pub runs: PathBuf,
    pub personas: PathBuf,
    pub templates: PathBuf,
    pub portraits: PathBuf,
    pub include: Regex,
    pub allowed_origins: HashSet<String>,
    pub allowed_hosts: HashSet<String>,
    pub openrouter_key: Option<String>,
    pub openrouter_key_file: Option<PathBuf>,
}

#[derive(Clone)]
pub struct App {
    pub cfg: Arc<Config>,
    agent: HostAgentClient<Channel>,
    feeds: Arc<Mutex<HashMap<(String, i32), Feed>>>,
    pub image_gate: Arc<tokio::sync::Mutex<()>>,
    pub http: reqwest::Client,
}

pub fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis() as i64
}

/// An ASP.NET-style problem body; the UI shows `detail`.
pub fn problem(status: StatusCode, detail: &str) -> Response {
    let body = json!({ "title": status.canonical_reason(), "status": status.as_u16(), "detail": detail });
    (status, [(header::CONTENT_TYPE, "application/problem+json")], body.to_string()).into_response()
}

fn env(k: &str, default: &str) -> String {
    std::env::var(k).ok().filter(|v| !v.is_empty()).unwrap_or_else(|| default.into())
}

/// `https://monitor.example` entries, `;`- or `,`-separated; anything else is a config error.
pub fn parse_origins(value: &str) -> Result<HashSet<String>, String> {
    static ORIGIN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$").unwrap());
    value
        .split([';', ','])
        .map(str::trim)
        .filter(|e| !e.is_empty())
        .map(|e| if ORIGIN.is_match(e) { Ok(e.to_lowercase()) } else { Err(format!("CONSOLE_ALLOWED_ORIGINS: {e} is not an origin like https://monitor.example")) })
        .collect()
}

pub fn origin_allowed(origin: &str, host: &str, allowed: &HashSet<String>) -> bool {
    origin.is_empty() || origin.split_once("://").is_some_and(|(_, authority)| authority.eq_ignore_ascii_case(host)) || allowed.contains(&origin.to_lowercase())
}

fn header(h: &HeaderMap, name: header::HeaderName) -> &str {
    h.get(name).and_then(|v| v.to_str().ok()).unwrap_or("")
}

pub fn origin_ok(app: &App, h: &HeaderMap) -> bool {
    origin_allowed(header(h, header::ORIGIN), header(h, header::HOST), &app.cfg.allowed_origins)
}

/// Host filtering (DNS rebinding) and the console's security headers.
async fn guard(State(app): State<App>, req: Request, next: Next) -> Response {
    let host = header(req.headers(), header::HOST);
    let name = host.rsplit_once(':').filter(|(_, p)| p.parse::<u16>().is_ok()).map_or(host, |(n, _)| n);
    if !app.cfg.allowed_hosts.contains(&name.to_lowercase()) {
        return (StatusCode::BAD_REQUEST, "Invalid Host header").into_response();
    }
    let mut res = next.run(req).await;
    let h = res.headers_mut();
    h.insert(
        header::CONTENT_SECURITY_POLICY,
        HeaderValue::from_static("default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"),
    );
    h.insert(header::X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    h.insert(header::REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    res
}

async fn vms(State(app): State<App>) -> Response {
    match app.agent.clone().list_vms(ListVmsRequest {}).await {
        Ok(r) => {
            let list: Vec<Value> = r
                .into_inner()
                .vms
                .into_iter()
                .filter(|v| app.cfg.include.is_match(&v.name))
                .map(|v| json!({ "name": v.name, "state": v.state, "width": v.width, "height": v.height }))
                .collect();
            axum::Json(list).into_response()
        }
        Err(e) => problem(StatusCode::SERVICE_UNAVAILABLE, &format!("host agent: {}", e.message())),
    }
}

async fn calls(State(app): State<App>) -> Response {
    let text = match std::fs::read_to_string(app.cfg.runs.join("openrouter-calls.json")) {
        Ok(t) => t,
        Err(_) => return problem(StatusCode::SERVICE_UNAVAILABLE, "Call counters unavailable"),
    };
    let body = match serde_json::from_str::<Value>(&text) {
        Ok(Value::Object(mut o)) => {
            if !o.contains_key("decision")
                && let Some(old) = o.remove("jev")
            {
                o.insert("decision".into(), old);
            }
            Value::Object(o).to_string()
        }
        Ok(_) => text,
        Err(_) => return problem(StatusCode::SERVICE_UNAVAILABLE, "Call counters unavailable"),
    };
    ([(header::CONTENT_TYPE, "application/json")], body).into_response()
}

async fn interactions(State(app): State<App>) -> Response {
    match console::last_lines(&app.cfg.runs.join("monitor-interactions.jsonl"), 20) {
        Ok(lines) => axum::Json(json!({ "interactions": lines })).into_response(),
        Err(_) => problem(StatusCode::SERVICE_UNAVAILABLE, "Interactions log unreadable"),
    }
}

async fn admission(State(app): State<App>) -> Response {
    match console::admission(&app.cfg.runs.join("admission"), now_ms()) {
        Ok(v) => axum::Json(v).into_response(),
        Err(_) => problem(StatusCode::SERVICE_UNAVAILABLE, "Admission log unreadable"),
    }
}

// --- console stream ---------------------------------------------------------------------------

const FPS: u64 = 15;
const MAX_IN_FLIGHT: usize = 3;
const SLOW_VIEWER: Duration = Duration::from_secs(10);

/// The newest frame of one (VM, width) feed; every viewer of it holds a receiver.
type Feed = watch::Sender<Option<Frame>>;

#[derive(Clone, PartialEq)]
enum Frame {
    Jpeg(Arc<Vec<u8>>),
    Error(String),
}

pub fn valid_vm_name(name: &str) -> bool {
    static SAFE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[\w .-]{1,64}$").unwrap());
    SAFE.is_match(name)
}

#[derive(serde::Deserialize)]
struct Width {
    w: Option<i32>,
}

async fn stream(
    State(app): State<App>,
    Path(name): Path<String>,
    Query(q): Query<Width>,
    headers: HeaderMap,
    ws: Result<WebSocketUpgrade, axum::extract::ws::rejection::WebSocketUpgradeRejection>,
) -> Response {
    if !app.cfg.include.is_match(&name) || !valid_vm_name(&name) {
        return StatusCode::NOT_FOUND.into_response();
    }
    let Ok(ws) = ws else { return (StatusCode::BAD_REQUEST, "WebSocket only").into_response() };
    if !origin_ok(&app, &headers) {
        return StatusCode::FORBIDDEN.into_response();
    }
    let width = q.w.unwrap_or(640).clamp(160, 1920);
    ws.on_upgrade(move |socket| view(app, name, width, socket))
}

async fn view(app: App, vm: String, width: i32, mut ws: WebSocket) {
    let mut rx = subscribe(&app, &vm, width);
    loop {
        tokio::select! {
            changed = rx.changed() => {
                if changed.is_err() {
                    break;
                }
                let frame = rx.borrow_and_update().clone();
                let msg = match frame {
                    Some(Frame::Jpeg(j)) => Message::Binary(j.to_vec().into()),
                    Some(Frame::Error(e)) => Message::Text(e.into()),
                    None => continue,
                };
                if !matches!(tokio::time::timeout(SLOW_VIEWER, ws.send(msg)).await, Ok(Ok(()))) {
                    return;
                }
            }
            // Any message or a close from the viewer ends the stream.
            _ = ws.recv() => break,
        }
    }
    let _ = ws.send(Message::Close(None)).await;
}

/// One feed per (VM, width), shared by its viewers; it stops when the last viewer leaves.
fn subscribe(app: &App, vm: &str, width: i32) -> watch::Receiver<Option<Frame>> {
    let mut feeds = app.feeds.lock().unwrap();
    if let Some(tx) = feeds.get(&(vm.to_string(), width)) {
        let mut rx = tx.subscribe();
        rx.mark_changed();
        return rx;
    }
    let (tx, rx) = watch::channel(None);
    feeds.insert((vm.to_string(), width), tx.clone());
    tokio::spawn(feed(app.clone(), vm.to_string(), width, tx));
    rx
}

struct Published {
    sent: u64,
    last: Option<(Frame, Instant)>,
    quiet_until: Instant,
}

async fn feed(app: App, vm: String, width: i32, tx: watch::Sender<Option<Frame>>) {
    let slots = Arc::new(Semaphore::new(MAX_IN_FLIGHT));
    let state = Arc::new(Mutex::new(Published { sent: 0, last: None, quiet_until: Instant::now() }));
    let mut tick = tokio::time::interval(Duration::from_millis(1000 / FPS));
    let mut seq = 0u64;
    loop {
        tick.tick().await;
        {
            let mut feeds = app.feeds.lock().unwrap();
            if tx.receiver_count() == 0 {
                feeds.remove(&(vm.clone(), width));
                return;
            }
        }
        if Instant::now() < state.lock().unwrap().quiet_until {
            continue;
        }
        let Ok(slot) = slots.clone().try_acquire_owned() else { continue };
        seq += 1;
        let (n, mut agent, vm, tx, state) = (seq, app.agent.clone(), vm.clone(), tx.clone(), state.clone());
        tokio::spawn(async move {
            let _slot = slot;
            let frame = match agent.frame(FrameRequest { vm, max_width: width }).await {
                Ok(r) => Frame::Jpeg(Arc::new(r.into_inner().jpeg)),
                Err(e) => {
                    state.lock().unwrap().quiet_until = Instant::now() + Duration::from_secs(1);
                    Frame::Error(match e.code() {
                        tonic::Code::Internal | tonic::Code::Unknown => "capture failed".into(),
                        _ => e.message().to_string(),
                    })
                }
            };
            let mut st = state.lock().unwrap();
            if n <= st.sent {
                return;
            }
            st.sent = n;
            let now = Instant::now();
            // An unchanged frame is re-sent at most once a second.
            if matches!(&frame, Frame::Jpeg(_)) && st.last.as_ref().is_some_and(|(f, at)| *f == frame && now - *at < Duration::from_secs(1)) {
                return;
            }
            st.last = Some((frame.clone(), now));
            tx.send_replace(Some(frame));
        });
    }
}

// --- startup ----------------------------------------------------------------------------------

fn config() -> Result<Config, String> {
    let runs = PathBuf::from(env("LUDIUS_RUNS", "/var/lib/ludius/runs"));
    let data = PathBuf::from(env("LUDIUS_DATA", "/etc/ludius/data"));
    Ok(Config {
        portraits: PathBuf::from(env("CONSOLE_PORTRAITS", &runs.join("portraits").to_string_lossy())),
        personas: data.join("personas"),
        templates: data.join("templates"),
        runs,
        include: Regex::new(&format!("(?i){}", env("CONSOLE_INCLUDE", "^ludius-"))).map_err(|e| e.to_string())?,
        allowed_origins: parse_origins(&env("CONSOLE_ALLOWED_ORIGINS", ""))?,
        allowed_hosts: env("CONSOLE_ALLOWED_HOSTS", "127.0.0.1;localhost").split([';', ',']).map(|h| h.trim().to_lowercase()).filter(|h| !h.is_empty()).collect(),
        openrouter_key: std::env::var("OPENROUTER_API_KEY").ok().filter(|k| !k.trim().is_empty()),
        openrouter_key_file: std::env::var("OPENROUTER_KEY_FILE").ok().filter(|f| !f.is_empty()).map(PathBuf::from),
    })
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let cfg = config()?;
    let channel = Channel::from_shared(env("LUDIUS_AGENT", "http://127.0.0.1:7451"))?.connect_lazy();
    let app = App {
        cfg: Arc::new(cfg),
        agent: HostAgentClient::new(channel).max_decoding_message_size(32 << 20),
        feeds: Default::default(),
        image_gate: Default::default(),
        http: reqwest::Client::builder().timeout(Duration::from_secs(180)).build()?,
    };
    let root = env("CONSOLE_ROOT", "/srv/console");
    let api = Router::new()
        .route("/vms", get(vms))
        .route("/vms/{name}/stream", get(stream))
        .route("/calls", get(calls))
        .route("/interactions", get(interactions))
        .route("/admission", get(admission))
        .merge(console::routes())
        .merge(portraits::routes());
    let router = Router::new()
        .nest("/api", api)
        .fallback_service(tower_http::services::ServeDir::new(root))
        .layer(middleware::from_fn_with_state(app.clone(), guard))
        .with_state(app);
    let listen = env("CONSOLE_LISTEN", "127.0.0.1:7452");
    let listener = tokio::net::TcpListener::bind(&listen).await?;
    eprintln!("ludius-console on {listen}");
    axum::serve(listener, router)
        .with_graceful_shutdown(async {
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
    fn origins() {
        let allowed = parse_origins("https://monitor.ludius.se").unwrap();
        assert!(origin_allowed("", "127.0.0.1:7452", &allowed));
        assert!(origin_allowed("https://monitor.ludius.se", "127.0.0.1:7452", &allowed));
        assert!(origin_allowed("http://127.0.0.1:7452", "127.0.0.1:7452", &allowed));
        assert!(!origin_allowed("https://evil.example", "127.0.0.1:7452", &allowed));
        assert!(parse_origins("https://monitor.ludius.se/").is_err());
        assert!(parse_origins("https://user@x.se").is_err());
    }
}
