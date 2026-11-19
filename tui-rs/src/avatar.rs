//! Mabel's pixel avatar, drawn exactly: one terminal cell = 1×2 pixels (▀ with the top pixel as
//! the foreground colour and the bottom one as background), so a 28×28 sprite is 28×14 cells.
//!
//! States (idle, talk, …) are horizontal PNG strips of square frames, listed in avatar.json with
//! per-frame timings in ms (as set in Aseprite). `avatar` in config.toml may point at another
//! such folder, or at a single PNG (a still, or one strip used for every state). A state without
//! a strip falls back to idle. Transparent pixels show the panel. The built-in avatar is
//! compiled into the binary.

use std::collections::HashMap;
use std::path::Path;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, anyhow};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;

type Pixel = Option<(u8, u8, u8)>;
type Frame = Vec<Vec<Pixel>>;

const BUILTIN_JSON: &str = include_str!("../assets/avatar/avatar.json");
const BUILTIN: &[(&str, &[u8])] = &[
    ("idle.png", include_bytes!("../assets/avatar/idle.png")),
    ("talk.png", include_bytes!("../assets/avatar/talk.png")),
    ("still.png", include_bytes!("../assets/avatar/still.png")),
];

struct Animation {
    frames: Vec<Frame>,
    ms: Vec<u64>,
    /// The first frame is held this long (a random pick in the range) before each loop.
    hold: Option<(u64, u64)>,
}

impl Animation {
    fn duration(&self, i: usize) -> Duration {
        let base = self.ms.get(i).or(self.ms.last()).copied().unwrap_or(250);
        let ms = match (i, self.hold) {
            (0, Some((lo, hi))) => base.max(lo + pseudo_random() % (hi.saturating_sub(lo) + 1)),
            _ => base,
        };
        Duration::from_millis(ms)
    }
}

/// Good enough for blink timing, without a dependency.
fn pseudo_random() -> u64 {
    let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.subsec_nanos());
    (nanos as u64).wrapping_mul(6364136223846793005).rotate_left(17)
}

pub struct Avatar {
    animations: HashMap<String, Animation>,
    state: String,
    frame: usize,
    next: Option<Instant>,
}

fn decode(bytes: &[u8]) -> Result<Vec<Frame>> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(bytes));
    decoder.set_transformations(png::Transformations::normalize_to_color8() | png::Transformations::ALPHA);
    let mut reader = decoder.read_info()?;
    let mut buf = vec![0; reader.output_buffer_size().ok_or_else(|| anyhow!("png too large"))?];
    let info = reader.next_frame(&mut buf)?;
    let (w, h) = (info.width as usize, info.height as usize);
    let channels = info.line_size / w.max(1);
    let px = |x: usize, y: usize| -> Pixel {
        let i = y * info.line_size + x * channels;
        let (r, g, b, a) = match channels {
            4 => (buf[i], buf[i + 1], buf[i + 2], buf[i + 3]),
            2 => (buf[i], buf[i], buf[i], buf[i + 1]),
            _ => return None,
        };
        (a >= 128).then_some((r, g, b))
    };
    // A strip of square frames if the width divides evenly; else one frame.
    let count = if h > 0 && w % h == 0 { (w / h).max(1) } else { 1 };
    let fw = if count > 1 { h } else { w };
    Ok((0..count).map(|f| (0..h).map(|y| (0..fw).map(|x| px(f * fw + x, y)).collect()).collect()).collect())
}

impl Avatar {
    /// The avatar named in the config ("" = built in). Falls back to the built-in one on errors.
    pub fn load(path: &str, fps: f64) -> Self {
        let animations = if path.is_empty() { None } else { Self::from_path(&crate::config::expand(path), fps).ok() };
        let animations = animations.unwrap_or_else(|| Self::builtin().expect("the built-in avatar decodes"));
        Avatar { animations, state: "idle".into(), frame: 0, next: None }
    }

    fn builtin() -> Result<HashMap<String, Animation>> {
        Self::from_manifest(BUILTIN_JSON, |name| {
            BUILTIN.iter().find(|(n, _)| *n == name).map(|(_, b)| b.to_vec()).ok_or_else(|| anyhow!("no {name}"))
        })
    }

    fn from_path(path: &Path, fps: f64) -> Result<HashMap<String, Animation>> {
        if path.is_file() {
            let frames = decode(&std::fs::read(path)?)?;
            let ms = if fps > 0.0 { vec![(1000.0 / fps) as u64] } else { vec![] };
            return Ok(HashMap::from([("idle".to_string(), Animation { frames, ms, hold: None })]));
        }
        let manifest = std::fs::read_to_string(path.join("avatar.json")).context("avatar.json")?;
        Self::from_manifest(&manifest, |name| Ok(std::fs::read(path.join(name))?))
    }

    fn from_manifest(json: &str, read: impl Fn(&str) -> Result<Vec<u8>>) -> Result<HashMap<String, Animation>> {
        let manifest: serde_json::Value = serde_json::from_str(json)?;
        let mut states = HashMap::new();
        for (name, spec) in manifest.as_object().into_iter().flatten() {
            let Some(file) = spec["file"].as_str() else { continue };
            let ms = spec["ms"].as_array().map(|a| a.iter().filter_map(|m| m.as_u64()).collect()).unwrap_or_default();
            let hold = spec["hold"].as_array().and_then(|h| Some((h.first()?.as_u64()?, h.get(1)?.as_u64()?)));
            states.insert(name.clone(), Animation { frames: decode(&read(file)?)?, ms, hold });
        }
        if !states.contains_key("idle") {
            let frames = match read("still.png") {
                Ok(bytes) => decode(&bytes)?,
                Err(_) => states.values().next().ok_or_else(|| anyhow!("no states"))?.frames.clone(),
            };
            states.insert("idle".into(), Animation { frames, ms: vec![], hold: None });
        }
        Ok(states)
    }

