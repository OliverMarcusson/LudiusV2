use std::cell::Cell;
use std::time::Duration;

use anyhow::{Result, anyhow, bail};
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use crate::calls;

pub const ENDPOINT: &str = "https://openrouter.ai/api/v1/chat/completions";
pub const DEFAULT_MODEL: &str = "deepseek/deepseek-v4.1-flash-1017";

/// Model-level checks after serde's shape checks (pydantic validators).
pub trait Check {
    fn check(&self) -> Result<()> {
        Ok(())
    }
}

pub struct LlmClient {
    pub model: String,
    http: reqwest::blocking::Client,
    key: String,
    pub cost: Cell<f64>,
}

fn strip_fence(t: &str) -> &str {
    let t = t.trim();
    let t = t.strip_prefix("```json").or_else(|| t.strip_prefix("```")).unwrap_or(t).trim_start();
    t.strip_suffix("```").unwrap_or(t).trim_end()
}

impl LlmClient {
    pub fn new(api_key: &str, model: Option<&str>) -> Result<Self> {
        if api_key.is_empty() {
            bail!("OPENROUTER_API_KEY is not set");
        }
        let http = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(180))
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        Ok(Self { model: model.unwrap_or(DEFAULT_MODEL).into(), http, key: api_key.into(), cost: Cell::new(0.0) })
    }

    pub fn json<M: DeserializeOwned + Check>(&self, system: &str, user: &str, temperature: f64, attempts: usize) -> Result<M> {
        let mut messages = vec![
            json!({"role": "system", "content": format!("{system}\nAnswer with one JSON object only, no prose, no code fence.")}),
            json!({"role": "user", "content": user}),
        ];
        for attempt in 0..attempts {
            let text = self.complete(&messages, temperature)?;
            let got = serde_json::from_str::<M>(strip_fence(&text)).map_err(anyhow::Error::from).and_then(|m| m.check().map(|()| m));
            match got {
                Ok(m) => return Ok(m),
                Err(e) if attempt == attempts - 1 => bail!("invalid {}: {e}", std::any::type_name::<M>()),
                Err(e) => {
                    messages.push(json!({"role": "assistant", "content": text}));
                    messages.push(json!({"role": "user", "content": format!("That did not validate: {e}\nSend the corrected JSON object.")}));
                }
            }
        }
        bail!("no attempts")
    }

    fn complete(&self, messages: &[Value], temperature: f64) -> Result<String> {
        let body = json!({"model": self.model, "messages": messages, "temperature": temperature, "usage": {"include": true}});
        calls::count("llm")?;
        let data = self.post(&body)?;
        data["choices"][0]["message"]["content"]
            .as_str()
            .map(str::to_string)
            .ok_or_else(|| anyhow!("unexpected response: {}", data.to_string().chars().take(300).collect::<String>()))
    }

    pub fn post(&self, body: &Value) -> Result<Value> {
        let r = self.http.post(ENDPOINT).bearer_auth(&self.key).header("X-Title", "Ludius planner").json(body).send()?;
        let status = r.status();
        let text = r.text()?;
        if status != 200 {
            bail!("OpenRouter {}: {}", status.as_u16(), text.chars().take(300).collect::<String>());
        }
        let data: Value = serde_json::from_str(&text)?;
        self.cost.set(self.cost.get() + data["usage"]["cost"].as_f64().unwrap_or(0.0));
        Ok(data)
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn fence() {
        assert_eq!(super::strip_fence("```json\n{\"a\":1}\n```"), "{\"a\":1}");
        assert_eq!(super::strip_fence(" {} "), "{}");
    }
}
