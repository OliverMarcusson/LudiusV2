use std::cell::Cell;
use std::time::Duration;

use anyhow::{Result, anyhow, bail};
use serde::de::DeserializeOwned;
use serde_json::{Value, json};

use crate::calls;

/// The default; LUDIUS_PLANNER_URL points the planner at another OpenAI-compatible endpoint.
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

fn validated<M: DeserializeOwned + Check>(text: &str) -> Result<M> {
    let mut decoder = serde_json::Deserializer::from_str(strip_fence(text));
    let model: M = serde_path_to_error::deserialize(&mut decoder)?;
    decoder.end()?;
    model.check()?;
    Ok(model)
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
            let got = validated::<M>(&text);
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
        let endpoint = std::env::var("LUDIUS_PLANNER_URL").unwrap_or_else(|_| ENDPOINT.into());
        let r = self.http.post(&endpoint).bearer_auth(&self.key).header("X-Title", "Ludius planner").json(body).send()?;
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
    use super::*;

    #[test]
    fn malformed_writer_content_names_the_field_for_correction() {
        #[derive(serde::Deserialize)]
        struct Draft { items: Vec<crate::planner::PlanItem> }
        impl Check for Draft {}
        let bad = r#"{"items":[{"at":"09:00","template":"word_short_document","params":{"body":["first","second"]}}]}"#;
        let error = validated::<Draft>(bad).err().unwrap().to_string();
        assert!(error.contains("items[0].params.body"), "{error}");
        assert!(error.contains("expected a string"), "{error}");
        let corrected = bad.replace(r#"["first","second"]"#, r#""first\nsecond""#);
        assert_eq!(validated::<Draft>(&corrected).unwrap().items[0].params["body"], "first\nsecond");
        assert!(validated::<Draft>(&format!("{corrected} garbage")).is_err());
    }

    #[test]
    fn fence() {
        assert_eq!(super::strip_fence("```json\n{\"a\":1}\n```"), "{\"a\":1}");
        assert_eq!(super::strip_fence(" {} "), "{}");
    }
}
