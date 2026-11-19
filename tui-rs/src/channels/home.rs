//! CH-01 HOME: the landing page. A wallpaper (a PIXEL .mpx or a .png, e.g. exported from
//! Aseprite) centred on an empty screen, animated if it has frames. Room for widgets later.
//!
//! The picture is `wallpaper` in config.toml; PIXEL's `:wallpaper` sets it to the open picture.
//! While HOME is on screen it notices a new setting or a re-saved picture within a couple of
//! seconds, so editing the wallpaper in PIXEL shows up here by itself.

use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime};

use crossterm::event::KeyEvent;
use mabel_media::pixel::{self, Doc, Grid};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::text::Line;
use ratatui::widgets::Paragraph;

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;

const ID: &str = "home";
/// How often (while on screen) to look for a changed setting or picture.
const LOOK_EVERY: Duration = Duration::from_secs(2);

/// The home screen's state.
pub struct Home {
    /// The wallpaper setting as last read, and the picture it names (None: unset or unreadable).
    setting: String,
    picture: Option<Doc>,
    /// Why the picture couldn't be opened, shown instead of it.
    problem: Option<String>,
    /// When the picture file and config.toml were last changed, to notice new versions.
    picture_changed: Option<SystemTime>,
    config_changed: Option<SystemTime>,
    /// The animation: the frame on screen and when the next one is due.
    frame: usize,
    next: Option<Instant>,
    last_look: Option<Instant>,
}

/// When a file was last changed (None if it isn't there).
fn changed(path: &std::path::Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

impl Home {
    /// HOME with the wallpaper from the config (opened straight away).
    pub fn new(cfg: &crate::os::config::Config) -> Self {
        let mut home = Home {
            setting: String::new(),
            picture: None,
            problem: None,
            picture_changed: None,
            config_changed: changed(&crate::os::config::config_path()),
            frame: 0,
            next: None,
            last_look: None,
        };
        home.open(&cfg.wallpaper);
        home
    }

    /// The wallpaper file's full path.
    fn path(&self) -> PathBuf {
        crate::os::config::expand(&self.setting)
    }

    /// Open the picture `setting` names (or none, for "").
    fn open(&mut self, setting: &str) {
        self.setting = setting.to_string();
        self.picture = None;
        self.problem = None;
        self.frame = 0;
        self.next = None;
        if setting.is_empty() {
            return;
        }
        let path = self.path();
        self.picture_changed = changed(&path);
        match pixel::open_any(&path) {
            Ok(doc) => {
                if doc.frames.len() > 1 {
                    self.next = Some(Instant::now() + Duration::from_millis(doc.frames[0].ms.max(20) as u64));
                }
                self.picture = Some(doc);
            }
            Err(e) => self.problem = Some(format!("{}: {e:#}", path.display())),
        }
    }

    /// Re-read the setting if config.toml changed, and the picture if its file changed.
    fn look(&mut self) {
        let config = crate::os::config::config_path();
        let config_now = changed(&config);
        if config_now != self.config_changed {
            self.config_changed = config_now;
            let setting = crate::os::config::load(&config).map(|c| c.wallpaper).unwrap_or_default();
            if setting != self.setting {
                self.open(&setting);
                return;
            }
        }
        if !self.setting.is_empty() && changed(&self.path()) != self.picture_changed {
            let setting = self.setting.clone();
            self.open(&setting);
        }
    }
}

/// Draw `grid` centred in `area` with half blocks (two pixels per cell, one above the other).
/// Pixel art keeps whole-number scaling (crisp pixels); a picture bigger than the screen is
/// shrunk to fit. See-through pixels show the background.
fn draw_picture(frame: &mut Frame, area: Rect, grid: &Grid, bg: Color) {
    let (w, h) = (grid.first().map_or(0, |r| r.len()), grid.len());
    if w == 0 || h == 0 || area.width == 0 || area.height == 0 {
        return;
    }
    let (room_w, room_h) = (area.width as f64, area.height as f64 * 2.0);
    let fit = (room_w / w as f64).min(room_h / h as f64);
    let scale = if fit >= 1.0 { fit.floor() } else { fit };
    let (out_w, out_h) = (((w as f64 * scale) as u16).max(1), ((h as f64 * scale) as usize).max(1));
    let x0 = area.x + (area.width - out_w.min(area.width)) / 2;
    let rows = out_h.div_ceil(2) as u16;
    let y0 = area.y + (area.height - rows.min(area.height)) / 2;
    let px = |x: u16, y: usize| -> Color {
        if y >= out_h {
            return bg;
        }
        let (sx, sy) = ((x as f64 / scale) as usize, (y as f64 / scale) as usize);
        match grid.get(sy.min(h - 1)).and_then(|r| r.get(sx.min(w - 1))).copied().flatten() {
            Some([r, g, b]) => Color::Rgb(r, g, b),
            None => bg,
        }
    };
    let buf = frame.buffer_mut();
    for row in 0..rows.min(area.height) {
        for x in 0..out_w.min(area.width) {
            let (top, bottom) = (px(x, row as usize * 2), px(x, row as usize * 2 + 1));
            if let Some(cell) = buf.cell_mut((x0 + x, y0 + row)) {
                cell.set_char('▀').set_style(Style::new().fg(top).bg(bottom));
            }
        }
    }
}

impl Channel for Home {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "HOME"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        self.advance(view.now);
        let pal = view.pal;
        match (&self.picture, &self.problem) {
            (Some(doc), _) => draw_picture(frame, area, &doc.composite(self.frame.min(doc.frames.len() - 1)), pal.bg),
            (None, Some(problem)) => {
                let text = vec![Line::raw("the wallpaper couldn't be opened"), Line::raw(problem.as_str())];
                let mid = Rect::new(area.x, area.y + area.height / 2, area.width, 2.min(area.height));
                frame.render_widget(Paragraph::new(text).centered().style(Style::new().fg(pal.amber_dim)), mid);
            }
            (None, None) => {
                let hint = "no wallpaper yet · in PIXEL, :wallpaper uses the open picture";
                let mid = Rect::new(area.x, area.y + area.height / 2, area.width, 1.min(area.height));
                frame.render_widget(Paragraph::new(hint).centered().style(Style::new().fg(pal.amber_faint)), mid);
            }
        }
        // One section, drawn without a border: the whole screen.
        vec![area]
    }

    /// r: open the wallpaper again now.
    fn nav_key(&mut self, key: KeyEvent, _ctx: &mut Ctx) -> Outcome {
        if keys::is(&key, 'r') {
            self.config_changed = None;
            self.picture_changed = None;
            self.look();
            return Outcome::Used;
        }
        Outcome::Ignored
    }

    fn next_wake(&self) -> Option<Instant> {
        self.next
    }

    /// While on screen: the next animation frame when it's due, and a look for changes.
    fn tick(&mut self, now: Instant, visible: bool, _ctx: &mut Ctx) {
        if !visible {
            return;
        }
        if self.last_look.is_none_or(|t| now.duration_since(t) >= LOOK_EVERY) {
            self.last_look = Some(now);
            self.look();
        }
        self.advance(now);
    }
}

impl Home {
    /// Move the animation on if its frame time is up (called from tick and before drawing).
    fn advance(&mut self, now: Instant) {
        let (Some(doc), Some(next)) = (&self.picture, self.next) else { return };
        if now < next {
            return;
        }
        self.frame = (self.frame + 1) % doc.frames.len();
        self.next = Some(now + Duration::from_millis(doc.frames[self.frame].ms.max(20) as u64));
    }
}
