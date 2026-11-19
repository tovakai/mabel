//! MABEL-OS colours in one place: amber phosphor on a beige 1980s terminal.
//!
//! The default set is the PC look. The "deck" preset pushes the ambers towards red for the 7"
//! panel on the Pi, which shows #ffb000 as plain yellow. Pick it in config.toml and nudge single
//! colours if needed (same keys as the Python client's palette.py):
//!
//! ```toml
//! palette = "deck"
//! [colors]
//! amber = "#ff7000"
//! ```

use ratatui::style::Color;
use toml::Table;

/// Every palette key, in the order the tuner lists them.
pub const KEYS: [&str; 15] = [
    "amber",
    "amber_hi",
    "amber_dim",
    "amber_faint",
    "beige",
    "beige_dark",
    "casing_text",
    "teal",
    "orange",
    "red",
    "sand",
    "mint",
    "bg",
    "panel",
    "rust",
];

const DEFAULT: &[(&str, &str)] = &[
    ("bg", "#0c0804"),
    ("panel", "#140e07"),
    ("amber", "#ffb000"),
    ("amber_hi", "#ffd066"),
    ("amber_dim", "#a86b00"),
    ("amber_faint", "#3d2a08"),
    ("beige", "#d9cba4"),
    ("beige_dark", "#8a7b58"),
    ("casing_text", "#2a2114"),
    ("teal", "#3fd6c6"),
    ("red", "#ff5a36"),
    ("orange", "#ff8a3d"),
    ("sand", "#e5b97a"),
    ("mint", "#9fd8a8"),
    ("rust", "#6b1d0a"),
];

/// The Pi's 7" panel: everything leans yellow, so the ambers lean red and the beiges lean pink.
/// Round 3 (2026-11-18): same hue as the PC, lifted green/blue = lighter, brighter oranges.
const DECK: &[(&str, &str)] = &[
    ("amber", "#ffa640"),
    ("amber_hi", "#ffd28a"),
    ("amber_dim", "#e88f38"),
    ("amber_faint", "#4a2e10"),
    ("beige", "#f2dcc2"),
    ("beige_dark", "#c4a482"),
    ("casing_text", "#2a1a10"),
    ("orange", "#ff8a50"),
    ("red", "#ff6050"),
    ("sand", "#f4c08e"),
];

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Palette {
    pub bg: Color,
    pub panel: Color,
    pub amber: Color,
    pub amber_hi: Color,
    pub amber_dim: Color,
    pub amber_faint: Color,
    pub beige: Color,
    pub beige_dark: Color,
    pub casing_text: Color,
    pub teal: Color,
    pub red: Color,
    pub orange: Color,
    pub sand: Color,
    pub mint: Color,
    pub rust: Color,
}

pub fn hex(value: &str) -> Option<Color> {
    let v = value.strip_prefix('#')?;
    if v.len() != 6 || !v.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let byte = |i: usize| u8::from_str_radix(&v[i..i + 2], 16).ok();
    Some(Color::Rgb(byte(0)?, byte(2)?, byte(4)?))
}

impl Palette {
    /// The PC look, before the config has its say.
    pub fn base() -> Self {
        let black = Color::Rgb(0, 0, 0);
        let mut p = Palette {
            bg: black,
            panel: black,
            amber: black,
            amber_hi: black,
            amber_dim: black,
            amber_faint: black,
            beige: black,
            beige_dark: black,
            casing_text: black,
            teal: black,
            red: black,
            orange: black,
            sand: black,
            mint: black,
            rust: black,
        };
        for (key, value) in DEFAULT.iter().copied() {
            p.set(key, value);
        }
        p
    }

    /// The preset named by `palette = "…"`, then each valid `[colors]` override.
    pub fn from_config(raw: &Table) -> Self {
        let mut p = Self::base();
        if raw.get("palette").and_then(|v| v.as_str()) == Some("deck") {
            for (key, value) in DECK.iter().copied() {
                p.set(key, value);
            }
        }
        if let Some(colors) = raw.get("colors").and_then(|v| v.as_table()) {
            for (key, value) in colors {
                if let Some(s) = value.as_str() {
                    p.set(key, s);
                }
            }
        }
        p
    }

    /// Set one colour by its key; false for an unknown key or a value that isn't `#rrggbb`.
    pub fn set(&mut self, key: &str, value: &str) -> bool {
        let Some(colour) = hex(value) else {
            return false;
        };
        match self.slot(key) {
            Some(slot) => {
                *slot = colour;
                true
            }
            None => false,
        }
    }

    pub fn get(&self, key: &str) -> Option<Color> {
        let mut copy = *self;
        copy.slot(key).map(|c| *c)
    }

    fn slot(&mut self, key: &str) -> Option<&mut Color> {
        Some(match key {
            "bg" => &mut self.bg,
            "panel" => &mut self.panel,
            "amber" => &mut self.amber,
            "amber_hi" => &mut self.amber_hi,
            "amber_dim" => &mut self.amber_dim,
            "amber_faint" => &mut self.amber_faint,
            "beige" => &mut self.beige,
            "beige_dark" => &mut self.beige_dark,
            "casing_text" => &mut self.casing_text,
            "teal" => &mut self.teal,
            "red" => &mut self.red,
            "orange" => &mut self.orange,
            "sand" => &mut self.sand,
            "mint" => &mut self.mint,
            "rust" => &mut self.rust,
            _ => return None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deck_preset_and_overrides() {
        let raw: Table =
            "palette = \"deck\"\n[colors]\namber = \"#ff7000\"\nteal = \"bad\"\nnope = \"#000000\"".parse().unwrap();
        let p = Palette::from_config(&raw);
        assert_eq!(p.amber, Color::Rgb(0xff, 0x70, 0x00));
        assert_eq!(p.amber_hi, hex("#ffd28a").unwrap());
        assert_eq!(p.teal, hex("#3fd6c6").unwrap());
        assert_eq!(p.get("bg"), hex("#0c0804"));
    }
}
