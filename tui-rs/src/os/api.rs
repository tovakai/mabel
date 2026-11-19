//! Talking to Mabel over HTTP with an API token: sessions, the chat stream, her day, the music
//! proxy, Beeper. Each call follows docs/README/MABEL_OS_API_CONTRACT.md.

use std::time::Duration;

use anyhow::{Result, anyhow};
use futures::{Stream, StreamExt};
use reqwest::{Client, Method, RequestBuilder, Response, header};
use serde_json::{Map, Value};

#[derive(Clone)]
pub struct Mabel {
    pub url: String,
    pub token: String,
    /// "custom" = Mabel's character preset in the web UI; "" sends none.
    pub preset: String,
    http: Client,
}

/// What `stream` hands back: a reply chunk, or a typed event (`final_response`, `tool_start`,
/// `tool_output`, `ui_control`, `error`) as the raw JSON object.
#[derive(Debug, Clone, PartialEq)]
pub enum ChatEvent {
    Delta(String),
    Event { kind: String, body: Map<String, Value> },
}

impl ChatEvent {
    /// An `error` event carrying `text` (so failures come through the same stream).
    fn error(text: impl Into<String>) -> Self {
        let mut body = Map::new();
        body.insert("error".into(), Value::String(text.into()));
        ChatEvent::Event { kind: "error".into(), body }
    }
}

impl Mabel {
    /// A client for Mabel at `url`, sending the token with every request.
    pub fn new(url: &str, token: &str, preset: &str) -> Result<Self> {
        let mut headers = header::HeaderMap::new();
        let mut auth = header::HeaderValue::from_str(&format!("Bearer {token}"))?;
        auth.set_sensitive(true);
        headers.insert(header::AUTHORIZATION, auth);
        let http = Client::builder()
            .default_headers(headers)
            .connect_timeout(Duration::from_secs(10))
            .read_timeout(Duration::from_secs(600)) // a long tool run keeps the stream quiet for minutes
            .user_agent(concat!("MABEL-OS/", env!("CARGO_PKG_VERSION")))
            .build()?;
        Ok(Mabel { url: url.trim_end_matches('/').to_string(), token: token.into(), preset: preset.into(), http })
    }

    /// A request to `path` with the usual 30-second limit.
    fn req(&self, method: Method, path: &str) -> RequestBuilder {
        self.http.request(method, format!("{}{}", self.url, path)).timeout(Duration::from_secs(30))
    }

    /// The response if it succeeded; otherwise an error with the status and the start of the body.
    async fn checked(path: &str, resp: Response) -> Result<Response> {
        let status = resp.status();
        if status.as_u16() >= 300 {
            let body = resp.text().await.unwrap_or_default();
            return Err(anyhow!("{path}: {} {}", status.as_u16(), clip(&body, 200)));
        }
        Ok(resp)
    }

    /// GET `path` and read its JSON.
    async fn get_json(&self, path: &str, params: &[(&str, &str)]) -> Result<Value> {
        let resp = self.req(Method::GET, path).query(params).send().await?;
        Ok(Self::checked(path, resp).await?.json().await?)
    }

    /// GET `path` as bytes (None on any failure: used for pictures).
    async fn get_bytes(&self, path: &str, params: &[(&str, &str)]) -> Option<Vec<u8>> {
        let resp = self.req(Method::GET, path).query(params).send().await.ok()?;
        if resp.status().as_u16() != 200 {
            return None;
        }
        resp.bytes().await.ok().map(|b| b.to_vec())
    }

    // ── chat ─────────────────────────────────────────────────────────────

    /// Live sessions, newest first (by last message, else last update).
    pub async fn sessions(&self) -> Result<Vec<Value>> {
        let data = self.get_json("/api/sessions", &[]).await?;
        let items = match data {
            Value::Array(items) => items,
            Value::Object(mut obj) => match obj.remove("sessions") {
                Some(Value::Array(items)) => items,
                _ => vec![],
            },
            _ => vec![],
        };
        let mut live: Vec<Value> = items.into_iter().filter(|s| !s["archived"].as_bool().unwrap_or(false)).collect();
        let when = |s: &Value| {
            let at = s["last_message_at"].as_str().filter(|v| !v.is_empty());
            at.or(s["updated_at"].as_str()).unwrap_or("").to_string()
        };
        live.sort_by_key(|s| std::cmp::Reverse(when(s)));
        Ok(live)
    }

