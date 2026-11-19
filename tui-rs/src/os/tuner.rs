//! The colour tuner (ctrl+t): adjust the palette live and save it to this machine's config.
//!
//! Made for the deck's 7" panel, which shows colours its own way and has no settings of its own.
//! j/k pick a colour · h/l hue · - + lightness · [ ] or < > saturation · g the whole amber family
//! together · e type a hex · s save to config.toml `[colors]` · r back to the saved colours · esc close.

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;

use crate::os::keys;
use crate::os::palette::{KEYS, Palette, hex};
use crate::os::ui::bold;
use crate::widgets::input::{Input, InputResult};
use crate::widgets::popup::frame_box;

/// The ambers move together with g (the deck's panel shifts them all alike).
const FAMILY: [&str; 6] = ["amber", "amber_hi", "amber_dim", "amber_faint", "orange", "sand"];

fn usage(key: &str) -> &'static str {
    match key {
        "amber" => "most text",
        "amber_hi" => "titles, highlights",
        "amber_dim" => "secondary text, borders",
        "amber_faint" => "selection, faint lines",
        "beige" => "labels, the casing strip",
        "beige_dark" => "codes, hints",
        "casing_text" => "text on the casing",
        "teal" => "HUD accents",
        "orange" => "warnings, loose ends",
        "red" => "errors, things out",
        "sand" | "mint" => "map bins",
        "bg" => "background",
        "panel" => "panel background",
        "rust" => "status messages on the casing",
        _ => "",
    }
}

pub fn to_hex(c: Color) -> String {
    match c {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        _ => "#000000".into(),
    }
}

/// RGB → (hue, lightness, saturation), all 0..1 (Python's colorsys.rgb_to_hls).
fn to_hls(c: Color) -> (f64, f64, f64) {
    let Color::Rgb(r, g, b) = c else { return (0.0, 0.0, 0.0) };
    let (r, g, b) = (r as f64 / 255.0, g as f64 / 255.0, b as f64 / 255.0);
    let (max, min) = (r.max(g).max(b), r.min(g).min(b));
    let l = (max + min) / 2.0;
    if max == min {
        return (0.0, l, 0.0);
    }
    let d = max - min;
    let s = if l <= 0.5 { d / (max + min) } else { d / (2.0 - max - min) };
    let (rc, gc, bc) = ((max - r) / d, (max - g) / d, (max - b) / d);
    let h = if r == max {
        bc - gc
    } else if g == max {
        2.0 + rc - bc
    } else {
        4.0 + gc - rc
    };
    ((h / 6.0).rem_euclid(1.0), l, s)
}

fn from_hls(h: f64, l: f64, s: f64) -> Color {
    let (h, l, s) = (h.rem_euclid(1.0), l.clamp(0.0, 1.0), s.clamp(0.0, 1.0));
    let to = |v: f64| (v * 255.0).round().clamp(0.0, 255.0) as u8;
    if s == 0.0 {
        return Color::Rgb(to(l), to(l), to(l));
    }
    let m2 = if l <= 0.5 { l * (1.0 + s) } else { l + s - l * s };
    let m1 = 2.0 * l - m2;
    let v = |hue: f64| {
        let hue = hue.rem_euclid(1.0);
        if hue < 1.0 / 6.0 {
            m1 + (m2 - m1) * hue * 6.0
        } else if hue < 0.5 {
            m2
        } else if hue < 2.0 / 3.0 {
            m1 + (m2 - m1) * (2.0 / 3.0 - hue) * 6.0
        } else {
            m1
        }
    };
    Color::Rgb(to(v(h + 1.0 / 3.0)), to(v(h)), to(v(h - 1.0 / 3.0)))
}

fn nudge(c: Color, dh: f64, dl: f64, ds: f64) -> Color {
    let (h, l, s) = to_hls(c);
    from_hls(h + dh, l + dl, s + ds)
}