    fn animation(&self) -> &Animation {
        self.animations.get(&self.state).unwrap_or(&self.animations["idle"])
    }

    /// Switch animation (idle, talk, …).
    pub fn set_state(&mut self, state: &str) {
        if state != self.state {
            self.state = state.into();
            self.frame = 0;
            self.next = None;
        }
    }

    /// Advance frames whose time is up; returns when the next frame is due.
    pub fn advance(&mut self, now: Instant) -> Option<Instant> {
        let count = self.animation().frames.len();
        if count < 2 {
            return None;
        }
        match self.next {
            None => self.next = Some(now + self.animation().duration(self.frame)),
            Some(due) if now >= due => {
                self.frame = (self.frame + 1) % count;
                self.next = Some(now + self.animation().duration(self.frame));
            }
            _ => {}
        }
        self.next
    }

    /// When the next frame is due (None for a still).
    pub fn next_due(&self) -> Option<Instant> {
        self.next
    }

    pub fn size(&self) -> (u16, u16) {
        let f = &self.animation().frames[0];
        (f.first().map_or(0, |r| r.len()) as u16, f.len().div_ceil(2) as u16)
    }

    /// Draw the current frame at the top left of `area` (cut to fit).
    pub fn render(&self, area: Rect, buf: &mut Buffer) {
        let anim = self.animation();
        let frame = &anim.frames[self.frame % anim.frames.len()];
        let rgb = |p: (u8, u8, u8)| Color::Rgb(p.0, p.1, p.2);
        for (row, y) in (0..frame.len()).step_by(2).enumerate() {
            if row as u16 >= area.height {
                break;
            }
            let top = &frame[y];
            let empty = vec![None; top.len()];
            let bottom = frame.get(y + 1).unwrap_or(&empty);
            for (x, (up, down)) in top.iter().zip(bottom).enumerate() {
                if x as u16 >= area.width {
                    break;
                }
                let cell = &mut buf[(area.x + x as u16, area.y + row as u16)];
                match (up, down) {
                    (None, None) => {}
                    (None, Some(d)) => {
                        cell.set_symbol("▄").set_fg(rgb(*d));
                    }
                    (Some(u), Some(d)) => {
                        cell.set_symbol("▀").set_fg(rgb(*u)).set_bg(rgb(*d));
                    }
                    (Some(u), None) => {
                        cell.set_symbol("▀").set_fg(rgb(*u));
                    }
                }
            }
        }
    }
}

/// This machine's editable copy of the avatar (PIXEL's :avatar), next to MABEL-OS's other data.
pub fn user_dir() -> std::path::PathBuf {
    let base = match std::env::var_os("XDG_DATA_HOME") {
        Some(d) if !d.is_empty() => std::path::PathBuf::from(d),
        _ => crate::config::home().join(".local/share"),
    };
    base.join("mabel-os").join("avatar")
}

/// Make `dir` hold an avatar to edit: if it has none yet, the built-in one is copied there.
pub fn ensure_user_copy(dir: &Path) -> Result<()> {
    if dir.join("avatar.json").exists() {
        return Ok(());
    }
    std::fs::create_dir_all(dir)?;
    std::fs::write(dir.join("avatar.json"), BUILTIN_JSON)?;
    for (name, bytes) in BUILTIN {
        std::fs::write(dir.join(name), bytes)?;
    }
    Ok(())
}

/// The built-in avatar's still frame as half-block lines: SYS STATUS's logo.
pub fn still_lines() -> Vec<ratatui::text::Line<'static>> {
    use ratatui::style::Style;
    use ratatui::text::{Line, Span};
    let Some((_, bytes)) = BUILTIN.iter().find(|(n, _)| *n == "still.png") else { return vec![] };
    let Ok(frames) = decode(bytes) else { return vec![] };
    let Some(frame) = frames.first() else { return vec![] };
    let rgb = |p: (u8, u8, u8)| Color::Rgb(p.0, p.1, p.2);
    (0..frame.len())
        .step_by(2)
        .map(|y| {
            let empty = vec![None; frame[y].len()];
            let bottom = frame.get(y + 1).unwrap_or(&empty);
            Line::from(
                frame[y]
                    .iter()
                    .zip(bottom)
                    .map(|(up, down)| match (up, down) {
                        (None, None) => Span::raw(" "),
                        (None, Some(d)) => Span::styled("▄", Style::new().fg(rgb(*d))),
                        (Some(u), Some(d)) => Span::styled("▀", Style::new().fg(rgb(*u)).bg(rgb(*d))),
                        (Some(u), None) => Span::styled("▀", Style::new().fg(rgb(*u))),
                    })
                    .collect::<Vec<_>>(),
            )
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builtin_avatar_has_idle_and_talk_strips() {
        let a = Avatar::load("", 4.0);
        assert_eq!(a.size(), (28, 14));
        assert_eq!(a.animations["idle"].frames.len(), 11);
        assert!(a.animations["idle"].hold.is_some());
        assert!(a.animations.contains_key("talk"));
    }
}
