//! `tape.toml`: what a DreamTape says about itself.
//!
//! ```toml
//! title = "Snake"
//! author = "tovakai"
//! description = "Eat, grow, don't bite yourself."
//! cover = "art/cover.mpx"   # optional: the LIBRARY card's picture
//! screen = "fill"           # or "128x72": a fixed size, scaled up where it fits
//! fps = 30
//!
//! [keys]                    # optional: replace a button's keys
//! a = ["space", "z"]
//! ```

use std::collections::BTreeMap;
use std::path::Path;

use serde::Deserialize;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Default)]
pub enum Screen {
    /// Every cell below the casing bar.
    #[default]
    #[serde(rename = "fill")]
    Fill,
    /// 128×72 pixels, centred, scaled by whole numbers where it fits.
    #[serde(rename = "128x72")]
    Fixed,
}

pub const FIXED: (usize, usize) = (128, 72);

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct TapeInfo {
    pub title: String,
    pub author: String,
    pub description: String,
    pub cover: Option<String>,
    pub screen: Screen,
    pub fps: u32,
    pub keys: BTreeMap<String, Vec<String>>,
}

impl Default for TapeInfo {
    fn default() -> Self {
        TapeInfo {
            title: String::new(),
            author: String::new(),
            description: String::new(),
            cover: None,
            screen: Screen::Fill,
            fps: 30,
            keys: BTreeMap::new(),
        }
    }
}

impl TapeInfo {
    pub fn parse(text: &str) -> Result<TapeInfo, String> {
        let mut info: TapeInfo = toml::from_str(text).map_err(|e| format!("tape.toml: {e}"))?;
        info.fps = info.fps.clamp(1, 60);
        Ok(info)
    }

    /// `dir/tape.toml`; a missing file is a tape with defaults named after the folder.
    pub fn read(dir: &Path) -> Result<TapeInfo, String> {
        let mut info = match std::fs::read_to_string(dir.join("tape.toml")) {
            Ok(text) => TapeInfo::parse(&text)?,
            Err(_) => TapeInfo::default(),
        };
        if info.title.is_empty() {
            info.title = dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        }
        Ok(info)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_with_defaults() {
        let t =
            TapeInfo::parse("title = \"Snake\"\nscreen = \"128x72\"\nfps = 200\n[keys]\na = [\"space\"]\n").unwrap();
        assert_eq!(t.title, "Snake");
        assert_eq!(t.screen, Screen::Fixed);
        assert_eq!(t.fps, 60);
        assert_eq!(t.keys["a"], vec!["space"]);
        assert_eq!(TapeInfo::parse("").unwrap().screen, Screen::Fill);
        assert!(TapeInfo::parse("screen = \"huge\"").is_err());
    }
}
