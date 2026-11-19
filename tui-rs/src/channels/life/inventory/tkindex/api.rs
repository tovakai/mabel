//! tk-index's HTTP API: loading the whole index, one item, a photo, and writes.

use super::*;

/// tk-index's API, at `index_url` from config.toml (no token: it's on the tailnet).
#[derive(Clone)]
pub struct TkIndex {
    url: String,
    http: Client,
}

impl TkIndex {
    /// No auth: loopback, or the tailnet through tailscale serve.
    pub fn new(url: &str) -> Self {
        let http = Client::builder().timeout(Duration::from_secs(20)).build().unwrap_or_default();
        TkIndex { url: url.trim_end_matches('/').to_string(), http }
    }

    /// The answer's JSON, or tk-index's own error message.
    async fn checked(resp: reqwest::Response) -> Result<Value> {
        let status = resp.status().as_u16();
        let body = resp.bytes().await?;
        if status >= 300 {
            let msg = serde_json::from_slice::<Value>(&body)
                .ok()
                .and_then(|v| v["error"].as_str().map(String::from))
                .unwrap_or_else(|| {
                    format!("{status}: {}", String::from_utf8_lossy(&body).chars().take(200).collect::<String>())
                });
            return Err(anyhow!(msg));
        }
        if body.is_empty() {
            return Ok(Value::Null);
        }
        Ok(serde_json::from_slice(&body)?)
    }

    /// GET a path.
    pub async fn get(&self, path: &str) -> Result<Value> {
        Self::checked(self.http.get(format!("{}/api/{path}", self.url)).send().await?).await
    }

    /// POST JSON to a path (every write; tk-index keeps it in its undo history).
    pub async fn post(&self, path: &str, data: &Value) -> Result<Value> {
        Self::checked(self.http.post(format!("{}/api/{path}", self.url)).json(data).send().await?).await
    }

    /// The whole index at once.
    pub async fn world(&self) -> Result<World> {
        Ok(World::new(self.get("world").await?))
    }

    /// One item with everything about it (history, photos).
    pub async fn item(&self, id: &str) -> Result<Value> {
        self.get(&format!("items/{id}")).await
    }

    /// A photo's bytes (None if it isn't there).
    pub async fn image(&self, path: &str) -> Option<Vec<u8>> {
        let resp = self.http.get(format!("{}/images/{path}", self.url)).send().await.ok()?;
        if resp.status().as_u16() != 200 {
            return None;
        }
        resp.bytes().await.ok().map(|b| b.to_vec())
    }
}
