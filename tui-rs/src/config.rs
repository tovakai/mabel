//! `~/.config/mabel-tui/config.toml`: where Mabel is, the API token, and per-machine choices.
//!
//! The same file the Python client reads (docs/README/MABEL_OS_API_CONTRACT.md lists every key).
//! Like the Python version, an empty value counts as "not set" and falls back to the default.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use toml::Table;

#[derive(Debug, Clone, PartialEq)]
pub struct Navidrome {
    pub url: String,
    pub user: String,
    pub password: String,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub url: String,
    pub token: String,
    pub volume: i64,
    pub index_url: String,
    pub vault: String,
    pub journal: String,
    pub navidrome: Option<Navidrome>,
    pub preset: String,
    pub avatar: String,
    pub avatar_fps: f64,
    pub offline_bitrate: i64,
    pub pixel_dir: String,
    pub tracker_dir: String,
    pub tracker_rate: i64,
    pub deck: bool,
    pub auto_update: bool,
    /// The whole file, for the parts other modules read themselves (palette, colors).
    pub raw: Table,
}

fn xdg(var: &str, fallback: &str) -> PathBuf {
    match std::env::var_os(var) {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => home().join(fallback),
    }
}

pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

pub fn config_path() -> PathBuf {
    xdg("XDG_CONFIG_HOME", ".config").join("mabel-tui").join("config.toml")
}

pub fn cache_dir() -> PathBuf {
    xdg("XDG_CACHE_HOME", ".cache").join("mabel-tui")
}

/// `~/…` → the home directory.
pub fn expand(path: &str) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => home().join(rest),
        None if path == "~" => home(),
        None => PathBuf::from(path),
    }
}

pub fn load(path: &Path) -> Result<Config> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => bail!(
            "No config at {}. It needs:\n  url = \"http://127.0.0.1:7000\"\n  token = \"ody_…\"  \
             (an API token with the tui profile: chat, music, life:read)",
            path.display()
        ),
        Err(e) => return Err(e).with_context(|| format!("reading {}", path.display())),
    };
    parse(&text, path)
}

pub fn parse(text: &str, path: &Path) -> Result<Config> {
    let raw: Table = text.parse().with_context(|| format!("{}: not valid TOML", path.display()))?;
    let t = Lookup(&raw);
    let url = t.str("url", "").trim_end_matches('/').to_string();
    let token = t.str("token", "");
    if url.is_empty() || !token.starts_with("ody_") {
        bail!("{}: url and token (ody_…) are both needed", path.display());
    }
    let navidrome = match raw.get("navidrome").and_then(|v| v.as_table()) {
        Some(section) if !section.is_empty() => {
            let nd = Lookup(section);
            let (url, user, password) = (nd.str("url", ""), nd.str("user", ""), nd.str("password", ""));
            if url.is_empty() || user.is_empty() || password.is_empty() {
                bail!("{}: [navidrome] needs url, user and password", path.display());
            }
            Some(Navidrome { url, user, password })
        }
        _ => None,
    };
    Ok(Config {
        url,
        token,
        volume: t.int("volume", 70),
        index_url: t.str("index_url", "http://127.0.0.1:8766").trim_end_matches('/').to_string(),
        vault: t.str("vault", "~/Documents/core of truth"),
        journal: t.str("journal", "~/Documents/mabel/journal"),
        navidrome,
        // preset is the one key where "" means something ("no preset"), as in the Python client.
        preset: raw.get("preset").and_then(|v| v.as_str()).unwrap_or("custom").to_string(),
        avatar: t.str("avatar", ""),
        avatar_fps: t.float("avatar_fps", 4.0),
        offline_bitrate: t.int("offline_bitrate", 0),
        pixel_dir: t.str("pixel_dir", "~/Pictures/MABEL-OS"),
        tracker_dir: t.str("tracker_dir", "~/Music/MABEL-OS"),
        tracker_rate: t.int("tracker_rate", 44100),
        deck: t.bool("deck", false),
        auto_update: t.bool("auto_update", false),
        raw,
    })
}

/// Python's `data.get(key) or default`: missing, empty or zero falls back.
struct Lookup<'a>(&'a Table);

impl Lookup<'_> {
    fn str(&self, key: &str, default: &str) -> String {
        match self.0.get(key) {
            Some(toml::Value::String(s)) if !s.is_empty() => s.clone(),
            Some(v) if !v.is_str() => v.to_string(),
            _ => default.to_string(),
        }
    }

    fn int(&self, key: &str, default: i64) -> i64 {
        match self.0.get(key) {
            Some(toml::Value::Integer(n)) if *n != 0 => *n,
            Some(toml::Value::Float(f)) if *f != 0.0 => *f as i64,
            Some(toml::Value::String(s)) => s.trim().parse().unwrap_or(default),
            _ => default,
        }
    }

    fn float(&self, key: &str, default: f64) -> f64 {
        match self.0.get(key) {
            Some(toml::Value::Float(f)) if *f != 0.0 => *f,
            Some(toml::Value::Integer(n)) if *n != 0 => *n as f64,
            _ => default,
        }
    }

    fn bool(&self, key: &str, default: bool) -> bool {
        self.0.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(text: &str) -> Result<Config> {
        parse(text, Path::new("test.toml"))
    }

    #[test]
    fn minimal_config_gets_python_defaults() {
        let c = p("url = \"http://h:7000/\"\ntoken = \"ody_x\"").unwrap();
        assert_eq!(c.url, "http://h:7000");
        assert_eq!(c.volume, 70);
        assert_eq!(c.preset, "custom");
        assert_eq!(c.index_url, "http://127.0.0.1:8766");
        assert_eq!(c.tracker_rate, 44100);
        assert!(!c.deck && c.navidrome.is_none());
    }

    #[test]
    fn empty_values_fall_back_but_empty_preset_means_none() {
        let c = p("url = \"u\"\ntoken = \"ody_x\"\nvault = \"\"\npreset = \"\"\nvolume = 0").unwrap();
        assert_eq!(c.vault, "~/Documents/core of truth");
        assert_eq!(c.preset, "");
        assert_eq!(c.volume, 70);
    }

    #[test]
    fn token_and_navidrome_are_checked() {
        assert!(p("url = \"u\"\ntoken = \"abc\"").is_err());
        assert!(p("url = \"u\"\ntoken = \"ody_x\"\n[navidrome]\nurl = \"n\"").is_err());
        let c = p("url = \"u\"\ntoken = \"ody_x\"\n[navidrome]\nurl = \"n\"\nuser = \"a\"\npassword = \"b\"").unwrap();
        assert_eq!(c.navidrome.unwrap().user, "a");
    }
}
