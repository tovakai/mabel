//! Where AUDIO gets its music: Mabel's proxy (/api/music/*, with the TUI token) or Navidrome
//! directly (`[navidrome]` in config.toml), so a device keeps its music when Mabel or the whole
//! PC is down. Both speak Subsonic and hand back the body of `subsonic-response`.

use std::time::Duration;

use anyhow::{Result, anyhow};
use reqwest::Client;
use serde_json::Value;

use crate::os::api::Mabel;
use crate::os::config::Navidrome as NavidromeConfig;

const API_VERSION: &str = "1.16.1";
const CLIENT: &str = "mabel-os";

#[derive(Clone)]
pub enum Music {
    Mabel(Mabel),
    Navidrome { url: String, user: String, password: String, http: Client },
}

/// A fresh salt for Subsonic's token auth (no crypto needed: it only varies the hash).
fn salt() -> String {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_nanos());
    format!("{:016x}", (nanos as u64).wrapping_mul(0x9E3779B97F4A7C15).rotate_left(23))
}

impl Music {
    pub fn new(mabel: &Mabel, navidrome: Option<&NavidromeConfig>) -> Self {
        match navidrome {
            None => Music::Mabel(mabel.clone()),
            Some(nd) => Music::Navidrome {
                url: nd.url.trim_end_matches('/').to_string(),
                user: nd.user.clone(),
                password: nd.password.clone(),
                http: Client::builder()
                    .connect_timeout(Duration::from_secs(10))
                    .read_timeout(Duration::from_secs(600))
                    .build()
                    .unwrap_or_default(),
            },
        }
    }

    fn auth(user: &str, password: &str) -> Vec<(String, String)> {
        let s = salt();
        let token = format!("{:x}", md5::compute(format!("{password}{s}")));
        [
            ("u", user.to_string()),
            ("t", token),
            ("s", s),
            ("v", API_VERSION.into()),
            ("c", CLIENT.into()),
            ("f", "json".into()),
        ]
        .into_iter()
        .map(|(k, v)| (k.to_string(), v))
        .collect()
    }

    /// Navidrome's query: auth, then each param (a JSON array becomes the key repeated).
    fn params(user: &str, password: &str, params: &Value) -> Vec<(String, String)> {
        let mut pairs = Self::auth(user, password);
        for (k, v) in params.as_object().into_iter().flatten() {
            let values: Vec<&Value> = match v {
                Value::Array(items) => items.iter().collect(),
                Value::Null => vec![],
                other => vec![other],
            };
            for v in values {
                pairs.push((k.clone(), v.as_str().map_or_else(|| v.to_string(), String::from)));
            }
        }
        pairs
    }

    /// A Subsonic read, e.g. `music("getAlbum", json!({"id": id}))`.
    pub async fn music(&self, method: &str, params: Value) -> Result<Value> {
        match self {
            Music::Mabel(api) => {
                let pairs: Vec<(String, String)> = params
                    .as_object()
                    .into_iter()
                    .flatten()
                    .map(|(k, v)| (k.clone(), v.as_str().map_or_else(|| v.to_string(), String::from)))
                    .collect();
                let borrowed: Vec<(&str, &str)> = pairs.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
                api.music(method, &borrowed).await
            }
            Music::Navidrome { url, user, password, http } => {
                let resp = http
                    .get(format!("{url}/rest/{method}"))
                    .query(&Self::params(user, password, &params))
                    .timeout(Duration::from_secs(20))
                    .send()
                    .await?;
                let code = resp.status().as_u16();
                let body: Value = resp.json().await?;
                let data = body["subsonic-response"].clone();
                if data["status"] != "ok" {
                    let msg = data["error"]["message"].as_str().map_or(format!("Navidrome said {code}"), String::from);
                    return Err(anyhow!(msg));
                }
                Ok(data)
            }
        }
    }

    /// A Subsonic write (scrobble, playlists). Lists are JSON arrays.
    pub async fn music_write(&self, method: &str, params: Value) -> Result<()> {
        match self {
            Music::Mabel(api) => api.music_write(method, &params).await,
            Music::Navidrome { .. } => self.music(method, params).await.map(|_| ()),
        }
    }

    pub async fn cover(&self, cover_id: &str, size: u32) -> Option<Vec<u8>> {
        match self {
            Music::Mabel(api) => api.cover(cover_id, size).await,
            Music::Navidrome { url, user, password, http } => {
                let q = Self::params(user, password, &serde_json::json!({"id": cover_id, "size": size}));
                let resp = http.get(format!("{url}/rest/getCoverArt")).query(&q).send().await.ok()?;
                let image = resp
                    .headers()
                    .get("content-type")
                    .and_then(|v| v.to_str().ok())
                    .is_some_and(|t| t.starts_with("image"));
                if resp.status().as_u16() != 200 || !image {
                    return None;
                }
                resp.bytes().await.ok().map(|b| b.to_vec())
            }
        }
    }

    /// Where mpv streams a song. Navidrome's auth rides in the URL; mpv gets URLs over its IPC
    /// socket, never on a command line, so neither shows in the process list.
    pub fn stream_url(&self, song_id: &str) -> String {
        match self {
            Music::Mabel(api) => api.stream_url(song_id),
            Music::Navidrome { url, user, password, .. } => {
                let q = Self::params(user, password, &serde_json::json!({"id": song_id}));
                let query: Vec<String> = q.iter().map(|(k, v)| format!("{}={}", enc(k), enc(v))).collect();
                format!("{url}/rest/stream?{}", query.join("&"))
            }
        }
    }

    /// Save a song to `path` (`max_bitrate` > 0 asks for a transcode). Returns the bytes saved.
    pub async fn download(&self, song_id: &str, path: &std::path::Path, max_bitrate: i64) -> Result<u64> {
        match self {
            Music::Mabel(api) => api.download(song_id, path, max_bitrate).await,
            Music::Navidrome { url, user, password, http } => {
                let mut params = serde_json::json!({"id": song_id});
                if max_bitrate > 0 {
                    params["maxBitRate"] = max_bitrate.into();
                }
                let resp =
                    http.get(format!("{url}/rest/stream")).query(&Self::params(user, password, &params)).send().await?;
                crate::os::api::save_stream(resp, path).await
            }
        }
    }

    /// The Authorization header mpv needs (Mabel's proxy only).
    pub fn bearer(&self) -> Option<String> {
        match self {
            Music::Mabel(api) => Some(api.token.clone()),
            Music::Navidrome { .. } => None,
        }
    }
}

/// Percent-encoding for a query value.
fn enc(s: &str) -> String {
    s.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => (b as char).to_string(),
            _ => format!("%{b:02X}"),
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn navidrome_params_hash_and_repeat() {
        let pairs = Music::params("mabel", "pw", &json!({"songIdToAdd": ["a", "b"], "size": 60, "none": null}));
        let get = |k: &str| pairs.iter().filter(|(key, _)| key == k).map(|(_, v)| v.as_str()).collect::<Vec<_>>();
        let s = get("s")[0];
        assert_eq!(get("t")[0], format!("{:x}", md5::compute(format!("pw{s}"))));
        assert_eq!(get("songIdToAdd"), vec!["a", "b"]);
        assert_eq!(get("size"), vec!["60"]);
        assert!(get("none").is_empty());
        assert_eq!(enc("a b/ö"), "a%20b%2F%C3%B6");
    }
}
