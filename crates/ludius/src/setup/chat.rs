//! addons.chat "adopt": a range's own Mattermost, over its REST API as the operator's system admin (password in
//! secrets). The gate only reads. `adopt` makes the narrative people's accounts (marked props.ludius = "noise"; a
//! same-name account without the mark is refused), adds them to the team and its public channels, and makes the
//! narrative's team channels (private, marked in their purpose). Server settings, other accounts and every channel
//! without the mark stay the operator's.
use std::net::{IpAddr, SocketAddr};
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use reqwest::Method;
use reqwest::blocking::Client;
use serde_json::{Value, json};

pub const USER_MARK: &str = "noise";
pub const CHANNEL_MARK: &str = "Ludius noise channel";

pub struct Server {
    base: String,
    client: Client,
    token: String,
}

/// The same client as the controller's (ludius-chat api.rs): only `ca` is trusted, the name resolves to `ip`.
pub fn connect(url: &str, ip: IpAddr, ca: &[u8]) -> Result<Server> {
    let u = reqwest::Url::parse(url)?;
    let cert = reqwest::Certificate::from_pem(ca)?;
    let client = Client::builder()
        .tls_certs_only([cert])
        .timeout(Duration::from_secs(30))
        .resolve(u.host_str().context("no host")?, SocketAddr::new(ip, u.port_or_known_default().unwrap_or(443)))
        .build()?;
    let s = Server { base: url.trim_end_matches('/').to_string(), client, token: String::new() };
    let r = s.client.get(s.url("system/ping")).send().map_err(|e| anyhow::anyhow!("{url}: {e:#} (a self-signed server certificate is refused: the chat certificate must be issued by a CA)"))?;
    ensure!(r.status().is_success(), "{url}/api/v4/system/ping: HTTP {}", r.status());
    Ok(s)
}

impl Server {
    fn url(&self, path: &str) -> String {
        format!("{}/api/v4/{path}", self.base)
    }

    pub fn login(&mut self, user: &str, password: &str) -> Result<()> {
        let r = self.client.post(self.url("users/login")).json(&json!({"login_id": user, "password": password})).send()?;
        ensure!(r.status().is_success(), "sign-in as {user}: HTTP {}", r.status());
        self.token = r.headers().get("Token").and_then(|t| t.to_str().ok()).context("sign-in: no token")?.to_string();
        let me = self.call(Method::GET, "users/me", None)?.context("users/me")?;
        ensure!(me["roles"].as_str().unwrap_or("").split(' ').any(|r| r == "system_admin"), "{user} is not a system admin");
        Ok(())
    }

    /// Some(body) on success, None on 404, an error with the server's message otherwise.
    fn call(&self, m: Method, path: &str, body: Option<Value>) -> Result<Option<Value>> {
        let mut rq = self.client.request(m.clone(), self.url(path)).bearer_auth(&self.token);
        if let Some(b) = body {
            rq = rq.json(&b);
        }
        let r = rq.send().with_context(|| format!("{m} {path}"))?;
        let st = r.status();
        let v: Value = r.json().unwrap_or(Value::Null);
        if st == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        ensure!(st.is_success(), "{m} {path}: HTTP {st} {}", v["message"].as_str().unwrap_or(""));
        Ok(Some(v))
    }

    fn must(&self, m: Method, path: &str, body: Option<Value>) -> Result<Value> {
        self.call(m, path, body)?.with_context(|| format!("{path}: not found"))
    }

    /// The team (id, display name) and its public channels (id, display name).
    pub fn team(&self, name: &str) -> Result<(String, String, Vec<(String, String)>)> {
        let t = self.call(Method::GET, &format!("teams/name/{name}"), None)?.with_context(|| format!("no team {name:?} on the chat server"))?;
        let id = t["id"].as_str().context("team id")?.to_string();
        let mut chans = vec![];
        for page in 0.. {
            let v = self.must(Method::GET, &format!("teams/{id}/channels?page={page}&per_page=200"), None)?;
            let a = v.as_array().context("channels")?;
            chans.extend(a.iter().filter(|c| c["type"] == "O" && c["delete_at"] == 0).filter_map(|c| Some((c["id"].as_str()?.to_string(), c["display_name"].as_str()?.to_string()))));
            if a.len() < 200 {
                break;
            }
        }
        Ok((id, t["display_name"].as_str().unwrap_or(name).to_string(), chans))
    }