/// Write the colours that differ from the chosen preset as `[colors]` in config.toml (keeping
/// everything else in the file as it is).
pub fn save_colours(pal: &Palette, text: &str, preset: &Palette) -> String {
    let mut lines: Vec<&str> = vec![];
    let mut in_colors = false;
    for line in text.lines() {
        let t = line.trim();
        if t.starts_with('[') {
            in_colors = t == "[colors]";
        }
        if !in_colors && t != "# tuned with ctrl+t" {
            lines.push(line);
        }
    }
    let mut out = lines.join("\n").trim_end().to_string();
    let changed: Vec<String> = KEYS
        .iter()
        .filter(|k| pal.get(k) != preset.get(k))
        .map(|k| format!("{k} = \"{}\"", to_hex(pal.get(k).unwrap_or(Color::Black))))
        .collect();
    if !changed.is_empty() {
        out.push_str("\n\n# tuned with ctrl+t\n[colors]\n");
        out.push_str(&changed.join("\n"));
    }
    out.push('\n');
    out
}

pub enum Outcome {
    Open,
    Close,
    /// Save to config.toml.
    Save,
    /// Back to the colours the config has.
    Reset,
}

pub struct Tuner {
    row: usize,
    group: bool,
    pub dirty: bool,
    hex: Option<Input>,
    rect: Rect,
}

impl Tuner {
    pub fn new() -> Self {
        Tuner { row: 0, group: false, dirty: false, hex: None, rect: Rect::default() }
    }

