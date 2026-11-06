//! Mattermost REST API (an adopted chat server, LUDIUS_CHAT_API): the same reads as the psql queries, as a system
//! admin (LUDIUS_CHAT_ADMIN_USER, password in LUDIUS_CHAT_ADMIN_PASSWORD_FILE); posts as the user itself (password:
//! user + the persona suffix, as `ludius setup` made the account). Certificate pinned (LUDIUS_CHAT_CA), the name
//! resolved to LUDIUS_CHAT_ADDR. Every failure is None: unknown, never empty.
use std::collections::BTreeMap;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use super::{Post, TALK};

fn env(k: &str) -> String {
    std::env::var(k).unwrap_or_default()
}

pub fn on() -> bool {
    env("LUDIUS_CHAT_API").starts_with("https://")
}

fn client() -> Option<&'static reqwest::blocking::Client> {
    static C: OnceLock<Option<reqwest::blocking::Client>> = OnceLock::new();
    C.get_or_init(|| {
        let base = reqwest::Url::parse(&env("LUDIUS_CHAT_API")).ok()?;
        let cert = reqwest::Certificate::from_pem(&std::fs::read(env("LUDIUS_CHAT_CA")).ok()?).ok()?;
        // a redirect is never followed: it could point out of the range
        let mut b = reqwest::blocking::Client::builder().redirect(reqwest::redirect::Policy::none()).tls_certs_only([cert]).timeout(Duration::from_secs(30));
        if let Ok(ip) = env("LUDIUS_CHAT_ADDR").parse::<std::net::IpAddr>() {
            b = b.resolve(base.host_str()?, std::net::SocketAddr::new(ip, base.port_or_known_default()?));
        }
        b.build().ok()
    })
    .as_ref()
}

fn url(path: &str) -> String {
    format!("{}/api/v4/{path}", env("LUDIUS_CHAT_API").trim_end_matches('/'))
}

/// A session token for `user` (POST users/login).
fn login(user: &str, password: &str) -> Option<String> {
    let r = client()?.post(url("users/login")).json(&json!({"login_id": user, "password": password})).send().ok()?;
    if !r.status().is_success() {
        return None;
    }
    r.headers().get("Token")?.to_str().ok().map(str::to_string)
}

fn read_secret(path: &str) -> Option<String> {
    std::fs::read_to_string(path).ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty())
}

/// The account's password as `ludius setup` made it: user + the persona suffix.
fn user_password(user: &str) -> Option<String> {
    Some(format!("{user}{}", read_secret(&env("LUDIUS_PERSONA_PASSWORD_SUFFIX_FILE"))?))
}

static ADMIN: Mutex<Option<String>> = Mutex::new(None);

fn admin_token(fresh: bool) -> Option<String> {
    let mut t = ADMIN.lock().ok()?;
    if fresh || t.is_none() {
        *t = login(&env("LUDIUS_CHAT_ADMIN_USER"), &read_secret(&env("LUDIUS_CHAT_ADMIN_PASSWORD_FILE"))?);
    }
    t.clone()
}

/// One call as the admin (logged in again once when the session is gone); the body for a 2xx answer.
fn admin(method: reqwest::Method, path: &str, body: Option<&Value>) -> Option<reqwest::blocking::Response> {
    for fresh in [false, true] {
        let mut req = client()?.request(method.clone(), url(path)).bearer_auth(admin_token(fresh)?);
        if let Some(b) = body {
            req = req.json(b);
        }
        let r = req.send().ok()?;
        match r.status().as_u16() {
            401 if !fresh => continue,
            s if (200..300).contains(&s) => return Some(r),
            _ => return None,
        }
    }
    None
}

fn get(path: &str) -> Option<Value> {
    serde_json::from_str(&admin(reqwest::Method::GET, path, None)?.text().ok()?).ok()
}

fn id_ok(s: &str) -> bool {
    s.len() == 26 && s.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
}

fn s(v: &Value, k: &str) -> Option<String> {
    v[k].as_str().map(str::to_string)
}

/// The user's id; None for a user the server does not have (or deactivated): a missing account is not an empty inbox.
fn uid(user: &str) -> Option<String> {
    if user.is_empty() || !user.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"._-".contains(&b)) {
        return None;
    }
    let u = get(&format!("users/username/{user}"))?;
    (u["delete_at"].as_i64()? == 0).then(|| s(&u, "id")).flatten().filter(|i| id_ok(i))
}