    /// The user and assistant turns of one chat.
    pub async fn history(&self, session_id: &str) -> Result<Vec<Value>> {
        let data = self.get_json(&format!("/api/history/{session_id}"), &[]).await?;
        let turns = data["history"].as_array().cloned().unwrap_or_default();
        Ok(turns.into_iter().filter(|m| matches!(m["role"].as_str(), Some("user" | "assistant"))).collect())
    }

    /// A new chat on the same model as `like` (tokens must name a registered endpoint, so the
    /// endpoint and model are copied from an existing session).
    pub async fn new_session(&self, name: &str, like: Option<&Value>) -> Result<Value> {
        let newest;
        let like = match like {
            Some(v) => v,
            None => {
                newest = self.sessions().await?.into_iter().next().unwrap_or(Value::Null);
                &newest
            }
        };
        let field = |k: &str| like[k].as_str().unwrap_or("").to_string();
        let form = [
            ("name", name.to_string()),
            ("endpoint_id", field("endpoint_id")),
            ("endpoint_url", field("endpoint_url")),
            ("model", field("model")),
        ];
        let resp = self.req(Method::POST, "/api/session").form(&form).send().await?;
        Ok(Self::checked("new session", resp).await?.json().await?)
    }

    /// The chat stream. `approval` = (approval_id, "approve_once" | "deny") resumes a held tool.
    /// Errors come through as `error` events, so the caller only reads one stream.
    pub async fn stream(
        &self,
        session_id: &str,
        message: &str,
        approval: Option<(&str, &str)>,
    ) -> impl Stream<Item = ChatEvent> + Send + 'static {
        let mut form = vec![("message", message.to_string()), ("session", session_id.into()), ("mode", "chat".into())];
        if !self.preset.is_empty() {
            form.push(("preset_id", self.preset.clone()));
        }
        if let Some((id, decision)) = approval {
            form[0].1 = String::new();
            form.push(("tool_approval_id", id.into()));
            form.push(("tool_approval_decision", decision.into()));
        }
        let sent = self.http.post(format!("{}/api/chat_stream", self.url)).form(&form).send().await;
        let resp = match sent {
            Err(e) => return futures::stream::iter(vec![ChatEvent::error(e.to_string())]).boxed(),
            Ok(r) if r.status().as_u16() >= 300 => {
                let code = r.status().as_u16();
                let body = r.text().await.unwrap_or_default();
                return futures::stream::iter(vec![ChatEvent::error(format!("{code}: {}", clip(&body, 300)))]).boxed();
            }
            Ok(r) => r,
        };
        let bytes = resp.bytes_stream();
        futures::stream::unfold((bytes, Sse::default(), false), |(mut bytes, mut sse, done)| async move {
            if done {
                return None;
            }
            loop {
                if let Some(item) = sse.next_event() {
                    return match item {
                        SseItem::Event(ev) => Some((ev, (bytes, sse, false))),
                        SseItem::Done => None,
                    };
                }
                match bytes.next().await {
                    Some(Ok(chunk)) => sse.feed(&chunk),
                    Some(Err(e)) => {
                        return Some((ChatEvent::error(e.to_string()), (bytes, sse, true)));
                    }
                    None => return None,
                }
            }
        })
        .boxed()
    }

    // ── her day ──────────────────────────────────────────────────────────

    /// LIFE: what she's doing, her jobs, her journal, his lists.
    pub async fn life(&self) -> Result<Value> {
        self.get_json("/api/tui/life", &[]).await
    }

    /// The picture of her last dream, if there is one.
    pub async fn dream_image(&self) -> Option<Vec<u8>> {
        self.get_bytes("/api/tui/dream-image", &[]).await
    }

    // ── music (Mabel's Navidrome proxy) ──────────────────────────────────

    /// A Subsonic call through her Navidrome proxy (`getAlbum`, `search3`…).
    pub async fn music(&self, method: &str, params: &[(&str, &str)]) -> Result<Value> {
        let data = self.get_json(&format!("/api/music/rest/{method}"), params).await?;
        Ok(if data.is_object() { data } else { Value::Object(Map::new()) })
    }

    /// A Subsonic call that changes something (playlists, scrobbles).
    pub async fn music_write(&self, method: &str, params: &Value) -> Result<()> {
        let path = format!("/api/music/rest/{method}");
        let resp = self.req(Method::POST, &path).json(params).send().await?;
        Self::checked(&path, resp).await.map(|_| ())
    }

    /// Cover art, `size` pixels wide.
    pub async fn cover(&self, cover_id: &str, size: u32) -> Option<Vec<u8>> {
        self.get_bytes(&format!("/api/music/cover/{cover_id}"), &[("size", &size.to_string())]).await
    }

    /// The URL mpv streams a song from (through her proxy).
    pub fn stream_url(&self, song_id: &str) -> String {
        format!("{}/api/music/stream/{song_id}", self.url)
    }

    /// Save a song to `path` (through Mabel's proxy, with the token). Returns the bytes saved.
    pub async fn download(&self, song_id: &str, path: &std::path::Path, max_bitrate: i64) -> Result<u64> {
        let mut req = self.http.get(self.stream_url(song_id));
        if max_bitrate > 0 {
            req = req.query(&[("maxBitRate", max_bitrate.to_string())]);
        }
        save_stream(req.send().await?, path).await
    }

    // ── Beeper, relayed by Mabel ─────────────────────────────────────────

    // ── the DreamTape repo (/api/tapes) ──────────────────────────────────

    /// The tapes on Mabel: name, title, author, description, screen, fps, size, sha, published.
    pub async fn tapes(&self) -> Result<Vec<Value>> {
        let data = self.get_json("/api/tapes", &[]).await?;
        Ok(data["tapes"].as_array().cloned().unwrap_or_default())
    }

    /// A tape's .dtape bytes from the repo.
    pub async fn tape_download(&self, name: &str) -> Result<Vec<u8>> {
        let path = format!("/api/tapes/{name}.dtape");
        let resp = self.req(Method::GET, &path).timeout(Duration::from_secs(120)).send().await?;
        Ok(Self::checked(&path, resp).await?.bytes().await?.to_vec())
    }

    /// Put a .dtape in the repo under `name` (replacing an older one).
    pub async fn tape_publish(&self, name: &str, dtape: Vec<u8>) -> Result<Value> {
        let path = format!("/api/tapes/{name}");
        let resp = self.req(Method::PUT, &path).timeout(Duration::from_secs(120)).body(dtape).send().await?;
        Ok(Self::checked(&path, resp).await?.json().await?)
    }

    /// Take a tape out of the repo.
    pub async fn tape_unpublish(&self, name: &str) -> Result<()> {
        let path = format!("/api/tapes/{name}");
        let resp = self.req(Method::DELETE, &path).send().await?;
        Self::checked(&path, resp).await.map(|_| ())
    }

    /// Is Beeper reachable through her, and with which accounts?
    pub async fn beeper_status(&self) -> Result<Value> {
        self.get_json("/api/beeper/status", &[]).await
    }

    /// One relayed Beeper API call, `path` like "v1/chats". Errors carry Beeper's own message.
    pub async fn beeper(
        &self,
        method: Method,
        path: &str,
        params: &[(&str, &str)],
        body: Option<&Value>,
    ) -> Result<Value> {
        let mut req = self.req(method, &format!("/api/beeper/{}", path.trim_start_matches('/'))).query(params);
        if let Some(body) = body {
            req = req.json(body);
        }
        let resp = req.send().await?;
        let status = resp.status().as_u16();
        let is_json =
            resp.headers().get(header::CONTENT_TYPE).and_then(|v| v.to_str().ok()).is_some_and(|v| v.contains("json"));
        let bytes = resp.bytes().await?;
        if status >= 300 {
            let text = String::from_utf8_lossy(&bytes).to_string();
            let detail = serde_json::from_slice::<Value>(&bytes)
                .ok()
                .and_then(|v| {
                    v.get("detail").or(v.get("message")).map(|d| d.as_str().map_or(d.to_string(), String::from))
                })
                .unwrap_or(text);
            return Err(anyhow!("{status}: {}", clip(&detail, 160)));
        }
        if is_json {
            Ok(serde_json::from_slice(&bytes)?)
        } else {
            Ok(Value::Array(bytes.iter().map(|b| Value::from(*b)).collect()))
        }
    }
}

