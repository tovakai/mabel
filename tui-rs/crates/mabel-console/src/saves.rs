//! `save` and `load`: a small notebook of keys and values per tape (high scores, the level
//! you reached, settings). One JSON file per tape in `~/.local/share/mabel-os/saves/`, shared
//! by TAPES and the standalone build of the same tape.

use std::collections::BTreeMap;
use std::path::PathBuf;

/// Everything a tape saves together stays under this.
pub const MAX_BYTES: usize = 64 * 1024;

/// `~/.local/share/mabel-os`.
pub fn data_dir() -> PathBuf {
    let base = match std::env::var_os("XDG_DATA_HOME") {
        Some(d) if !d.is_empty() => PathBuf::from(d),
        _ => std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/")).join(".local/share"),
    };
    base.join("mabel-os")
}

pub struct Saves {
    path: PathBuf,
    data: BTreeMap<String, String>,
}

impl Saves {
    /// The saves of the tape called `key` (its folder name).
    pub fn open(key: &str) -> Saves {
        let safe: String =
            key.chars().map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' }).collect();
        let path = data_dir().join("saves").join(format!("{safe}.json"));
        let data = std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        Saves { path, data }
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.data.get(key).map(String::as_str)
    }

    /// Remember `value` under `key` (written to disk straight away).
    pub fn set(&mut self, key: &str, value: &str) -> Result<(), String> {
        let old = self.data.insert(key.to_string(), value.to_string());
        let text = serde_json::to_string_pretty(&self.data).map_err(|e| e.to_string())?;
        if text.len() > MAX_BYTES {
            match old {
                Some(v) => self.data.insert(key.to_string(), v),
                None => self.data.remove(key),
            };
            return Err(format!("save: a tape's saves are at most {} KB", MAX_BYTES / 1024));
        }
        if let Some(dir) = self.path.parent() {
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, text).and_then(|_| std::fs::rename(&tmp, &self.path)).map_err(|e| format!("save: {e}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saves_survive_a_restart_and_have_a_limit() {
        let dir = std::env::temp_dir().join(format!("mabel-saves-{}", std::process::id()));
        // SAFETY (edition 2024): tests in this crate don't read XDG_DATA_HOME concurrently.
        unsafe { std::env::set_var("XDG_DATA_HOME", &dir) };
        let mut s = Saves::open("snake");
        assert_eq!(s.get("best"), None);
        s.set("best", "120").unwrap();
        assert_eq!(Saves::open("snake").get("best"), Some("120"));
        assert!(s.set("huge", &"x".repeat(MAX_BYTES)).is_err());
        assert_eq!(Saves::open("snake").get("huge"), None);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
