//! Mirror runs/ into PostgreSQL (traces, world and plan docs) and screenshots into an S3 bucket, as Python store.py.
use anyhow::{bail, Context, Result};
use hmac::{Hmac, Mac};
use postgres::{Client, NoTls};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::path::{Path, PathBuf};
use std::time::Duration;

const SCHEMA: &str = "
create table if not exists trace (
    source  text   not null,  -- log path relative to the run directory, forward slashes
    line    int    not null,  -- 1-based line number in that log
    event   text,
    host_ms bigint,
    rec     jsonb  not null,
    primary key (source, line)
);
create index if not exists trace_event on trace (event);
create table if not exists world_doc (
    key     text        primary key,  -- path relative to the run directory, forward slashes
    body    jsonb       not null,
    sha256  text        not null,
    updated timestamptz not null default now()
);
create table if not exists screenshot (
    key     text        primary key,  -- path relative to the run directory, forward slashes
    sha256  text        not null,     -- the object is sha256/<sha256>.png in the bucket
    size    bigint      not null,
    updated timestamptz not null default now()
);
";

#[derive(Debug, Default, PartialEq)]
pub struct Synced {
    pub lines: usize,
    pub docs: u64,
    pub shots: usize,
}

/// An S3 bucket (MinIO) reached with path-style requests signed by AWS SigV4.
pub struct Bucket {
    base: String,
    host: String,
    name: String,
    key: String,
    secret: String,
    http: reqwest::blocking::Client,
}

fn sha(raw: &[u8]) -> String {
    format!("{:x}", Sha256::digest(raw))
}

fn mac(key: &[u8], msg: &str) -> Vec<u8> {
    let mut m = Hmac::<Sha256>::new_from_slice(key).expect("hmac takes any key length");
    m.update(msg.as_bytes());
    m.finalize().into_bytes().to_vec()
}

impl Bucket {
    /// SigV4 `authorization` header over host, x-amz-content-sha256 and x-amz-date (path-style, no query, us-east-1).
    fn auth(&self, method: &reqwest::Method, path: &str, hash: &str, now: chrono::DateTime<chrono::Utc>) -> String {
        let (date, stamp) = (now.format("%Y%m%d").to_string(), now.format("%Y%m%dT%H%M%SZ").to_string());
        let signed = "host;x-amz-content-sha256;x-amz-date";
        let canon = format!("{method}\n{path}\n\nhost:{}\nx-amz-content-sha256:{hash}\nx-amz-date:{stamp}\n\n{signed}\n{hash}", self.host);
        let scope = format!("{date}/us-east-1/s3/aws4_request");
        let k = ["us-east-1", "s3", "aws4_request"].iter().fold(mac(format!("AWS4{}", self.secret).as_bytes(), &date), |k, part| mac(&k, part));
        let sig = sha_hex(&mac(&k, &format!("AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{}", sha(canon.as_bytes()))));
        format!("AWS4-HMAC-SHA256 Credential={}/{scope}, SignedHeaders={signed}, Signature={sig}", self.key)
    }

    fn send(&self, method: reqwest::Method, path: &str, body: Vec<u8>) -> Result<reqwest::blocking::Response> {
        let (now, hash) = (chrono::Utc::now(), sha(&body));
        let auth = self.auth(&method, path, &hash, now);
        let req = self.http.request(method, format!("{}{path}", self.base));
        Ok(req.header("x-amz-date", now.format("%Y%m%dT%H%M%SZ").to_string()).header("x-amz-content-sha256", hash).header("authorization", auth).body(body).send()?)
    }

    fn object(&self, sha: &str) -> String {
        format!("/{}/sha256/{sha}.png", self.name)
    }

    fn put(&self, sha: &str, raw: Vec<u8>) -> Result<()> {
        let r = self.send(reqwest::Method::PUT, &self.object(sha), raw)?;
        if !r.status().is_success() {
            bail!("S3 put {}: {}", self.object(sha), r.status());
        }
        Ok(())
    }

