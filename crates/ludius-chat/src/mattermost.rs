//! Mattermost backend: read-only psql queries and mmctl over SSH (port of react.py's queries).
//! Every failure (no host, SSH, psql, odd output) is None: unknown, never empty.
use std::io::{Read as _, Write as _};
use std::process::{Command, Stdio};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use regex::Regex;
use serde_json::Value;

use crate::Post;

/// Posts the conversation view shows before the unread ones.
pub const TALK: usize = 12;

const UNREAD_SQL: &str = "select coalesce(json_agg(row_to_json(t)), '[]') from (
  select p.id, s.username as sender, p.createat, p.message,
    case when c.type = 'P' then c.displayname else '' end as channel
  from channels c join channelmembers m on m.channelid = c.id join users u on u.id = m.userid
  join posts p on p.channelid = c.id join users s on s.id = p.userid
  where c.type in ('D', 'P') and u.username = :'u' and p.userid <> u.id and p.deleteat = 0 and length(p.type) = 0
    and p.createat > m.lastviewedat
  order by p.createat) t;
";

fn talk_sql(tail: &str) -> String {
    format!(
        "select json_build_object('viewed', m.lastviewedat, 'posts', coalesce((
  select json_agg(row_to_json(t) order by t.createat) from (
    select p.id, s.username as sender, p.createat, p.message from posts p join users s on s.id = p.userid
    where p.channelid = c.id and p.deleteat = 0 and length(p.type) = 0 and p.createat <= m.lastviewedat
    order by p.createat desc limit {TALK}) t),
  '[]'))
from channels c join channelmembers m on m.channelid = c.id join users u on u.id = m.userid
{tail}"
    )
}

const DM_TAIL: &str = "  join channelmembers m2 on m2.channelid = c.id join users o on o.id = m2.userid
where c.type = 'D' and u.username = :'u' and o.username = :'o';
";
const CHANNEL_TAIL: &str = "where c.type = 'P' and c.displayname = :'c' and c.deleteat = 0 and u.username = :'u';\n";

const PAIR_SQL: &str = "select coalesce(json_agg(p.id), '[]') from channels c
  join channelmembers m on m.channelid = c.id join users u on u.id = m.userid
  join channelmembers m2 on m2.channelid = c.id join users o on o.id = m2.userid
  join posts p on p.channelid = c.id
where c.type = 'D' and u.username = :'u' and o.username = :'o' and u.id <> o.id and p.deleteat = 0;
";

const SHARED_SQL: &str = "select coalesce(json_agg(f.path), '[]') from fileinfo f
  join channels c on c.id = f.channelid join users u on u.id = f.creatorid
  join channelmembers m on m.channelid = c.id join users o on o.id = m.userid left join posts p on p.id = f.postid
where c.type = 'D' and u.username = :'u' and o.username = :'o' and u.id <> o.id and f.deleteat = 0
  and f.createat >= :s and lower(f.name) = lower(:'n')
  and case when :'state' = 'posted' then p.deleteat = 0 and p.userid = u.id and p.channelid = c.id
      else coalesce(f.postid, '') = '' end;
";

// Python hashed the stripped message locally; md5 runs in psql here (no md5 crate), on the same stripped text.
const POSTED_SQL: &str = "select count(*) from posts p join channels c on c.id = p.channelid join users u on u.id = p.userid
where u.username = :'u' and p.deleteat = 0 and p.createat >= :s and md5(btrim(p.message)) = md5(:'m')
  and case when :'k' = 'D' then c.type = 'D' and exists (select 1 from channelmembers m join users o on o.id = m.userid
      where m.channelid = c.id and o.username = :'c' and o.id <> u.id)
    else c.type in ('O', 'P') and c.displayname = :'c' and c.deleteat = 0 end;
";