    /// An account Ludius made (marked) or a new one; its password set to `password`.
    fn account(&self, user: &str, name: &str, email: &str, password: &str) -> Result<String> {
        match self.call(Method::GET, &format!("users/username/{user}"), None)? {
            Some(u) => {
                ensure!(u["props"]["ludius"] == USER_MARK, "chat account {user} exists and is not Ludius's (no props.ludius mark): refusing to take it over");
                ensure!(u["delete_at"] == 0, "chat account {user} is deactivated");
                let id = u["id"].as_str().context("user id")?.to_string();
                self.must(Method::PUT, &format!("users/{id}/password"), Some(json!({"new_password": password})))?;
                Ok(id)
            }
            None => {
                let (first, last) = name.split_once(' ').unwrap_or((name, ""));
                let u = self.must(
                    Method::POST,
                    "users",
                    Some(json!({"username": user, "email": email, "password": password, "first_name": first, "last_name": last})),
                )?;
                let id = u["id"].as_str().context("user id")?.to_string();
                let p = self.must(Method::PUT, &format!("users/{id}/patch"), Some(json!({"props": {"ludius": USER_MARK}})))?;
                ensure!(p["props"]["ludius"] == USER_MARK, "chat account {user}: the server dropped the Ludius mark");
                Ok(id)
            }
        }
    }

    fn member(&self, team: &str, channel: Option<&str>, uid: &str) -> Result<()> {
        match channel {
            None => self.must(Method::POST, &format!("teams/{team}/members"), Some(json!({"team_id": team, "user_id": uid}))),
            Some(c) => self.must(Method::POST, &format!("channels/{c}/members"), Some(json!({"user_id": uid}))),
        }
        .map(|_| ())
    }

    /// A narrative team channel: Ludius's own (marked) or new and private; an unmarked one of that handle is refused.
    fn team_channel(&self, team: &str, name: &str) -> Result<String> {
        let handle = name.to_lowercase().replace(' ', "-");
        let c = match self.call(Method::GET, &format!("teams/{team}/channels/name/{handle}?include_deleted=true"), None)? {
            Some(c) => {
                ensure!(c["purpose"] == CHANNEL_MARK && c["delete_at"] == 0, "chat channel {handle} exists and is not Ludius's (or is archived): refusing to use it");
                c
            }
            None => self.must(Method::POST, "channels", Some(json!({"team_id": team, "name": handle, "display_name": name, "type": "P", "purpose": CHANNEL_MARK})))?,
        };
        Ok(c["id"].as_str().context("channel id")?.to_string())
    }
}

/// Accounts, team and public channel memberships, and the narrative's team channels with their members.
pub fn adopt(s: &Server, team: &str, domain: &str, people: &[(String, String)], suffix: &str, team_channels: &indexmap::IndexMap<String, Vec<String>>) -> Result<()> {
    ensure!(suffix.len() >= 6, "persona password suffix too short");
    let (tid, _, open) = s.team(team)?;
    let mut ids = std::collections::BTreeMap::new();
    for (user, name) in people {
        if !regex::Regex::new(r"^[a-z][a-z0-9._-]{0,19}$").expect("static").is_match(user) {
            bail!("narrative account {user:?}: not a plain account name");
        }
        let uid = s.account(user, name, &format!("{user}@{domain}"), &format!("{user}{suffix}"))?;
        s.member(&tid, None, &uid)?;
        for (c, _) in &open {
            s.member(&tid, Some(c), &uid)?;
        }
        println!("   chat account {user}: team {team}, {} public channels", open.len());
        ids.insert(user.clone(), uid);
    }
    for (name, members) in team_channels {
        let cid = s.team_channel(&tid, name)?;
        for m in members {
            s.member(&tid, Some(&cid), ids.get(m).with_context(|| format!("team channel {name}: {m} is no narrative account"))?)?;
        }
        println!("   chat team channel {name}: {} members", members.len());
    }
    Ok(())
}