    fn get(&self, sha: &str) -> Result<Vec<u8>> {
        let r = self.send(reqwest::Method::GET, &self.object(sha), vec![])?;
        if !r.status().is_success() {
            bail!("S3 get {}: {}", self.object(sha), r.status());
        }
        Ok(r.bytes()?.to_vec())
    }
}

fn sha_hex(raw: &[u8]) -> String {
    raw.iter().map(|b| format!("{b:02x}")).collect()
}

fn parse(url: &str) -> Result<Bucket> {
    let u = url::Url::parse(url)?;
    let host = u.host_str().unwrap_or_default();
    if !matches!(u.scheme(), "http" | "https") || host.is_empty() || u.username().is_empty() || u.password().is_none() {
        bail!("S3 URL must be http(s)://ACCESS:SECRET@host:port/bucket");
    }
    let name = u.path().trim_matches('/');
    if name.is_empty() {
        bail!("S3 URL has no bucket");
    }
    let dec = |s: &str| Ok::<_, anyhow::Error>(percent_encoding::percent_decode_str(s).decode_utf8()?.into_owned());
    let host = match u.port() {
        Some(p) => format!("{host}:{p}"),
        None => host.to_string(),
    };
    Ok(Bucket {
        base: format!("{}://{host}", u.scheme()),
        host,
        name: name.to_string(),
        key: dec(u.username())?,
        secret: dec(u.password().unwrap_or_default())?,
        http: reqwest::blocking::Client::builder().timeout(Duration::from_secs(60)).no_proxy().redirect(reqwest::redirect::Policy::none()).build()?,
    })
}

/// The bucket named in `url` (http(s)://ACCESS:SECRET@host:port/bucket), created when missing.
pub fn bucket(url: &str) -> Result<Bucket> {
    let b = parse(url)?;
    let path = format!("/{}", b.name);
    match b.send(reqwest::Method::HEAD, &path, vec![])?.status().as_u16() {
        200 => {}
        404 => {
            let r = b.send(reqwest::Method::PUT, &path, vec![])?;
            if !r.status().is_success() {
                bail!("S3 create bucket {}: {}", b.name, r.status());
            }
        }
        s => bail!("S3 bucket {}: HTTP {s}", b.name),
    }
    Ok(b)
}

// ponytail: NoTls; a DSN with sslmode=require fails to connect, add postgres-native-tls if the database leaves the box
pub fn connect(dsn: &str) -> Result<Client> {
    let mut conn = dsn.parse::<postgres::Config>()?.connect_timeout(Duration::from_secs(10)).connect(NoTls)?;
    let mut tx = conn.transaction()?;
    tx.batch_execute(SCHEMA)?;
    tx.commit()?;
    Ok(conn)
}

/// Files under `dir` with extension `ext`, sorted; recursive or one level. A missing dir is empty, an unreadable one an error.
fn files(dir: &Path, ext: &str, recurse: bool) -> Result<Vec<PathBuf>> {
    let mut out = vec![];
    let entries = match std::fs::read_dir(dir) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
        r => r.with_context(|| dir.display().to_string())?,
    };
    for e in entries {
        let (p, t) = {
            let e = e?;
            (e.path(), e.file_type()?)
        };
        if t.is_dir() && recurse {
            out.extend(files(&p, ext, true)?);
        } else if t.is_file() && p.extension().is_some_and(|x| x == ext) {
            out.push(p);
        }
    }
    out.sort();
    Ok(out)
}

fn key(root: &Path, p: &Path) -> Result<String> {
    Ok(p.strip_prefix(root)?.components().map(|c| c.as_os_str().to_string_lossy()).collect::<Vec<_>>().join("/"))
}