fn team() -> Option<String> {
    static T: OnceLock<Option<String>> = OnceLock::new();
    T.get_or_init(|| s(&get(&format!("teams/name/{}", env("LUDIUS_CHAT_TEAM")))?, "id").filter(|i| id_ok(i))).clone()
}

/// The user's channels in the team (direct messages included): (id, type, display name).
fn channels(uid: &str) -> Option<Vec<(String, String, String)>> {
    get(&format!("users/{uid}/teams/{}/channels", team()?))?
        .as_array()?
        .iter()
        .filter(|c| c["delete_at"].as_i64() == Some(0))
        .map(|c| Some((s(c, "id")?, s(c, "type")?, s(c, "display_name").unwrap_or_default())))
        .collect()
}

/// When the user last viewed each of their team channels.
fn viewed(uid: &str) -> Option<BTreeMap<String, i64>> {
    get(&format!("users/{uid}/teams/{}/channels/members", team()?))?.as_array()?.iter().map(|m| Some((s(m, "channel_id")?, m["last_viewed_at"].as_i64()?))).collect()
}

/// The direct channel between two users, named by their sorted ids: Some(None) if they never talked, None when a
/// lookup failed (unknown, not absent).
fn direct(chans: &[(String, String, String)], a: &str, b: &str) -> Option<Option<String>> {
    let mut ids = [a, b];
    ids.sort();
    let name = format!("{}__{}", ids[0], ids[1]);
    // the API lists D channels' name only as `name`; fetch it per candidate
    for c in chans.iter().filter(|c| c.1 == "D") {
        if s(&get(&format!("channels/{}", c.0))?, "name")? == name {
            return Some(Some(c.0.clone()));
        }
    }
    Some(None)
}

struct Raw {
    id: String,
    user: String,
    at: i64,
    message: String,
    files: Vec<String>,
}

/// The channel's live user posts (no system posts): since `since` (modified since, so then by create time), else the newest 200.
fn raw_posts(cid: &str, since: Option<i64>) -> Option<Vec<Raw>> {
    let q = since.map_or("page=0&per_page=200".to_string(), |s| format!("since={s}"));
    let v = get(&format!("channels/{cid}/posts?{q}"))?;
    let mut out = v["posts"]
        .as_object()?
        .values()
        .filter(|p| p["delete_at"].as_i64() == Some(0) && p["type"].as_str() == Some(""))
        .map(|p| {
            Some(Raw {
                id: s(p, "id")?,
                user: s(p, "user_id")?,
                at: p["create_at"].as_i64()?,
                message: s(p, "message")?,
                files: p["file_ids"].as_array().map(|a| a.iter().filter_map(|f| f.as_str().map(str::to_string)).collect()).unwrap_or_default(),
            })
        })
        .collect::<Option<Vec<_>>>()?;
    if let Some(s) = since {
        out.retain(|p| p.at >= s);
    }
    out.sort_by_key(|p| p.at);
    Some(out)
}

fn usernames(ids: &[&str]) -> Option<BTreeMap<String, String>> {
    if ids.is_empty() {
        return Some(BTreeMap::new());
    }
    let r = admin(reqwest::Method::POST, "users/ids", Some(&json!(ids)))?;
    serde_json::from_str::<Value>(&r.text().ok()?).ok()?.as_array()?.iter().map(|u| Some((s(u, "id")?, s(u, "username")?))).collect()
}

fn to_posts(raw: Vec<Raw>, channel: &str) -> Option<Vec<Post>> {
    let ids: Vec<&str> = raw.iter().map(|p| p.user.as_str()).collect();
    let names = usernames(&ids)?;
    raw.into_iter().map(|p| Some(Post { sender: names.get(&p.user)?.clone(), id: p.id, message: p.message, at: p.at, channel: channel.into() })).collect()
}

pub fn unread(user: &str) -> Option<Vec<Post>> {
    let me = uid(user)?;
    let seen = viewed(&me)?;
    let mut out = vec![];
    for (cid, kind, name) in channels(&me)?.into_iter().filter(|c| c.1 == "D" || c.1 == "P") {
        let since = *seen.get(&cid)?;
        let raw: Vec<Raw> = raw_posts(&cid, Some(since))?.into_iter().filter(|p| p.at > since && p.user != me).collect();
        out.extend(to_posts(raw, if kind == "P" { &name } else { "" })?);
    }
    out.sort_by_key(|p| p.at);
    Some(out)
}

