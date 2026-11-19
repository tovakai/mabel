//! `~/.config/mabel-tui/config.toml`: where Mabel is, the API token, and per-machine choices.
//!
//! The same file the Python client reads (docs/README/MABEL_OS_API_CONTRACT.md lists every key).
//! Like the Python version, an empty value counts as "not set" and falls back to the default.

use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use toml::Table;

/// `[navidrome]` in config.toml: talk to Navidrome directly instead of through Mabel's proxy
/// (the deck does, so music works when Mabel's PC sleeps).
#[derive(Debug, Clone, PartialEq)]
pub struct Navidrome {
    pub url: String,
    pub user: String,
    pub password: String,
}

#[derive(Debug, Clone)]
/// Everything in ~/.config/mabel-tui/config.toml (defaults in `parse`). Each field is a key.
pub struct Config {
    /// Mabel's address, e.g. "http://127.0.0.1:7000" (required).
    pub url: String,
    /// An API token with the tui profile, "ody_…" (required; pairing writes it).
    pub token: String,
    /// Music volume at start, 0-100 (70).
    pub volume: i64,
    /// tk-index (the Gremlin Index) for INVENTORY ("http://127.0.0.1:8766").
    pub index_url: String,
    /// His Obsidian vault, and Mabel's journal (VAULT, COURSE, housekeeping).
    pub vault: String,
    pub journal: String,
    /// `[navidrome]`: music straight from Navidrome (None: through Mabel).
    pub navidrome: Option<Navidrome>,
    /// Mabel's character preset sent with chats ("custom"; "" sends none).
    pub preset: String,
    /// A folder with another avatar for COMMS ("" = the built-in one), and its frames per second (4).
    pub avatar: String,
    pub avatar_fps: f64,
    /// HOME's wallpaper: a .png (Aseprite exports these) or a PIXEL .mpx; "" = none. PIXEL's
    /// :wallpaper sets it.
    pub wallpaper: String,
    /// Bitrate for offline downloads in kbps (0 = the original files).
    pub offline_bitrate: i64,
    /// Where PIXEL and TRACKER keep their files.
    pub pixel_dir: String,
    pub tracker_dir: String,
    /// TRACKER's and tapes' audio sample rate (44100).
    pub tracker_rate: i64,
    /// This is the deck: SYS gets every page and the machine's power (false). (The narrow
    /// layouts come from the screen's width, not from this.)
    pub deck: bool,
    /// Install updates by itself when idle and nothing plays (false: wait for F12).
    pub auto_update: bool,
    /// The whole file, for the parts other modules read themselves (palette, colors).
    pub raw: Table,
}

/// An XDG folder (`var` if set, else `~/fallback`).
fn xdg(var: &str, fallback: &str) -> PathBuf {
    match std::env::var_os(var) {
        Some(dir) if !dir.is_empty() => PathBuf::from(dir),
        _ => home().join(fallback),
    }
}

/// The home folder.
pub fn home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// ~/.config/mabel-tui/config.toml (or under XDG_CONFIG_HOME).
pub fn config_path() -> PathBuf {
    xdg("XDG_CONFIG_HOME", ".config").join("mabel-tui").join("config.toml")
}

/// ~/.cache/mabel-tui: small things worth keeping but fine to lose (covers, PIXEL and
/// TRACKER's last-opened file, the player's socket if there is no runtime dir).
pub fn cache_dir() -> PathBuf {
    xdg("XDG_CACHE_HOME", ".cache").join("mabel-tui")
}

/// `text` (a config.toml) with the top-level `key` set to the string `value`. An existing
/// `key = …` line before the first `[section]` is replaced; otherwise a new line goes at the
/// top. The rest of the file (comments, layout) stays as it is.
pub fn set_top_key(text: &str, key: &str, value: &str) -> String {
    let line = format!("{key} = {}", toml::Value::from(value));
    let mut out = Vec::new();
    let mut done = false;
    let mut in_top = true;
    for l in text.lines() {
        let t = l.trim_start();
        if t.starts_with('[') {
            in_top = false;
        }
        let is_key = t.strip_prefix(key).is_some_and(|rest| rest.trim_start().starts_with('='));
        if in_top && !done && is_key {
            out.push(line.clone());
            done = true;
        } else {
            out.push(l.to_string());
        }
    }
    if !done {
        out.insert(0, line);
    }
    let mut joined = out.join("\n");
    joined.push('\n');
    joined
}

/// Set one top-level key in this machine's config.toml (made if missing).
pub fn save_top_key(key: &str, value: &str) -> std::io::Result<PathBuf> {
    let path = config_path();
    let text = std::fs::read_to_string(&path).unwrap_or_default();
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(&path, set_top_key(&text, key, value))?;
    Ok(path)
}

/// `~/…` → the home directory.
pub fn expand(path: &str) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => home().join(rest),
        None if path == "~" => home(),
        None => PathBuf::from(path),
    }
}

/// Read the config file (a missing file says what it needs).
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

/// Config from TOML text, with every default. `path` is only for error messages.
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
        wallpaper: t.str("wallpaper", ""),
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
    /// A text key, or `default`.
    fn str(&self, key: &str, default: &str) -> String {
        match self.0.get(key) {
            Some(toml::Value::String(s)) if !s.is_empty() => s.clone(),
            Some(v) if !v.is_str() => v.to_string(),
            _ => default.to_string(),
        }
    }

    /// A whole-number key, or `default`.
    fn int(&self, key: &str, default: i64) -> i64 {
        match self.0.get(key) {
            Some(toml::Value::Integer(n)) if *n != 0 => *n,
            Some(toml::Value::Float(f)) if *f != 0.0 => *f as i64,
            Some(toml::Value::String(s)) => s.trim().parse().unwrap_or(default),
            _ => default,
        }
    }

    /// A number key, or `default`.
    fn float(&self, key: &str, default: f64) -> f64 {
        match self.0.get(key) {
            Some(toml::Value::Float(f)) if *f != 0.0 => *f,
            Some(toml::Value::Integer(n)) if *n != 0 => *n as f64,
            _ => default,
        }
    }

    /// A true/false key, or `default`.
    fn bool(&self, key: &str, default: bool) -> bool {
        self.0.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_top_key_replaces_or_adds_and_keeps_the_rest() {
        let text = "# mine\nurl = \"http://x\"\nwallpaper = \"old.png\"\n\n[colors]\nwallpaper = \"no\"\n";
        let out = set_top_key(text, "wallpaper", "~/new.mpx");
        assert_eq!(out, "# mine\nurl = \"http://x\"\nwallpaper = \"~/new.mpx\"\n\n[colors]\nwallpaper = \"no\"\n");
        let added = set_top_key("url = \"http://x\"\n", "wallpaper", "a.png");
        assert_eq!(added, "wallpaper = \"a.png\"\nurl = \"http://x\"\n");
        // A key that only starts the same is left alone.
        assert!(set_top_key("wallpapers = 1\n", "wallpaper", "b").starts_with("wallpaper = \"b\"\n"));
    }

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