/// Stream a response body to `path` via a .part file, so a broken download never looks finished.
pub async fn save_stream(resp: Response, path: &std::path::Path) -> Result<u64> {
    use tokio::io::AsyncWriteExt;
    if resp.status().as_u16() >= 300 {
        return Err(anyhow!("download failed: {}", resp.status().as_u16()));
    }
    if let Some(dir) = path.parent() {
        tokio::fs::create_dir_all(dir).await?;
    }
    let part = path.with_extension(format!("{}.part", path.extension().and_then(|e| e.to_str()).unwrap_or("")));
    let mut file = tokio::fs::File::create(&part).await?;
    let mut size = 0u64;
    let mut body = resp.bytes_stream();
    while let Some(chunk) = body.next().await {
        let chunk = chunk?;
        file.write_all(&chunk).await?;
        size += chunk.len() as u64;
    }
    file.flush().await?;
    drop(file);
    tokio::fs::rename(&part, path).await?;
    Ok(size)
}

/// `text` cut to `max` characters (for error messages).
fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

// ── server-sent events ──────────────────────────────────────────────────

/// One item from the reply stream: an event, or the end.
enum SseItem {
    Event(ChatEvent),
    Done,
}

/// Splits the byte stream into `data: …` lines (chunks can cut a line, or a UTF-8 character, anywhere).
#[derive(Default)]
struct Sse {
    buf: Vec<u8>,
}

