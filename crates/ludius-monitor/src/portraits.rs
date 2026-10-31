//! Persona portraits and the company logo: generated on request through OpenRouter, stored under
//! runs/portraits. Generation needs an allowed Origin and a JSON request, one image at a time.
use crate::console::{USER, catalog};
use crate::{App, origin_ok, problem};
use axum::Router;
use axum::body::Body;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use base64::Engine;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::UNIX_EPOCH;

const MODEL: &str = "qwen/qwen-image-2.1-turbo";
const LOGO_MODEL: &str = "openai/gpt-image-1-mini";

pub fn routes() -> Router<App> {
    Router::new()
        .route("/portraits", get(list))
        .route("/portraits/{user}", get(portrait).post(generate_portrait))
        .route("/logo", get(logo).post(generate_logo))
}

fn key(app: &App) -> Option<String> {
    if let Some(k) = &app.cfg.openrouter_key {
        return Some(k.clone());
    }
    // The key alone, or an env file with an OPENROUTER_API_KEY= line.
    let text = std::fs::read_to_string(app.cfg.openrouter_key_file.as_ref()?).ok()?;
    let key = match text.lines().find_map(|l| l.strip_prefix("OPENROUTER_API_KEY=")) {
        Some(k) => k,
        None if !text.contains('=') => text.trim(),
        None => return None,
    };
    Some(key.trim().trim_matches(['"', '\'']).to_string()).filter(|k| !k.is_empty())
}

fn file_for(app: &App, user: &str) -> PathBuf {
    app.cfg.portraits.join(format!("{user}.image"))
}

fn logo_file(app: &App) -> PathBuf {
    app.cfg.portraits.join("company.logo")
}

fn version(p: &std::path::Path) -> u128 {
    std::fs::metadata(p).and_then(|m| m.modified()).ok().and_then(|t| t.duration_since(UNIX_EPOCH).ok()).map_or(0, |d| d.as_nanos() / 100)
}

pub fn mime(b: &[u8]) -> &'static str {
    if b.starts_with(&[137, 80, 78, 71, 13, 10, 26, 10]) {
        "image/png"
    } else if b.starts_with(&[255, 216, 255]) {
        "image/jpeg"
    } else if b.len() >= 12 && &b[0..4] == b"RIFF" && &b[8..12] == b"WEBP" {
        "image/webp"
    } else {
        "application/octet-stream"
    }
}

async fn list(State(app): State<App>) -> Response {
    let images: BTreeMap<String, String> = std::fs::read_dir(&app.cfg.portraits)
        .map(|d| {
            d.filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| p.extension().is_some_and(|x| x == "image"))
                .filter_map(|p| {
                    let user = p.file_stem()?.to_string_lossy().into_owned();
                    USER.is_match(&user).then(|| (user.clone(), format!("/api/portraits/{user}?v={}", version(&p))))
                })
                .collect()
        })
        .unwrap_or_default();
    let logo = logo_file(&app);
    let logo = logo.is_file().then(|| format!("/api/logo?v={}", version(&logo)));
    axum::Json(json!({ "enabled": key(&app).is_some(), "images": images, "logo": logo })).into_response()
}