    fn targets(&self) -> Vec<&'static str> {
        let key = KEYS[self.row];
        if self.group && FAMILY.contains(&key) { FAMILY.to_vec() } else { vec![key] }
    }

    fn adjust(&mut self, pal: &mut Palette, dh: f64, dl: f64, ds: f64) {
        for k in self.targets() {
            if let Some(c) = pal.get(k) {
                pal.set(k, &to_hex(nudge(c, dh, dl, ds)));
            }
        }
        self.dirty = true;
    }

    pub fn key(&mut self, key: &KeyEvent, pal: &mut Palette) -> Outcome {
        if let Some(input) = &mut self.hex {
            match key.code {
                KeyCode::Esc => self.hex = None,
                _ => {
                    if input.key(key) == InputResult::Submit {
                        let value = format!("#{}", input.value.trim().trim_start_matches('#').to_lowercase());
                        if hex(&value).is_some() {
                            pal.set(KEYS[self.row], &value);
                            self.dirty = true;
                        }
                        self.hex = None;
                    }
                }
            }
            return Outcome::Open;
        }
        match (keys::ch(key), key.code) {
            (_, KeyCode::Esc) | (Some('q'), _) => return Outcome::Close,
            (Some('j'), _) | (_, KeyCode::Down) => self.row = (self.row + 1) % KEYS.len(),
            (Some('k'), _) | (_, KeyCode::Up) => self.row = (self.row + KEYS.len() - 1) % KEYS.len(),
            (Some('h'), _) | (_, KeyCode::Left) => self.adjust(pal, -0.01, 0.0, 0.0),
            (Some('l'), _) | (_, KeyCode::Right) => self.adjust(pal, 0.01, 0.0, 0.0),
            (Some('-'), _) => self.adjust(pal, 0.0, -0.03, 0.0),
            (Some('+' | '='), _) => self.adjust(pal, 0.0, 0.03, 0.0),
            (Some('[' | '<'), _) => self.adjust(pal, 0.0, 0.0, -0.05),
            (Some(']' | '>'), _) => self.adjust(pal, 0.0, 0.0, 0.05),
            (Some('g'), _) => self.group = !self.group,
            (Some('e'), _) => {
                let mut input = Input::new("#rrggbb");
                input.set(&to_hex(pal.get(KEYS[self.row]).unwrap_or(Color::Black)));
                self.hex = Some(input);
            }
            (Some('s'), _) => {
                self.dirty = false;
                return Outcome::Save;
            }
            (Some('r'), _) => {
                self.dirty = false;
                return Outcome::Reset;
            }
            _ => {}
        }
        Outcome::Open
    }

    /// A click picks the colour on that row.
    pub fn click(&mut self, y: u16) {
        let first = self.rect.y + 2;
        if y >= first && ((y - first) as usize) < KEYS.len() {
            self.row = (y - first) as usize;
        }
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        let h = KEYS.len() as u16 + 6;
        let inner = frame_box(frame, area, pal, "COLOUR TUNER", "s save · r reset · esc close", 96, h);
        self.rect = inner;
        let mut lines = vec![
            Line::styled(
                "j k pick · h l hue · - + lighter/darker · [ ] or < > saturation · g whole amber family · e hex",
                Style::new().fg(pal.beige_dark),
            ),
            Line::raw(""),
        ];
        let current = KEYS[self.row];
        for (i, key) in KEYS.iter().enumerate() {
            let value = pal.get(key).unwrap_or(Color::Black);
            let marked = i == self.row || (self.group && FAMILY.contains(key) && FAMILY.contains(&current));
            let editing = i == self.row && self.hex.is_some();
            let hex_text = if editing {
                format!("{}█", self.hex.as_ref().map_or("", |h| h.value.as_str()))
            } else {
                to_hex(value)
            };
            lines.push(Line::from(vec![
                Span::styled(
                    if i == self.row {
                        "▶ "
                    } else if marked {
                        "· "
                    } else {
                        "  "
                    },
                    Style::new().fg(pal.amber_hi),
                ),
                Span::styled("      ", Style::new().bg(value)),
                Span::styled(
                    format!("  {key:<12} {hex_text:<8}  "),
                    if marked { bold(pal.amber_hi) } else { Style::new().fg(pal.amber) },
                ),
                Span::styled("Sample text", Style::new().fg(value).bg(pal.bg)),
                Span::styled(format!("  {}", usage(key)), Style::new().fg(pal.beige_dark)),
            ]));
        }
        lines.push(Line::raw(""));
        let mut foot = vec![Span::styled(
            if self.group { "GROUP: the amber family moves together" } else { "one colour at a time" },
            Style::new().fg(pal.teal),
        )];
        if self.dirty {
            foot.push(Span::styled("   ·   unsaved changes", Style::new().fg(pal.orange)));
        }
        lines.push(Line::from(foot));
        frame.render_widget(Paragraph::new(lines), inner);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hls_round_trip_and_nudges() {
        let amber = hex("#ffb000").unwrap();
        assert_eq!(to_hex(nudge(amber, 0.0, 0.0, 0.0)), "#ffb000");
        let (h, l, s) = to_hls(amber);
        assert!((h - 0.115).abs() < 0.01 && (l - 0.5).abs() < 0.01 && (s - 1.0).abs() < 0.01);
        assert_eq!(to_hex(nudge(amber, 0.0, 0.5, 0.0)), "#ffffff");
    }

    #[test]
    fn saving_keeps_the_file_and_writes_only_changes() {
        let raw: toml::Table = "url = \"u\"\npalette = \"deck\"\n".parse().unwrap();
        let preset = Palette::from_config(&raw);
        let mut pal = preset;
        pal.set("teal", "#00ff00");
        let old = "url = \"u\"\npalette = \"deck\"\n\n# tuned with ctrl+t\n[colors]\namber = \"#123456\"\n\n[navidrome]\nuser = \"m\"\n";
        let new = save_colours(&pal, old, &preset);
        assert!(new.contains("[navidrome]\nuser = \"m\""));
        assert!(new.contains("[colors]\nteal = \"#00ff00\"\n"));
        assert!(!new.contains("#123456"));
        assert_eq!(new.matches("[colors]").count(), 1);
        let parsed: toml::Table = new.parse().unwrap();
        assert_eq!(parsed["colors"]["teal"].as_str(), Some("#00ff00"));
    }
}