impl Sse {
    /// Add bytes as they arrive.
    fn feed(&mut self, chunk: &[u8]) {
        self.buf.extend_from_slice(chunk);
    }

    /// The next complete `data:` line as an event (None: need more bytes).
    fn next_event(&mut self) -> Option<SseItem> {
        while let Some(end) = self.buf.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = self.buf.drain(..=end).collect();
            let line = String::from_utf8_lossy(&line);
            let Some(payload) = line.trim_end_matches(['\r', '\n']).strip_prefix("data: ") else {
                continue;
            };
            let payload = payload.trim();
            if payload == "[DONE]" {
                return Some(SseItem::Done);
            }
            if let Some(event) = parse_event(payload) {
                return Some(SseItem::Event(event));
            }
        }
        None
    }
}

/// One `data:` payload: `{"delta": …}` is reply text, `{"type": …}` a typed event,
/// `{"error": …}` an error; anything else is ignored.
fn parse_event(payload: &str) -> Option<ChatEvent> {
    let Ok(Value::Object(body)) = serde_json::from_str::<Value>(payload) else {
        return None;
    };
    let kind = body.get("type").and_then(|t| t.as_str()).map(String::from);
    match kind {
        None if body.contains_key("delta") => Some(ChatEvent::Delta(body["delta"].as_str().unwrap_or("").to_string())),
        None if body.contains_key("error") => Some(ChatEvent::Event { kind: "error".into(), body }),
        None => None,
        Some(kind) => Some(ChatEvent::Event { kind, body }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn drain(sse: &mut Sse) -> Vec<ChatEvent> {
        let mut out = vec![];
        while let Some(item) = sse.next_event() {
            match item {
                SseItem::Event(e) => out.push(e),
                SseItem::Done => break,
            }
        }
        out
    }

    #[test]
    fn sse_lines_split_across_chunks() {
        let mut sse = Sse::default();
        sse.feed(b"data: {\"delta\": \"hej \"}\n\ndata: {\"del");
        assert_eq!(drain(&mut sse), vec![ChatEvent::Delta("hej ".into())]);
        sse.feed("ta\": \"d\u{e5}\"}\r\n: ping\n".as_bytes());
        assert_eq!(drain(&mut sse), vec![ChatEvent::Delta("då".into())]);
    }

    #[test]
    fn typed_events_untyped_errors_and_done() {
        let mut sse = Sse::default();
        sse.feed(b"data: {\"type\": \"tool_start\", \"tool\": \"x\"}\ndata: {\"error\": \"boom\"}\ndata: [DONE]\ndata: {\"delta\": \"late\"}\n");
        let events = drain(&mut sse);
        assert!(matches!(&events[0], ChatEvent::Event { kind, .. } if kind == "tool_start"));
        assert!(matches!(&events[1], ChatEvent::Event { kind, body } if kind == "error" && body["error"] == "boom"));
        assert_eq!(events.len(), 2);
    }
}
