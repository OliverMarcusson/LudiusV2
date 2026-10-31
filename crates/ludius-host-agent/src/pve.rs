use crate::error::{E, R, down};
use reqwest::{Client, Method, RequestBuilder};
use serde_json::Value;
use std::time::Duration;

/// The PVE REST API: an API token for everything, a root@pam ticket only where PVE demands root (`args`).
pub struct Pve {
    http: Client,
    base: String,
    token: String,
    root_password: Option<String>,
}

impl Pve {
    pub fn new(base: &str, token: String, ca_pem: Option<&[u8]>, root_password: Option<String>) -> R<Self> {
        let mut b = Client::builder().timeout(Duration::from_secs(60));
        if let Some(pem) = ca_pem {
            b = b.add_root_certificate(reqwest::Certificate::from_pem(pem).map_err(down)?);
        }
        Ok(Pve { http: b.build().map_err(down)?, base: format!("{}/api2/json", base.trim_end_matches('/')), token, root_password })
    }

    pub async fn get(&self, path: &str, query: &[(&str, &str)]) -> R<Value> {
        self.send(self.req(Method::GET, path).query(query)).await
    }

    pub async fn post(&self, path: &str, form: &[(&str, &str)]) -> R<Value> {
        self.send(self.req(Method::POST, path).form(form)).await
    }

    pub async fn delete(&self, path: &str, query: &[(&str, &str)]) -> R<Value> {
        self.send(self.req(Method::DELETE, path).query(query)).await
    }

    /// PUT as root@pam with a fresh ticket: PVE lets only root@pam itself (not its tokens) set `args`.
    pub async fn put_as_root(&self, path: &str, form: &[(&str, &str)]) -> R<Value> {
        let password = self
            .root_password
            .as_deref()
            .ok_or_else(|| E::Denied("no root@pam password secret: cannot set args".into()))?;
        let t = self
            .send(self.http.post(format!("{}/access/ticket", self.base)).form(&[("username", "root@pam"), ("password", password)]))
            .await?;
        let (Some(ticket), Some(csrf)) = (t["ticket"].as_str(), t["CSRFPreventionToken"].as_str()) else {
            return Err(down("PVE ticket: no ticket in the answer"));
        };
        let rb = self
            .http
            .put(format!("{}{path}", self.base))
            .header("Cookie", format!("PVEAuthCookie={ticket}"))
            .header("CSRFPreventionToken", csrf)
            .form(form);
        self.send(rb).await
    }

    fn req(&self, m: Method, path: &str) -> RequestBuilder {
        self.http.request(m, format!("{}{path}", self.base)).header("Authorization", format!("PVEAPIToken={}", self.token))
    }

    async fn send(&self, rb: RequestBuilder) -> R<Value> {
        let res = rb.send().await.map_err(|e| E::Down(format!("PVE API: {e}")))?;
        let status = res.status();
        // PVE puts the error text in the HTTP reason phrase.
        let reason = res
            .extensions()
            .get::<hyper::ext::ReasonPhrase>()
            .map(|r| String::from_utf8_lossy(r.as_bytes()).into_owned())
            .unwrap_or_else(|| status.canonical_reason().unwrap_or("").to_string());
        let mut body: Value = res.json().await.unwrap_or(Value::Null);
        if status.is_success() {
            return Ok(body["data"].take());
        }
        let detail = match &body["errors"] {
            Value::Null => String::new(),
            errors => format!(" {errors}"),
        };
        let msg = format!("PVE {}: {reason}{detail}", status.as_u16());
        Err(match status.as_u16() {
            401 | 403 => E::Denied(msg),
            400 => E::Arg(msg),
            _ if reason.contains("does not exist") => E::NotFound(msg),
            _ => E::Failed(msg),
        })
    }
}