pub fn conversation(user: &str, other: &str) -> Option<(Vec<Post>, i64)> {
    let me = uid(user)?;
    let chans = channels(&me)?;
    let cid = match other.strip_prefix('~') {
        Some(c) => chans.iter().find(|x| x.1 == "P" && x.2 == c)?.0.clone(),
        None => direct(&chans, &me, &uid(other)?)??,
    };
    let seen = *viewed(&me)?.get(&cid)?;
    let mut raw: Vec<Raw> = raw_posts(&cid, None)?.into_iter().filter(|p| p.at <= seen).collect();
    raw.drain(..raw.len().saturating_sub(TALK));
    Some((to_posts(raw, "")?, seen))
}

/// Only posted files: the API lists no file staged in a draft, so a staged check is unknown.
pub fn shared(user: &str, other: &str, name: &str, since_ms: i64, posted: bool) -> Option<Vec<String>> {
    if !posted {
        return None;
    }
    let me = uid(user)?;
    let Some(cid) = direct(&channels(&me)?, &me, &uid(other)?)? else { return Some(vec![]) };
    let mut sums = vec![];
    for f in raw_posts(&cid, Some(since_ms))?.into_iter().filter(|p| p.user == me).flat_map(|p| p.files) {
        let info = get(&format!("files/{f}/info"))?;
        if info["delete_at"].as_i64() == Some(0) && info["create_at"].as_i64()? >= since_ms && s(&info, "name")?.to_lowercase() == name.to_lowercase() {
            let bytes = admin(reqwest::Method::GET, &format!("files/{f}"), None)?.bytes().ok()?;
            sums.push(format!("{:x}", Sha256::digest(&bytes)));
        }
    }
    Some(sums)
}

pub fn posted(user: &str, r#where: &str, message: &str, since_ms: i64) -> Option<i64> {
    let me = uid(user)?;
    let chans = channels(&me)?;
    let cid = match r#where.strip_prefix('~') {
        Some(c) => chans.iter().find(|x| (x.1 == "O" || x.1 == "P") && x.2 == c).map(|x| x.0.clone()),
        None => direct(&chans, &me, &uid(r#where)?)?,
    };
    let Some(cid) = cid else { return Some(0) };
    Some(raw_posts(&cid, Some(since_ms))?.iter().filter(|p| p.user == me && p.message.trim() == message.trim()).count() as i64)
}

/// Post as `user` (its own session) in the direct chat with `other`; Some(true) when the server answered with the post.
pub fn post(user: &str, other: &str, message: &str) -> Option<bool> {
    let token = login(user, &user_password(user)?)?;
    let (me, them) = (uid(user)?, uid(other)?);
    let c = client()?;
    let r = c.post(url("channels/direct")).bearer_auth(&token).json(&json!([me, them])).send().ok()?;
    let cid = s(&serde_json::from_str::<Value>(&r.error_for_status().ok()?.text().ok()?).ok()?, "id")?;
    let r = c.post(url("posts")).bearer_auth(&token).json(&json!({"channel_id": cid, "message": message.trim()})).send().ok()?;
    Some(r.status().is_success())
}

/// Deletes the direct chat's posts between two noise accounts; an account without Ludius's mark (the operator's) is
/// refused (None).
pub fn reset(user: &str, other: &str) -> Option<usize> {
    let noise = |u: &str| get(&format!("users/username/{u}")).is_some_and(|v| v["props"]["ludius"].as_str() == Some("noise"));
    if !noise(user) || !noise(other) {
        return None;
    }
    let me = uid(user)?;
    let Some(cid) = direct(&channels(&me)?, &me, &uid(other)?)? else { return Some(0) };
    let mut n = 0;
    loop {
        let ids: Vec<String> = raw_posts(&cid, None)?.into_iter().map(|p| p.id).collect();
        if ids.is_empty() {
            return Some(n);
        }
        for id in ids {
            admin(reqwest::Method::DELETE, &format!("posts/{id}"), None)?;
            n += 1;
        }
    }
}