fn re(cell: &'static OnceLock<Regex>, pattern: &str) -> &'static Regex {
    cell.get_or_init(|| Regex::new(pattern).expect("static regex"))
}

fn user_ok(v: &str) -> bool {
    static R: OnceLock<Regex> = OnceLock::new();
    re(&R, r"^(?:[a-z0-9._-]+|[A-Za-z0-9][A-Za-z0-9 ._-]{0,40})$").is_match(v)
}

fn quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// Run `remote` on the chat host; stdout when it exits 0 within `secs`.
fn ssh(remote: &str, stdin: &str, secs: u64) -> Option<Vec<u8>> {
    let host = std::env::var("LUDIUS_CHAT_HOST").ok().filter(|h| !h.is_empty())?;
    let env = |k: &str, d: &str| std::env::var(k).unwrap_or_else(|_| d.into());
    let known = format!("UserKnownHostsFile={}", env("LUDIUS_CHAT_KNOWN_HOSTS", "/root/.ssh/ludius_known_hosts"));
    let mut child = Command::new("ssh")
        .args(["-i", &env("LUDIUS_CHAT_KEY", "/root/.ssh/ludius_chat01"), "-o", &known, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"])
        .arg(format!("ludius@{host}"))
        .arg(remote)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let wrote = child.stdin.take().map(|mut i| i.write_all(stdin.as_bytes()));
    let mut out = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut b = Vec::new();
        out.read_to_end(&mut b).map(|_| b)
    });
    let deadline = Instant::now() + Duration::from_secs(secs);
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) if Instant::now() < deadline => std::thread::sleep(Duration::from_millis(50)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let stdout = reader.join().ok()?.ok()?;
    (status.success() && matches!(wrote, Some(Ok(())))).then_some(stdout)
}

/// One psql query with `-v` variables; every variable but the message `m` must look like a user or name.
fn query(sql: &str, vars: &[(&str, &str)]) -> Option<Value> {
    if !vars.iter().all(|(k, v)| *k == "m" || user_ok(v)) {
        return None;
    }
    let v: Vec<String> = vars.iter().map(|(k, v)| format!("-v {}", quote(&format!("{k}={v}")))).collect();
    let out = ssh(&format!("sudo -u postgres psql -d mattermost -tA -v ON_ERROR_STOP=1 {}", v.join(" ")), sql, 30)?;
    let s = String::from_utf8(out).ok()?;
    serde_json::from_str(if s.trim().is_empty() { "null" } else { &s }).ok()
}

fn posts(rows: &Value) -> Option<Vec<Post>> {
    rows.as_array()?
        .iter()
        .map(|r| {
            Some(Post {
                id: r["id"].as_str()?.into(),
                sender: r["sender"].as_str()?.into(),
                message: r["message"].as_str()?.into(),
                at: r["createat"].as_i64()?,
                channel: r.get("channel").and_then(Value::as_str).unwrap_or_default().into(),
            })
        })
        .collect()
}

/// Unread posts to `user` in direct messages and private team channels, oldest first.
pub fn unread(user: &str) -> Option<Vec<Post>> {
    posts(&query(UNREAD_SQL, &[("u", user)])?)
}

/// The last TALK read posts with `other` (a user, or ~channel) and when `user` last viewed it.
pub fn conversation(user: &str, other: &str) -> Option<(Vec<Post>, i64)> {
    let row = match other.strip_prefix('~') {
        Some(c) => query(&talk_sql(CHANNEL_TAIL), &[("u", user), ("c", c)])?,
        None => query(&talk_sql(DM_TAIL), &[("u", user), ("o", other)])?,
    };
    Some((posts(&row["posts"])?, row["viewed"].as_i64()?))
}

/// sha256 of each file `user` shared with `other` named `name` since `since_ms` (posted, or only staged).
pub fn shared(user: &str, other: &str, name: &str, since_ms: i64, posted: bool) -> Option<Vec<String>> {
    static PATH: OnceLock<Regex> = OnceLock::new();
    static SHA: OnceLock<Regex> = OnceLock::new();
    let state = if posted { "posted" } else { "staged" };
    let since = since_ms.to_string();
    let paths = query(SHARED_SQL, &[("u", user), ("o", other), ("n", name), ("s", &since), ("state", state)])?;
    let raw = paths.as_array()?;
    let ok: Vec<&str> = raw
        .iter()
        .filter_map(Value::as_str)
        .filter(|p| re(&PATH, r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_][A-Za-z0-9 ._-]*)+$").is_match(p) && !p.contains(".."))
        .collect();
    if ok.len() != raw.len() {
        return None;
    }
    if ok.is_empty() {
        return Some(vec![]);
    }
    let files: Vec<String> = ok.iter().map(|x| quote(&format!("/opt/mattermost/data/{x}"))).collect();
    let out = String::from_utf8_lossy(&ssh(&format!("sudo sha256sum -- {}", files.join(" ")), "", 30)?).into_owned();
    let sums: Vec<String> = out.lines().map(|l| l.split(' ').next().unwrap_or_default().to_string()).collect();
    (sums.len() == ok.len() && sums.iter().all(|s| re(&SHA, "^[0-9a-f]{64}$").is_match(s))).then_some(sums)
}

/// Posts by `user` in `where` (a user, or ~channel) with exactly `message` since `since_ms`.
pub fn posted(user: &str, r#where: &str, message: &str, since_ms: i64) -> Option<i64> {
    let (k, c) = match r#where.strip_prefix('~') {
        Some(c) => ("C", c),
        None => ("D", r#where),
    };
    let since = since_ms.to_string();
    query(POSTED_SQL, &[("u", user), ("c", c), ("s", &since), ("k", k), ("m", message.trim())])?.as_i64()
}

/// Soft-delete every post in the direct chat between `user` and `other` (mmctl); how many.
pub fn reset(user: &str, other: &str) -> Option<usize> {
    static ID: OnceLock<Regex> = OnceLock::new();
    let result = query(PAIR_SQL, &[("u", user), ("o", other)])?;
    let raw = result.as_array()?;
    let ids: Vec<&str> = raw.iter().filter_map(Value::as_str).filter(|i| re(&ID, "^[a-z0-9]{26}$").is_match(i)).collect();
    if ids.len() != raw.len() {
        return None;
    }
    if !ids.is_empty() {
        ssh(&format!("sudo -u mattermost /opt/mattermost/bin/mmctl --local post delete {}", ids.join(" ")), "", 120)?;
    }
    Some(ids.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rows_and_guards() {
        let rows = serde_json::json!([{"id": "a", "sender": "bek", "createat": 5, "message": "hi", "channel": null},
                                      {"id": "b", "sender": "lnord", "createat": 6, "message": "yo", "channel": "Ops"}]);
        let p = posts(&rows).unwrap();
        assert_eq!((p[0].key(), p[1].key()), ("bek".to_string(), "~Ops".to_string()));
        assert!(posts(&serde_json::json!([{"id": "a"}])).is_none(), "a malformed row is unknown, never skipped");
        assert!(user_ok("j.doe") && user_ok("Team Ops") && !user_ok("x';drop") && !user_ok(""));
        assert_eq!(quote("it's"), r"'it'\''s'");
        // no host configured: unknown
        assert_eq!(unread("bek"), None);
    }
}