fn image(path: PathBuf, png_only: bool) -> Response {
    match std::fs::read(path) {
        Ok(b) => {
            let m = if png_only { "image/png" } else { mime(&b) };
            ([(header::CONTENT_TYPE, m)], Body::from(b)).into_response()
        }
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn portrait(State(app): State<App>, Path(user): Path<String>) -> Response {
    if !USER.is_match(&user) {
        return StatusCode::NOT_FOUND.into_response();
    }
    image(file_for(&app, &user), false)
}

async fn logo(State(app): State<App>) -> Response {
    image(logo_file(&app), true)
}

fn refused(app: &App, h: &HeaderMap) -> Option<Response> {
    if h.get(header::ORIGIN).is_none() || !origin_ok(app, h) {
        return Some(StatusCode::FORBIDDEN.into_response());
    }
    let ct = h.get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).unwrap_or("");
    let essence = ct.split(';').next().unwrap_or("").trim().to_lowercase();
    (essence != "application/json" && !essence.ends_with("+json")).then(|| StatusCode::UNSUPPORTED_MEDIA_TYPE.into_response())
}

enum Fail {
    Generate(String),
    Storage,
}

async fn guarded(app: &App, what: &str, work: impl Future<Output = Result<Response, Fail>>) -> Response {
    let Ok(_gate) = app.image_gate.try_lock() else {
        return (StatusCode::CONFLICT, axum::Json(json!({ "error": "Another image is being generated. Try again when it finishes." }))).into_response();
    };
    match work.await {
        Ok(r) => r,
        Err(Fail::Generate(detail)) if !detail.is_empty() => problem(StatusCode::BAD_GATEWAY, &detail),
        Err(Fail::Generate(_)) => problem(StatusCode::BAD_GATEWAY, &format!("{what} generation failed. The existing image was kept; try again.")),
        Err(Fail::Storage) => problem(StatusCode::SERVICE_UNAVAILABLE, &format!("{what} storage or persona data is unavailable.")),
    }
}

fn saved() -> Result<Response, Fail> {
    Ok(axum::Json(json!({ "saved": true })).into_response())
}

async fn generate(app: &App, target: PathBuf, payload: Value, png_only: bool) -> Result<Response, Fail> {
    let Some(key) = key(app) else {
        return Ok(problem(StatusCode::SERVICE_UNAVAILABLE, "OpenRouter image generation is not configured."));
    };
    let res = app.http.post("https://openrouter.ai/api/v1/images").bearer_auth(key).json(&payload).send().await.map_err(|_| Fail::Generate(String::new()))?;
    if !res.status().is_success() {
        return Err(Fail::Generate(format!("OpenRouter image generation failed (HTTP {}).", res.status().as_u16())));
    }
    let body: Value = res.json().await.map_err(|_| Fail::Generate(String::new()))?;
    let Some(encoded) = body["data"][0]["b64_json"].as_str() else {
        return Err(Fail::Generate("OpenRouter returned no image. The existing image was kept.".into()));
    };
    let img = base64::engine::general_purpose::STANDARD.decode(encoded).map_err(|_| Fail::Generate(String::new()))?;
    let m = mime(&img);
    if img.len() > 16 << 20 || m == "application/octet-stream" || (png_only && m != "image/png") {
        return Err(Fail::Generate("OpenRouter returned an unsupported image.".into()));
    }
    std::fs::create_dir_all(&app.cfg.portraits).map_err(|_| Fail::Storage)?;
    let tmp = target.with_extension(format!("{}.tmp", target.extension().unwrap_or_default().to_string_lossy()));
    std::fs::write(&tmp, &img).map_err(|_| Fail::Storage)?;
    std::fs::rename(&tmp, &target).map_err(|_| Fail::Storage)?;
    saved()
}

#[derive(serde::Deserialize)]
struct Replace {
    replace: Option<String>,
}

async fn generate_portrait(State(app): State<App>, Path(user): Path<String>, Query(q): Query<Replace>, h: HeaderMap) -> Response {
    if let Some(r) = refused(&app, &h) {
        return r;
    }
    if !USER.is_match(&user) {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let work = async {
        let catalog = catalog(&app.cfg.runs, &app.cfg.personas).map_err(|_| Fail::Storage)?;
        let mine = |list: &Value| list.as_array().and_then(|l| l.iter().find(|p| p["user"] == user.as_str()).cloned());
        let Some(person) = mine(&catalog["people"]).or_else(|| mine(&catalog["company"]["people"])) else {
            return Ok(StatusCode::NOT_FOUND.into_response());
        };
        let target = file_for(&app, &user);
        if target.is_file() && q.replace.as_deref() != Some("true") {
            return saved();
        }
        let s = |v: &Value| v.as_str().unwrap_or("").to_string();
        let prompt = format!(
            "Create a realistic staff-directory portrait of one fictional adult employee: {}, {}, {}, at {} in {}. \
             Head and shoulders, looking at the camera, natural expression, ordinary work clothing appropriate to the role, soft daylight, plain muted grey office background. \
             Square crop, face clearly visible. Candid corporate directory photography, realistic skin texture. No text, lettering, logo, watermark or collage. \
             This is an invented character, not a real person.",
            s(&person["name"]),
            s(&person["role"]),
            s(&person["department"]),
            s(&catalog["company"]["company"]),
            s(&catalog["company"]["city"]),
        );
        generate(&app, target, json!({ "model": MODEL, "prompt": prompt, "n": 1, "aspect_ratio": "1:1", "resolution": "1K" }), false).await
    };
    guarded(&app, "Portrait", work).await
}

async fn generate_logo(State(app): State<App>, Query(q): Query<Replace>, h: HeaderMap) -> Response {
    if let Some(r) = refused(&app, &h) {
        return r;
    }
    let work = async {
        let catalog = catalog(&app.cfg.runs, &app.cfg.personas).map_err(|_| Fail::Storage)?;
        let company = &catalog["company"];
        let Some(name) = company["company"].as_str() else { return Ok(StatusCode::NOT_FOUND.into_response()) };
        let target = logo_file(&app);
        if target.is_file() && q.replace.as_deref() != Some("true") {
            return saved();
        }
        let summary: String = company["summary"].as_str().unwrap_or("").chars().take(400).collect();
        let prompt = format!(
            "Design a logo emblem for {name}, a fictional company in {}. About the company: {summary} \
             A single simple, memorable symbol that hints at what the company makes. Flat vector style, two or three solid colours of medium brightness so it reads on both dark and light backgrounds, clean geometric shapes, centred with generous margin. \
             Transparent background: no backdrop, frame, badge, shadow or mockup. No text, letters or numbers.",
            company["city"].as_str().unwrap_or(""),
        );
        let payload = json!({ "model": LOGO_MODEL, "prompt": prompt, "n": 1, "aspect_ratio": "1:1", "background": "transparent", "quality": "medium" });
        generate(&app, target, payload, true).await
    };
    guarded(&app, "Logo", work).await
}