/// Append new trace lines (logs are append-only; a torn last line waits), upsert changed world/plan docs, upload changed screenshots.
pub fn sync(conn: &mut Client, root: &Path, shots: Option<&Bucket>) -> Result<Synced> {
    let mut out = Synced::default();
    let mut tx = conn.transaction()?;
    for log in files(root, "jsonl", true)? {
        let source = key(root, &log)?;
        let have = tx.query_one("select coalesce(max(line), 0) from trace where source = $1", &[&source])?.get::<_, i32>(0) as usize;
        let text = std::fs::read_to_string(&log).with_context(|| log.display().to_string())?;
        let mut lines: Vec<&str> = text.split('\n').collect();
        lines.pop();
        if lines.len() < have {
            bail!("{source}: {} lines on disk but {have} stored; logs are append-only", lines.len());
        }
        let insert = tx.prepare("insert into trace (source, line, event, host_ms, rec) values ($1, $2, $3, $4, $5)")?;
        let mut rows = vec![];
        for (i, line) in lines.iter().enumerate().skip(have) {
            let rec: Value = serde_json::from_str(line).map_err(|e| anyhow::anyhow!("{source}:{}: not JSON ({e})", i + 1))?;
            rows.push((i as i32 + 1, rec));
        }
        for (n, rec) in &rows {
            tx.execute(&insert, &[&source, n, &rec.get("event").and_then(Value::as_str), &rec.get("host_ms").and_then(Value::as_i64), rec])?;
        }
        out.lines += rows.len();
    }
    let mut docs = files(&root.join("world"), "yaml", true)?;
    docs.extend(files(&root.join("plans"), "yaml", false)?);
    docs.sort();
    for doc in docs {
        let raw = std::fs::read(&doc)?;
        let body: Value = serde_yaml::from_slice(&raw).with_context(|| doc.display().to_string())?;
        out.docs += tx.execute(
            "insert into world_doc (key, body, sha256) values ($1, $2, $3) on conflict (key) do update \
             set body = excluded.body, sha256 = excluded.sha256, updated = now() where world_doc.sha256 <> excluded.sha256",
            &[&key(root, &doc)?, &body, &sha(&raw)],
        )?;
    }
    if let Some(shots) = shots {
        for png in files(root, "png", true)? {
            let raw = std::fs::read(&png)?;
            let (sum, k, size) = (sha(&raw), key(root, &png)?, raw.len() as i64);
            let stored: Option<String> = tx.query_opt("select sha256 from screenshot where key = $1", &[&k])?.map(|r| r.get(0));
            if stored.as_deref() == Some(sum.as_str()) {
                continue;
            }
            shots.put(&sum, raw)?;
            tx.execute(
                "insert into screenshot (key, sha256, size) values ($1, $2, $3) on conflict (key) do update \
                 set sha256 = excluded.sha256, size = excluded.size, updated = now()",
                &[&k, &sum, &size],
            )?;
            out.shots += 1;
        }
    }
    tx.commit()?;
    Ok(out)
}

pub fn trace(conn: &mut Client, source: &str) -> Result<Vec<Value>> {
    Ok(conn.query("select rec from trace where source = $1 order by line", &[&source])?.iter().map(|r| r.get(0)).collect())
}

pub fn doc(conn: &mut Client, key: &str) -> Result<Value> {
    Ok(conn.query_opt("select body from world_doc where key = $1", &[&key])?.with_context(|| format!("no world doc {key}"))?.get(0))
}

/// The screenshot stored under `key`, checked against its recorded sha256.
pub fn screenshot(conn: &mut Client, shots: &Bucket, key: &str) -> Result<Vec<u8>> {
    let sum: String = conn.query_opt("select sha256 from screenshot where key = $1", &[&key])?.with_context(|| format!("no screenshot {key}"))?.get(0);
    let raw = shots.get(&sum)?;
    if sha(&raw) != sum {
        bail!("{key}: object does not match its stored sha256");
    }
    Ok(raw)
}

#[cfg(test)]
mod tests;
