//! CREATIVE ▸ PIXEL: a keyboard-first sprite editor. The model is pixel.rs.
//!
//! Left: TOOLS (with fg/bg and toggles) and PALETTE. Centre: the CANVAS. Right: PREVIEW (actual
//! size, plays the animation), LAYERS and FRAMES. Everything has a key on the canvas, so you rarely
//! leave it; rarer things are `:` commands (vim-like). Zoom 1 is exact half-blocks (1×2 pixels per
//! cell, like Mabel's avatar); zoom 2+ draws square pixels (2n×n cells) with a cursor box.
//!
//! Canvas keys: hjkl/arrows move (HJKL ×4) · space/enter use the tool · t pen down (draw while
//! moving) · b pencil e eraser f fill n line r rect o ellipse c pick · F filled shapes · m mirror ·
//! g grid · O onion skin · + - zoom (0 fits) · P palette · , . or [ ] colour · x swap fg/bg ·
//! # hex colour · PgUp/PgDn or { } layer · A add layer · V hide layer · < > frame · N new frame ·
//! p play · u undo · ctrl+r redo · ctrl+s save · : command · ctrl+o files.

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, PanelState, bold};
use crate::pixel::{self as px, Doc, Grid, History, Px};
use crate::widgets::input::{Input, InputResult};

const ID: &str = "pixel";
const CANVAS: usize = 0;
const PALETTE: usize = 1;
const SWATCHES_PER_ROW: usize = 8;
const CHECKER: [[u8; 3]; 2] = [[0x1b, 0x15, 0x10], [0x25, 0x1d, 0x15]];

const COMMANDS: &str = ":new W H [palette] · :w [file] · :e file · :avatar · :e! / :new! (drop changes) · \
:export png|strip|sheet [scale] [file] [columns] · :palette NAME (pico8 db16 db32 gameboy grayscale phosphor) · \
:flip h|v · :rotate cw|ccw · :resize W H · :ms N · :ms all N · :tag NAME FROM TO · :untag NAME · \
:layer name NAME · :layer del · :layer up|down · :frame del · :frame left|right · :hex #rrggbb";

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Tool {
    Pencil,
    Eraser,
    Fill,
    Line,
    Rect,
    Ellipse,
    Pick,
}

const TOOLS: [(char, Tool, &str); 7] = [
    ('b', Tool::Pencil, "pencil"),
    ('e', Tool::Eraser, "eraser"),
    ('f', Tool::Fill, "fill"),
    ('n', Tool::Line, "line"),
    ('r', Tool::Rect, "rect"),
    ('o', Tool::Ellipse, "ellipse"),
    ('c', Tool::Pick, "pick"),
];

fn tool_name(t: Tool) -> &'static str {
    TOOLS.iter().find(|(_, x, _)| *x == t).map_or("?", |(_, _, n)| n)
}

fn rgb(c: [u8; 3]) -> Color {
    Color::Rgb(c[0], c[1], c[2])
}

/// Black or white, whichever reads on `c`.
fn contrast(c: [u8; 3]) -> Color {
    let lum = 0.299 * c[0] as f64 + 0.587 * c[1] as f64 + 0.114 * c[2] as f64;
    if lum > 140.0 { Color::Black } else { Color::White }
}

fn mix(a: [u8; 3], b: [u8; 3], amount: f64) -> [u8; 3] {
    let m = |x: u8, y: u8| (x as f64 + (y as f64 - x as f64) * amount) as u8;
    [m(a[0], b[0]), m(a[1], b[1]), m(a[2], b[2])]
}

fn checker(x: usize, y: usize) -> [u8; 3] {
    CHECKER[(x / 2 + y / 2) % 2]
}

/// Where Mabel's avatar is edited: the configured folder, else this machine's copy (made from
/// the built-in one the first time; COMMS shows it from then on).
pub fn avatar_dir(configured: &str) -> PathBuf {
    let c = crate::os::config::expand(configured);
    if !configured.is_empty() && c.is_dir() {
        return c;
    }
    crate::os::avatar::user_dir()
}

/// The browser's preview of a sprite: its first frame (half-blocks, scaled to fit) and its facts.
pub fn thumbnail(path: &Path, width: u16, height: u16, pal: &Palette) -> Vec<Line<'static>> {
    let Ok(doc) = px::open_any(path) else { return vec![Line::styled("can't read it", Style::new().fg(pal.red))] };
    let grid = doc.composite(0);
    let (room_w, room_h) = ((width as usize).saturating_sub(2).max(4), (height as usize).saturating_sub(7).max(2));
    let mut out = vec![Line::raw("")];
    if doc.w * 2 <= room_w && doc.h <= room_h {
        // Square pixels: 2 cells × 1 row each (bigger while it fits).
        let scale = (room_w / (doc.w * 2)).min(room_h / doc.h).clamp(1, 4);
        for (y, row) in grid.iter().enumerate() {
            for _ in 0..scale {
                let mut spans = vec![Span::raw(" ")];
                for (x, c) in row.iter().enumerate() {
                    spans.push(Span::styled("  ".repeat(scale), Style::new().bg(rgb(c.unwrap_or(checker(x, y))))));
                }
                out.push(Line::from(spans));
            }
        }
    } else {
        let step = doc.w.div_ceil(room_w).max(doc.h.div_ceil(room_h * 2)).max(1);
        let rows: Vec<usize> = (0..doc.h).step_by(step).collect();
        let cols: Vec<usize> = (0..doc.w).step_by(step).collect();
        for pair in rows.chunks(2) {
            let mut spans = vec![Span::raw(" ")];
            for &x in &cols {
                let top = grid[pair[0]][x].unwrap_or(checker(x, pair[0]));
                let bottom = pair.get(1).map(|&y1| grid[y1][x].unwrap_or(checker(x, y1)));
                let style = Style::new().fg(rgb(top)).bg(bottom.map_or(pal.panel, rgb));
                spans.push(Span::styled("▀", style));
            }
            out.push(Line::from(spans));
        }
    }
    let plural = |n: usize| if n == 1 { "" } else { "s" };
    out.push(Line::raw(""));
    out.push(Line::styled(
        format!(
            " {}×{} · {} frame{} · {} layer{}",
            doc.w,
            doc.h,
            doc.frames.len(),
            plural(doc.frames.len()),
            doc.layers.len(),
            plural(doc.layers.len())
        ),
        Style::new().fg(pal.amber),
    ));
    if !doc.tags.is_empty() {
        let tags: Vec<String> = doc.tags.iter().map(|t| format!("{} {}–{}", t.name, t.start + 1, t.end + 1)).collect();
        out.push(Line::styled(format!(" {}", tags.join(" · ")), Style::new().fg(pal.teal)));
    }
    let kind = match doc.kind {
        px::Kind::Mpx => "mpx",
        px::Kind::Png => "png",
        px::Kind::Avatar => "avatar",
    };
    out.push(Line::styled(format!(" {} colours · {kind}", doc.palette.len()), Style::new().fg(pal.amber_dim)));
    out
}

pub struct PixelView {
    pub doc: Doc,
    hist: History,
    cx: i64,
    cy: i64,
    tool: Tool,
    prev_tool: Tool,
    fg: Px,
    bg: Px,
    zoom: i64,
    layer: usize,
    frame: usize,
    filled: bool,
    mirror: bool,
    grid: bool,
    onion: bool,
    anchor: Option<(i64, i64)>,
    pen: bool,
    playing: Option<(Vec<usize>, usize, Instant)>,
    pub dirty: bool,
    message: String,
    cmdline: Option<Input>,
    palette_at: usize,
    /// The canvas's scroll, in pixels, and where it was drawn.
    ox: i64,
    oy: i64,
    canvas_rect: Rect,
    palette_rect: Rect,
    dir: PathBuf,
    avatar: String,
    /// The file browser (ctrl+o).
    browser: Option<crate::channels::creative::browser::Browser>,
    loaded_last: bool,
}

impl PixelView {
    pub fn new(cfg: &crate::os::config::Config) -> Self {
        let doc = Doc::new(32, 32, "db32");
        let fg = doc.palette.get(4).copied();
        PixelView {
            doc,
            hist: History::default(),
            cx: 0,
            cy: 0,
            tool: Tool::Pencil,
            prev_tool: Tool::Pencil,
            fg,
            bg: None,
            zoom: 3,
            layer: 0,
            frame: 0,
            filled: false,
            mirror: false,
            grid: true,
            onion: false,
            anchor: None,
            pen: false,
            playing: None,
            dirty: false,
            message: String::new(),
            cmdline: None,
            palette_at: 0,
            ox: 0,
            oy: 0,
            canvas_rect: Rect::default(),
            palette_rect: Rect::default(),
            dir: crate::os::config::expand(&cfg.pixel_dir),
            avatar: cfg.avatar.clone(),
            browser: None,
            loaded_last: false,
        }
    }

    fn last_file() -> PathBuf {
        crate::os::config::cache_dir().join("pixel-last")
    }

    fn remember(&self) {
        if let Some(p) = &self.doc.path {
            let _ = std::fs::create_dir_all(crate::os::config::cache_dir());
            let _ = std::fs::write(Self::last_file(), p.display().to_string());
        }
    }

    fn path(&self, name: &str) -> PathBuf {
        let p = crate::os::config::expand(name);
        if p.is_absolute() { p } else { self.dir.join(p) }
    }

    pub fn open(&mut self, path: &Path) -> anyhow::Result<()> {
        self.doc = px::open_any(path)?;
        self.hist = History::default();
        (self.frame, self.layer) = (0, 0);
        (self.anchor, self.pen, self.dirty) = (None, false, false);
        self.zoom = 0; // fit when drawn
        if self.fg.is_none_or(|c| !self.doc.palette.contains(&c)) {
            self.fg = self.doc.palette.first().copied();
        }
        self.remember();
        Ok(())
    }

    // ── what the canvas shows ────────────────────────────────────────────

    fn view_grid(&self) -> Grid {
        let mut grid = self.doc.composite(self.frame);
        if self.onion && self.frame > 0 {
            let ghost = self.doc.composite(self.frame - 1);
            for (y, row) in grid.iter_mut().enumerate() {
                for (x, c) in row.iter_mut().enumerate() {
                    if c.is_none() {
                        *c = ghost[y][x].map(|g| mix(g, [0x14, 0x0e, 0x07], 0.6));
                    }
                }
            }
        }
        if let Some((ax, ay)) = self.anchor {
            for (x, y) in self.shape(ax, ay, self.cx, self.cy) {
                for qx in if self.mirror { vec![x, self.doc.w as i64 - 1 - x] } else { vec![x] } {
                    if self.doc.inside(qx, y) {
                        grid[y as usize][qx as usize] = self.fg;
                    }
                }
            }
        }
        grid
    }

    fn shape(&self, x0: i64, y0: i64, x1: i64, y1: i64) -> Vec<(i64, i64)> {
        match self.tool {
            Tool::Line => px::line(x0, y0, x1, y1),
            Tool::Rect => px::rect(x0, y0, x1, y1, self.filled),
            _ => px::ellipse(x0, y0, x1, y1, self.filled),
        }
    }

    /// Cells per pixel: zoom 1 = 1 column × ½ row; zoom z ≥ 2 = 2(z−1) columns × (z−1) rows.
    fn cell(&self) -> (i64, i64) {
        if self.zoom <= 1 { (1, 0) } else { (2 * (self.zoom - 1), self.zoom - 1) }
    }

    /// The biggest zoom at which the whole sprite fits the canvas (1 = half-blocks if nothing does).
    fn fit_zoom(&mut self) {
        let (cw, ch) = (self.canvas_rect.width as i64, self.canvas_rect.height as i64);
        self.zoom = (2..=8)
            .rev()
            .find(|z| self.doc.w as i64 * 2 * (z - 1) <= cw && self.doc.h as i64 * (z - 1) <= ch)
            .unwrap_or(1);
    }

    // ── editing ──────────────────────────────────────────────────────────

    fn changed(&mut self, pixels: bool, say: &str) {
        if pixels {
            self.dirty = true;
        }
        self.message = say.to_string();
        self.cx = self.cx.min(self.doc.w as i64 - 1);
        self.cy = self.cy.min(self.doc.h as i64 - 1);
        self.layer = self.layer.min(self.doc.layers.len() - 1);
        self.frame = self.frame.min(self.doc.frames.len() - 1);
    }

    fn movement(&mut self, dx: i64, dy: i64) {
        let nx = (self.cx + dx).clamp(0, self.doc.w as i64 - 1);
        let ny = (self.cy + dy).clamp(0, self.doc.h as i64 - 1);
        if self.pen && matches!(self.tool, Tool::Pencil | Tool::Eraser) {
            return self.stroke_to(nx, ny, false);
        }
        (self.cx, self.cy) = (nx, ny);
        self.message.clear();
    }

    fn stroke_to(&mut self, x: i64, y: i64, use_bg: bool) {
        let color = if self.tool == Tool::Eraser {
            None
        } else if use_bg {
            self.bg
        } else {
            self.fg
        };
        self.doc.put(self.frame, self.layer, &px::line(self.cx, self.cy, x, y), color, self.mirror);
        (self.cx, self.cy) = (x, y);
        self.changed(true, "");
    }

    fn act(&mut self, use_bg: bool) {
        let color = if use_bg { self.bg } else { self.fg };
        let (x, y) = (self.cx, self.cy);
        match self.tool {
            Tool::Pick => {
                let picked = self.doc.composite(self.frame)[y as usize][x as usize];
                if use_bg {
                    self.bg = picked;
                } else {
                    self.fg = picked;
                }
                self.tool = self.prev_tool;
                let name = picked.map_or("transparent".into(), px::hex);
                self.changed(false, &format!("picked {name}"));
            }
            Tool::Line | Tool::Rect | Tool::Ellipse => match self.anchor {
                None => {
                    self.anchor = Some((x, y));
                    let msg = format!("{} from {x},{y}: move, then space · esc cancels", tool_name(self.tool));
                    self.changed(false, &msg);
                }
                Some((ax, ay)) => {
                    self.hist.push(&self.doc);
                    let pts = self.shape(ax, ay, x, y);
                    self.doc.put(self.frame, self.layer, &pts, color, self.mirror);
                    self.anchor = None;
                    self.changed(true, "");
                }
            },
            Tool::Fill => {
                self.hist.push(&self.doc);
                let area = px::flood(&self.doc.frames[self.frame].cels[self.layer], x, y);
                self.doc.put(self.frame, self.layer, &area, color, self.mirror);
                self.changed(true, "");
            }
            Tool::Pencil | Tool::Eraser => {
                self.hist.push(&self.doc);
                let c = if self.tool == Tool::Eraser { None } else { color };
                self.doc.put(self.frame, self.layer, &[(x, y)], c, self.mirror);
                self.changed(true, "");
            }
        }
    }

    /// esc: a shape, the pen or playback first; false if there was nothing to cancel.
    fn cancel(&mut self) -> bool {
        if self.anchor.is_some() || self.pen || self.playing.is_some() {
            (self.anchor, self.pen, self.playing) = (None, false, None);
            self.changed(false, "");
            return true;
        }
        false
    }

    fn toggle_pen(&mut self) {
        if !matches!(self.tool, Tool::Pencil | Tool::Eraser) {
            self.set_tool(Tool::Pencil);
        }
        self.pen = !self.pen;
        if self.pen {
            self.hist.push(&self.doc);
            let c = if self.tool == Tool::Eraser { None } else { self.fg };
            self.doc.put(self.frame, self.layer, &[(self.cx, self.cy)], c, self.mirror);
            self.changed(true, "pen down: moving draws · t or esc lifts it");
        } else {
            self.changed(false, "pen up");
        }
    }

    fn set_tool(&mut self, t: Tool) {
        if t == Tool::Pick && self.tool != Tool::Pick {
            self.prev_tool = self.tool;
        }
        (self.tool, self.anchor) = (t, None);
        if !matches!(t, Tool::Pencil | Tool::Eraser) {
            self.pen = false;
        }
        self.changed(false, "");
    }

    fn step_colour(&mut self, step: isize) {
        let pal = &self.doc.palette;
        if pal.is_empty() {
            return;
        }
        let i = self.fg.and_then(|c| pal.iter().position(|p| *p == c)).map_or(-1, |i| i as isize);
        self.fg = Some(pal[(i + step).rem_euclid(pal.len() as isize) as usize]);
    }

    fn undo(&mut self, back: bool) {
        if self.hist.step(&mut self.doc, back) {
            self.changed(true, if back { "undone" } else { "redone" });
        } else {
            self.changed(false, if back { "nothing to undo" } else { "nothing to redo" });
        }
    }

    fn toggle_play(&mut self) {
        if self.playing.take().is_some() {
            return;
        }
        let frames: Vec<usize> = match self.doc.tag_at(self.frame) {
            Some(tag) => {
                let mut f: Vec<usize> = (tag.start..=tag.end).collect();
                if tag.extra.get("direction").and_then(|d| d.as_str()) == Some("pingpong") && f.len() > 2 {
                    let back: Vec<usize> = f[1..f.len() - 1].iter().rev().copied().collect();
                    f.extend(back);
                }
                f
            }
            None => (0..self.doc.frames.len()).collect(),
        };
        self.playing = Some((frames, 0, Instant::now()));
    }

    /// The frame the preview shows now (advancing a playing animation).
    fn preview_frame(&mut self, now: Instant) -> usize {
        let Some((frames, i, since)) = &mut self.playing else { return self.frame };
        let f = frames[*i % frames.len()];
        let ms = self.doc.frames.get(f).map_or(100, |fr| fr.ms).max(20) as u64;
        if now.duration_since(*since) >= Duration::from_millis(ms) {
            *i += 1;
            *since = now;
        }
        frames[*i % frames.len()]
    }

    // ── keys ─────────────────────────────────────────────────────────────

    fn canvas_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        let step = if shift { 4 } else { 1 };
        if keys::ctrl(key, 'r') {
            self.undo(false);
            return Outcome::Used;
        }
        if keys::ctrl(key, 's') {
            self.command("w", ctx);
            return Outcome::Used;
        }
        if keys::ctrl(key, 'o') {
            self.browse();
            return Outcome::Used;
        }
        match (keys::ch(key), key.code) {
            (Some('h'), _) => self.movement(-1, 0),
            (Some('l'), _) => self.movement(1, 0),
            (Some('k'), _) => self.movement(0, -1),
            (Some('j'), _) => self.movement(0, 1),
            (Some('H'), _) => self.movement(-4, 0),
            (Some('L'), _) => self.movement(4, 0),
            (Some('K'), _) => self.movement(0, -4),
            (Some('J'), _) => self.movement(0, 4),
            (_, KeyCode::Left) => self.movement(-step, 0),
            (_, KeyCode::Right) => self.movement(step, 0),
            (_, KeyCode::Up) => self.movement(0, -step),
            (_, KeyCode::Down) => self.movement(0, step),
            (Some(' '), _) | (_, KeyCode::Enter) => self.act(false),
            (Some('t'), _) => self.toggle_pen(),
            (Some(c), _) if TOOLS.iter().any(|(k, _, _)| *k == c) => {
                let t = TOOLS.iter().find(|(k, _, _)| *k == c).map(|(_, t, _)| *t).unwrap_or(Tool::Pencil);
                self.set_tool(t);
            }
            (Some('F'), _) => {
                self.filled = !self.filled;
                self.changed(false, if self.filled { "filled on" } else { "filled off" });
            }
            (Some('m'), _) => {
                self.mirror = !self.mirror;
                self.changed(false, if self.mirror { "mirror on" } else { "mirror off" });
            }
            (Some('g'), _) => {
                self.grid = !self.grid;
                self.changed(false, if self.grid { "grid on" } else { "grid off" });
            }
            (Some('O'), _) => {
                self.onion = !self.onion;
                self.changed(false, if self.onion { "onion on" } else { "onion off" });
            }
            (Some('+' | '='), _) => {
                self.zoom = (self.zoom + 1).min(8);
                self.changed(false, &format!("zoom {}", self.zoom));
            }
            (Some('-'), _) => {
                self.zoom = (self.zoom - 1).max(1);
                self.changed(false, &format!("zoom {}", self.zoom));
            }
            (Some('0'), _) => {
                self.fit_zoom();
                self.changed(false, &format!("zoom {} (fits)", self.zoom));
            }
            (Some('[' | ','), _) => self.step_colour(-1),
            (Some(']' | '.'), _) => self.step_colour(1),
            (Some('x'), _) => std::mem::swap(&mut self.fg, &mut self.bg),
            (Some('#'), _) => self.open_cmdline("hex #"),
            (Some('{'), _) | (_, KeyCode::PageDown) => {
                self.layer = self.layer.saturating_sub(1);
                let say = format!("layer {}", self.doc.layers[self.layer].name);
                self.changed(false, &say);
            }
            (Some('}'), _) | (_, KeyCode::PageUp) => {
                self.layer = (self.layer + 1).min(self.doc.layers.len() - 1);
                let say = format!("layer {}", self.doc.layers[self.layer].name);
                self.changed(false, &say);
            }
            (Some('A'), _) => {
                self.hist.push(&self.doc);
                self.layer = self.doc.add_layer(self.layer);
                let say = format!("added {}", self.doc.layers[self.layer].name);
                self.changed(true, &say);
            }
            (Some('V'), _) => {
                let l = &mut self.doc.layers[self.layer];
                l.visible = !l.visible;
                let say = format!("{} {}", l.name, if l.visible { "shown" } else { "hidden" });
                self.changed(true, &say);
            }
            (Some('<'), _) => self.frame = (self.frame + self.doc.frames.len() - 1) % self.doc.frames.len(),
            (Some('>'), _) => self.frame = (self.frame + 1) % self.doc.frames.len(),
            (Some('N'), _) => {
                self.hist.push(&self.doc);
                self.frame = self.doc.add_frame(self.frame, true);
                let say = format!("frame {} (a copy)", self.frame + 1);
                self.changed(true, &say);
            }
            (Some('p'), _) => self.toggle_play(),
            (Some('P'), _) => {
                self.palette_at = self.fg.and_then(|c| self.doc.palette.iter().position(|p| *p == c)).unwrap_or(0);
                ctx.focus(PALETTE);
            }
            (Some('u'), _) => self.undo(true),
            (Some(':'), _) => self.open_cmdline(""),
            (_, KeyCode::Esc) => return if self.cancel() { Outcome::Used } else { Outcome::Ignored },
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn palette_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        let n = self.doc.palette.len();
        if n == 0 {
            return Outcome::Ignored;
        }
        let at = self.palette_at as isize;
        let go = |d: isize| (at + d).clamp(0, n as isize - 1) as usize;
        match (keys::ch(key), key.code) {
            (Some('h'), _) | (_, KeyCode::Left) => self.palette_at = go(-1),
            (Some('l'), _) | (_, KeyCode::Right) => self.palette_at = go(1),
            (Some('k'), _) | (_, KeyCode::Up) => self.palette_at = go(-(SWATCHES_PER_ROW as isize)),
            (Some('j'), _) | (_, KeyCode::Down) => self.palette_at = go(SWATCHES_PER_ROW as isize),
            (Some(' '), _) | (_, KeyCode::Enter) => {
                self.fg = Some(self.doc.palette[self.palette_at]);
                self.message = format!("FG {}", px::hex(self.doc.palette[self.palette_at]));
                ctx.focus(CANVAS);
            }
            (Some('b'), _) => {
                self.bg = Some(self.doc.palette[self.palette_at]);
                self.message = format!("BG {}", px::hex(self.doc.palette[self.palette_at]));
                ctx.focus(CANVAS);
            }
            (Some('x'), _) => {
                self.bg = None;
                self.message = "BG clear".into();
                ctx.focus(CANVAS);
            }
            (_, KeyCode::Esc) => ctx.focus(CANVAS),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn open_cmdline(&mut self, prefill: &str) {
        let mut input = Input::new(": command   (try :new 28 28, :avatar, :export strip 4)");
        input.set(prefill);
        self.cmdline = Some(input);
    }

    // ── files ────────────────────────────────────────────────────────────

    fn browse(&mut self) {
        let avatar = avatar_dir(&self.avatar);
        self.browser =
            Some(crate::channels::creative::browser::Browser::new(crate::channels::creative::browser::Spec {
                title: "PIXEL · FILES".into(),
                root: self.dir.clone(),
                suffixes: vec!["mpx".into(), "png".into()],
                also_show: vec![],
                document_dir: Some("avatar.json".into()),
                extras: if avatar.join("avatar.json").exists() {
                    vec![(avatar, "Mabel's avatar".into())]
                } else {
                    vec![]
                },
                new_suffix: "mpx".into(),
                dirty: self.dirty,
                current: self.doc.path.clone(),
                preview: crate::channels::creative::browser::Preview::Sprite,
            }));
    }

    fn browsed(&mut self, choice: crate::channels::creative::browser::Choice) {
        let result: anyhow::Result<String> = (|| {
            if choice.save_first {
                if self.doc.path.is_none() {
                    return Ok("this one has no name yet: :w name.mpx first".into());
                }
                px::save(&mut self.doc, None)?;
            }
            if choice.new {
                let pal = self.doc.palette.clone();
                let (w, h) = (self.doc.w, self.doc.h);
                self.doc = Doc::new(w as i64, h as i64, "db32");
                self.doc.palette = pal;
                px::save(&mut self.doc, Some(&choice.path))?;
                self.hist = History::default();
                (self.frame, self.layer, self.dirty, self.cx, self.cy) = (0, 0, false, 0, 0);
                self.remember();
                self.zoom = 0;
                return Ok(format!(
                    "new {} ({w}×{h}; :resize to change)",
                    choice.path.file_name().map_or(String::new(), |n| n.to_string_lossy().into_owned())
                ));
            }
            self.open(&choice.path)?;
            Ok(format!(
                "opened {}",
                choice.path.file_name().map_or(String::new(), |n| n.to_string_lossy().into_owned())
            ))
        })();
        self.message = result.unwrap_or_else(|e| format!("✖ {e}"));
    }

    fn command(&mut self, line: &str, ctx: &mut Ctx) {
        let args: Vec<&str> = line.trim().trim_start_matches(':').split_whitespace().collect();
        self.message = match self.run(&args, ctx) {
            Ok(m) => m,
            Err(e) => format!("✖ {e}"),
        };
    }

    fn run(&mut self, args: &[&str], ctx: &mut Ctx) -> anyhow::Result<String> {
        let Some((first, rest)) = args.split_first() else { return Ok(String::new()) };
        let force = first.ends_with('!');
        let cmd = first.trim_end_matches('!');
        let num = |i: usize| -> anyhow::Result<i64> {
            Ok(rest.get(i).ok_or_else(|| anyhow::anyhow!("a number is missing"))?.parse()?)
        };
        if matches!(cmd, "new" | "e" | "avatar") && self.dirty && !force && !(cmd == "e" && rest.is_empty()) {
            return Ok(format!("unsaved changes: :w first, or :{cmd}! to drop them"));
        }
        Ok(match cmd {
            "new" => {
                let (w, h) = if rest.len() >= 2 { (num(0)?, num(1)?) } else { (32, 32) };
                self.doc = Doc::new(w, h, rest.get(2).copied().unwrap_or("db32"));
                self.hist = History::default();
                (self.frame, self.layer, self.dirty, self.cx, self.cy) = (0, 0, false, 0, 0);
                self.fg = self.doc.palette.get(4.min(self.doc.palette.len().saturating_sub(1))).copied();
                self.zoom = 0;
                format!("new {}×{}", self.doc.w, self.doc.h)
            }
            "w" => {
                let target = rest.first().map(|n| self.path(n));
                if target.is_none() && self.doc.path.is_none() {
                    return Ok("name it: :w sprite.mpx (or .png)".into());
                }
                let p = px::save(&mut self.doc, target.as_deref())?;
                self.dirty = false;
                self.remember();
                format!("saved {}", p.display())
            }
            "files" | "browse" => {
                self.browse();
                String::new()
            }
            "e" if rest.is_empty() => {
                self.browse();
                String::new()
            }
            "e" => {
                let p = self.path(&rest.join(" "));
                self.open(&p)?;
                format!("opened {}", p.display())
            }
            "avatar" => {
                let dir = avatar_dir(&self.avatar);
                crate::os::avatar::ensure_user_copy(&dir)?;
                self.open(&dir)?;
                let tags: Vec<&str> = self.doc.tags.iter().map(|t| t.name.as_str()).collect();
                format!("Mabel's avatar ({}) · :w writes the strips back", tags.join(", "))
            }
            "export" => {
                let how = rest.first().copied().filter(|h| matches!(*h, "png" | "strip" | "sheet")).unwrap_or("png");
                let nums: Vec<usize> = rest.iter().skip(1).filter_map(|a| a.parse().ok()).collect();
                let names: Vec<&&str> = rest.iter().skip(1).filter(|a| a.parse::<usize>().is_err()).collect();
                let scale = nums.first().copied().unwrap_or(1);
                let columns = nums.get(1).copied().unwrap_or(4);
                let stem = self.doc.path.as_ref().map_or("sprite".into(), |p| {
                    p.file_stem().map_or("sprite".into(), |s| s.to_string_lossy().into_owned())
                });
                let path = match names.first() {
                    Some(n) => self.path(n),
                    None => self.dir.join(format!(
                        "{stem}-{how}{}.png",
                        if scale == 1 { String::new() } else { format!("@{scale}x") }
                    )),
                };
                let what = match how {
                    "strip" => px::Export::Strip,
                    "sheet" => px::Export::Sheet(columns),
                    _ => px::Export::Png(self.frame),
                };
                px::export(&self.doc, &path, what, scale)?;
                format!("exported {}", path.display())
            }
            "palette" => {
                let Some(name) = rest.first().filter(|n| px::PALETTES.iter().any(|(p, _)| p == *n)) else {
                    return Ok(format!(
                        "palettes: {}",
                        px::PALETTES.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(" ")
                    ));
                };
                self.hist.push(&self.doc);
                self.doc.palette = px::palette(name);
                self.doc.adopt_colors();
                self.dirty = true;
                format!("palette {name}")
            }
            "hex" => {
                let Some(c) = rest.first().and_then(|h| px::parse_color(h)) else {
                    return Ok("a colour like :hex #ff8800".into());
                };
                if !self.doc.palette.contains(&c) && self.doc.palette.len() < px::MAX_PALETTE {
                    self.doc.palette.push(c);
                }
                self.fg = Some(c);
                format!("colour {}", px::hex(c))
            }
            "flip" | "rotate" | "resize" => {
                self.hist.push(&self.doc);
                match cmd {
                    "flip" => self.doc.flip(rest.first() != Some(&"v")),
                    "rotate" => self.doc.rotate(rest.first() != Some(&"ccw")),
                    _ => self.doc.resize(num(0)?, num(1)?),
                }
                self.dirty = true;
                format!("{cmd} {}", rest.join(" ")).trim().to_string()
            }
            "ms" => {
                if rest.first() == Some(&"all") {
                    let ms = num(1)? as u32;
                    self.doc.frames.iter_mut().for_each(|f| f.ms = ms);
                } else {
                    self.doc.frames[self.frame].ms = num(0)? as u32;
                }
                self.dirty = true;
                String::new()
            }
            "tag" => {
                let name = rest.first().ok_or_else(|| anyhow::anyhow!(":tag NAME FROM TO"))?.to_string();
                let (a, b) = (num(1)? - 1, num(2)? - 1);
                self.doc.tags.retain(|t| t.name != name);
                self.doc.tags.push(px::Tag {
                    name: name.clone(),
                    start: a.min(b).max(0) as usize,
                    end: a.max(b).max(0) as usize,
                    extra: Default::default(),
                });
                self.dirty = true;
                format!("tag {name} {}–{}", a.min(b) + 1, a.max(b) + 1)
            }
            "untag" => {
                let name = rest.first().copied().unwrap_or("");
                self.doc.tags.retain(|t| t.name != name);
                self.dirty = true;
                String::new()
            }
            "layer" | "frame" => {
                let what = rest.first().copied().unwrap_or("");
                self.hist.push(&self.doc);
                self.dirty = true;
                match (cmd, what) {
                    ("layer", "name") => {
                        let name = rest[1..].join(" ");
                        if !name.is_empty() {
                            self.doc.layers[self.layer].name = name;
                        }
                    }
                    ("layer", "del") => self.layer = self.doc.remove_layer(self.layer),
                    ("layer", "up") => self.layer = self.doc.move_layer(self.layer, 1),
                    ("layer", "down") => self.layer = self.doc.move_layer(self.layer, -1),
                    ("frame", "del") => self.frame = self.doc.remove_frame(self.frame),
                    ("frame", "right") => self.frame = self.doc.move_frame(self.frame, 1),
                    ("frame", "left") => self.frame = self.doc.move_frame(self.frame, -1),
                    _ => {}
                }
                String::new()
            }
            "help" | "h" => {
                ctx.toast("PIXEL COMMANDS", COMMANDS);
                String::new()
            }
            other => format!("unknown command :{other} (try :help)"),
        })
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn swatch(c: Px, pal: &Palette) -> Span<'static> {
        match c {
            Some(c) => Span::styled("██", Style::new().fg(rgb(c))),
            None => Span::styled("░░", Style::new().fg(pal.amber_dim)),
        }
    }

    fn render_canvas(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let name = self
            .doc
            .path
            .as_ref()
            .and_then(|p| p.file_name())
            .map_or("untitled".into(), |n| n.to_string_lossy().into_owned());
        let block =
            ui::panel(pal, &format!("CANVAS · {name}{}", if self.dirty { " ●" } else { "" }), view.panel_state(CANVAS))
                .padding(ratatui::widgets::Padding::ZERO)
                .title_bottom(
                    Line::styled(
                        format!(" {}×{} · zoom {} ", self.doc.w, self.doc.h, self.zoom),
                        Style::new().fg(pal.amber_dim),
                    )
                    .right_aligned(),
                );
        let inner = block.inner(area);
        frame.render_widget(block, area);
        frame.render_widget(Paragraph::new("").style(Style::new().bg(pal.bg)), inner);
        self.canvas_rect = inner;
        if self.zoom == 0 {
            self.fit_zoom();
        }
        let (pw, ph) = self.cell();
        let focused = view.inside(CANVAS) || self.anchor.is_some();
        let blink = (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis())
            / 500)
            .is_multiple_of(2);
        let grid = self.view_grid();
        // Pixels across and down that fit; scroll to keep the cursor in view.
        let (fit_w, fit_h) = if ph == 0 {
            (inner.width as i64, inner.height as i64 * 2)
        } else {
            (inner.width as i64 / pw, inner.height as i64 / ph)
        };
        if self.cx < self.ox {
            self.ox = self.cx;
        } else if self.cx >= self.ox + fit_w.max(1) {
            self.ox = self.cx - fit_w + 1;
        }
        if self.cy < self.oy {
            self.oy = self.cy;
        } else if self.cy >= self.oy + fit_h.max(1) {
            self.oy = self.cy - fit_h + 1;
        }
        self.ox = self.ox.clamp(0, (self.doc.w as i64 - fit_w).max(0));
        self.oy = self.oy.clamp(0, (self.doc.h as i64 - fit_h).max(0));
        let color_at = |x: i64, y: i64| grid[y as usize][x as usize].unwrap_or(checker(x as usize, y as usize));
        let buf = frame.buffer_mut();
        if ph == 0 {
            for row in 0..inner.height as i64 {
                let (y0, y1) = (self.oy + row * 2, self.oy + row * 2 + 1);
                if y0 >= self.doc.h as i64 {
                    break;
                }
                for col in 0..(inner.width as i64).min(self.doc.w as i64 - self.ox) {
                    let x = self.ox + col;
                    let mut top = color_at(x, y0);
                    let mut bottom = if y1 < self.doc.h as i64 { color_at(x, y1) } else { [0x0c, 0x08, 0x04] };
                    let (mut tc, mut bc) = (rgb(top), rgb(bottom));
                    if focused && blink {
                        if (x, y0) == (self.cx, self.cy) {
                            tc = contrast(top);
                        }
                        if (x, y1) == (self.cx, self.cy) {
                            bc = contrast(bottom);
                        }
                    }
                    let _ = (&mut top, &mut bottom);
                    buf[(inner.x + col as u16, inner.y + row as u16)].set_symbol("▀").set_fg(tc).set_bg(bc);
                }
            }
        } else {
            for py in self.oy..(self.oy + fit_h).min(self.doc.h as i64) {
                for pxx in self.ox..(self.ox + fit_w).min(self.doc.w as i64) {
                    let c = color_at(pxx, py);
                    let cur = focused && (pxx, py) == (self.cx, self.cy);
                    for sub in 0..ph {
                        let y = inner.y + ((py - self.oy) * ph + sub) as u16;
                        let x0 = inner.x + ((pxx - self.ox) * pw) as u16;
                        let text: String = if cur && sub == (ph - 1) / 2 {
                            format!("[{}]", " ".repeat((pw - 2).max(0) as usize))
                        } else if !cur && self.grid && pw >= 4 {
                            format!("▏{}", " ".repeat((pw - 1) as usize))
                        } else {
                            " ".repeat(pw as usize)
                        };
                        let style = if cur {
                            Style::new().fg(contrast(c)).bg(rgb(c)).add_modifier(Modifier::BOLD)
                        } else if self.grid && pw >= 4 {
                            Style::new().fg(rgb(mix(c, [0, 0, 0], 0.35))).bg(rgb(c))
                        } else {
                            Style::new().bg(rgb(c))
                        };
                        for (i, ch) in text.chars().enumerate() {
                            if x0 + (i as u16) < inner.right() && y < inner.bottom() {
                                buf[(x0 + i as u16, y)].set_char(ch).set_style(style);
                            }
                        }
                    }
                }
            }
        }
    }

    fn tools_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let mut out = vec![];
        for (k, t, name) in TOOLS {
            let on = t == self.tool;
            let style = if on { bold(pal.amber_hi) } else { Style::new().fg(pal.amber) };
            out.push(Line::from(vec![
                Span::styled(
                    format!("{}{k} ", if on { "▸ " } else { "  " }),
                    if on { bold(pal.amber_hi) } else { Style::new().fg(pal.amber_dim) },
                ),
                Span::styled(name, style),
            ]));
        }
        out.push(Line::raw(""));
        let name = |c: Px| c.map_or("clear".into(), px::hex);
        out.push(Line::from(vec![
            Span::styled("FG ", Style::new().fg(pal.amber_dim)),
            Self::swatch(self.fg, pal),
            Span::styled(format!(" {}", name(self.fg)), Style::new().fg(pal.amber)),
        ]));
        out.push(Line::from(vec![
            Span::styled("BG ", Style::new().fg(pal.amber_dim)),
            Self::swatch(self.bg, pal),
            Span::styled(format!(" {}", name(self.bg)), Style::new().fg(pal.amber)),
        ]));
        out.push(Line::raw(""));
        for (k, what, on) in [
            ("F", "filled", self.filled),
            ("m", "mirror", self.mirror),
            ("g", "grid", self.grid),
            ("O", "onion", self.onion),
        ] {
            out.push(Line::from(vec![
                Span::styled(format!("{k} {what:<7}"), Style::new().fg(pal.amber_dim)),
                Span::styled(
                    if on { "on" } else { "off" },
                    if on { bold(pal.teal) } else { Style::new().fg(pal.amber_dim) },
                ),
            ]));
        }
        out.push(Line::from(vec![
            Span::styled("t pen     ", Style::new().fg(pal.amber_dim)),
            Span::styled(
                if self.pen { "DOWN" } else { "up" },
                if self.pen { bold(pal.red) } else { Style::new().fg(pal.amber_dim) },
            ),
        ]));
        out
    }

    fn palette_lines(&self, pal: &Palette, focused: bool) -> Vec<Line<'static>> {
        let mut out = vec![];
        for row in self.doc.palette.chunks(SWATCHES_PER_ROW).enumerate() {
            let (r, colours) = row;
            let spans: Vec<Span> = colours
                .iter()
                .enumerate()
                .map(|(i, c)| {
                    let n = r * SWATCHES_PER_ROW + i;
                    if focused && n == self.palette_at {
                        Span::styled("▐▌", Style::new().fg(contrast(*c)).bg(rgb(*c)).add_modifier(Modifier::BOLD))
                    } else if Some(*c) == self.fg {
                        Span::styled("[]", Style::new().fg(contrast(*c)).bg(rgb(*c)).add_modifier(Modifier::BOLD))
                    } else if Some(*c) == self.bg {
                        Span::styled("<>", Style::new().fg(contrast(*c)).bg(rgb(*c)))
                    } else {
                        Span::styled("██", Style::new().fg(rgb(*c)))
                    }
                })
                .collect();
            out.push(Line::from(spans));
        }
        out.push(Line::raw(""));
        if focused {
            out.push(Line::styled("enter FG · b BG · x clear BG · esc", Style::new().fg(pal.sand)));
        } else {
            out.push(Line::styled("P pick · , . or [ ] step", Style::new().fg(pal.amber_dim)));
            out.push(Line::styled("x swap · # hex", Style::new().fg(pal.amber_dim)));
        }
        out
    }

    fn render_preview(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let block = ui::panel(pal, "PREVIEW", PanelState::Idle);
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let f = self.preview_frame(view.now);
        let grid = self.doc.composite(f.min(self.doc.frames.len() - 1));
        let step = self
            .doc
            .w
            .div_ceil(inner.width.max(1) as usize)
            .max(self.doc.h.div_ceil((inner.height.max(1) as usize) * 2))
            .max(1);
        let rows: Vec<usize> = (0..self.doc.h).step_by(step).collect();
        let cols: Vec<usize> = (0..self.doc.w).step_by(step).collect();
        let buf = frame.buffer_mut();
        for (r, pair) in rows.chunks(2).enumerate() {
            if r as u16 >= inner.height {
                break;
            }
            for (cx, &x) in cols.iter().enumerate() {
                if cx as u16 >= inner.width {
                    break;
                }
                let top = grid[pair[0]][x];
                let bottom = pair.get(1).and_then(|&y| grid[y][x]);
                let cell = &mut buf[(inner.x + cx as u16, inner.y + r as u16)];
                match (top, bottom) {
                    (None, None) => {}
                    (None, Some(b)) => {
                        cell.set_symbol("▄").set_fg(rgb(b));
                    }
                    (Some(t), b) => {
                        cell.set_symbol("▀").set_fg(rgb(t)).set_bg(b.map_or(pal.panel, rgb));
                    }
                }
            }
        }
    }

    fn layer_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let mut out: Vec<Line> = (0..self.doc.layers.len())
            .rev()
            .map(|i| {
                let l = &self.doc.layers[i];
                let style = if i == self.layer {
                    bold(pal.amber).add_modifier(Modifier::REVERSED)
                } else {
                    Style::new().fg(pal.amber)
                };
                Line::from(vec![
                    Span::styled(
                        if l.visible { "● " } else { "○ " },
                        Style::new().fg(if l.visible { pal.teal } else { pal.amber_dim }),
                    ),
                    Span::styled(l.name.clone(), style),
                ])
            })
            .collect();
        out.push(Line::styled("{ } pick · A add · V hide", Style::new().fg(pal.amber_dim)));
        out
    }

    fn frame_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let mut out = vec![];
        for chunk in (0..self.doc.frames.len()).collect::<Vec<_>>().chunks(8) {
            let spans: Vec<Span> = chunk
                .iter()
                .flat_map(|&i| {
                    let style = if i == self.frame {
                        bold(pal.amber).add_modifier(Modifier::REVERSED)
                    } else {
                        Style::new().fg(pal.amber)
                    };
                    [Span::styled(format!("{:02}", i + 1), style), Span::raw(" ")]
                })
                .collect();
            out.push(Line::from(spans));
        }
        let mut info = vec![Span::styled(format!("{} ms", self.doc.frames[self.frame].ms), Style::new().fg(pal.amber))];
        if let Some(tag) = self.doc.tag_at(self.frame) {
            info.push(Span::styled(
                format!(" · {} {}–{}", tag.name, tag.start + 1, tag.end + 1),
                Style::new().fg(pal.teal),
            ));
        }
        if self.playing.is_some() {
            info.push(Span::styled(" · ▶", bold(pal.teal)));
        }
        out.push(Line::from(info));
        out.push(Line::styled("< > pick · N new · p play", Style::new().fg(pal.amber_dim)));
        out
    }

    fn status_line(&self, pal: &Palette) -> Line<'static> {
        let c = self.doc.composite(self.frame)[self.cy as usize][self.cx as usize];
        let mut spans = vec![
            Span::styled(format!(" x {:>3} y {:>3} ", self.cx, self.cy), bold(pal.amber_hi)),
            Span::styled("│ ", Style::new().fg(pal.amber_dim)),
            Self::swatch(c, pal),
            Span::styled(format!(" {:<8}", c.map_or("clear".into(), px::hex)), Style::new().fg(pal.amber)),
            Span::styled(format!("│ {} ", tool_name(self.tool)), Style::new().fg(pal.amber)),
            Span::styled(
                format!(
                    "│ layer {}/{} │ frame {}/{} ",
                    self.layer + 1,
                    self.doc.layers.len(),
                    self.frame + 1,
                    self.doc.frames.len()
                ),
                Style::new().fg(pal.amber_dim),
            ),
        ];
        if !self.message.is_empty() {
            spans.push(Span::styled(format!("│ {}", self.message), bold(pal.sand)));
        }
        Line::from(spans)
    }

    /// Pixel under a mouse position on the canvas.
    fn pixel_at(&self, col: u16, row: u16) -> Option<(i64, i64)> {
        let r = self.canvas_rect;
        if !r.contains((col, row).into()) {
            return None;
        }
        let (pw, ph) = self.cell();
        let (x, y) = ((col - r.x) as i64, (row - r.y) as i64);
        let (px_, py_) = if ph == 0 { (self.ox + x, self.oy + y * 2) } else { (self.ox + x / pw, self.oy + y / ph) };
        self.doc.inside(px_, py_).then_some((px_, py_))
    }
}

impl Channel for PixelView {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "CREATIVE"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        if !self.loaded_last {
            self.loaded_last = true;
            if let Ok(last) = std::fs::read_to_string(Self::last_file()) {
                let p = PathBuf::from(last.trim());
                if p.exists() {
                    let _ = self.open(&p);
                }
            }
        }
        let cmd_h = if self.cmdline.is_some() { 3 } else { 0 };
        let [main, status, cmd] =
            Layout::vertical([Constraint::Fill(1), Constraint::Length(1), Constraint::Length(cmd_h)]).areas(area);
        let [left, canvas, right] =
            Layout::horizontal([Constraint::Length(24), Constraint::Fill(1), Constraint::Length(32)]).areas(main);
        let tools = self.tools_lines(pal);
        let [tools_area, palette_area] =
            Layout::vertical([Constraint::Length(tools.len() as u16 + 2), Constraint::Fill(1)]).areas(left);
        let block = ui::panel(pal, "TOOLS", PanelState::Idle);
        let inner = block.inner(tools_area);
        frame.render_widget(block, tools_area);
        frame.render_widget(Paragraph::new(tools), inner);
        let block = ui::panel(pal, "PALETTE", view.panel_state(PALETTE));
        let inner = block.inner(palette_area);
        frame.render_widget(block, palette_area);
        self.palette_rect = inner;
        frame.render_widget(Paragraph::new(self.palette_lines(pal, view.inside(PALETTE))), inner);

        self.render_canvas(frame, canvas, view);

        let layers = self.layer_lines(pal);
        let [preview, layers_area, frames_area] = Layout::vertical([
            Constraint::Length(17),
            Constraint::Length((layers.len() as u16 + 2).min(12)),
            Constraint::Fill(1),
        ])
        .areas(right);
        self.render_preview(frame, preview, view);
        for (area, title, lines) in [(layers_area, "LAYERS", layers), (frames_area, "FRAMES", self.frame_lines(pal))] {
            let block = ui::panel(pal, title, PanelState::Idle);
            let inner = block.inner(area);
            frame.render_widget(block, area);
            frame.render_widget(Paragraph::new(lines), inner);
        }
        frame.render_widget(Paragraph::new(self.status_line(pal)).style(Style::new().bg(pal.panel)), status);
        if let Some(input) = &mut self.cmdline {
            let block = ui::panel(pal, "", PanelState::Inside);
            let inner = block.inner(cmd);
            frame.render_widget(block, cmd);
            input.render(frame, inner, Style::new().fg(pal.amber_hi), Style::new().fg(pal.amber_dim), true);
        }
        if let Some(b) = &mut self.browser {
            b.render(frame, area, pal);
        }
        vec![canvas, palette_area]
    }

    fn captures_keys(&self) -> bool {
        self.cmdline.is_some() || self.browser.is_some()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if let Some(b) = &mut self.browser {
            let answer = b.key(&key);
            if let Some((old, new)) = b.renamed.take()
                && self.doc.path.as_ref() == Some(&old)
            {
                self.doc.path = Some(new); // the open file was renamed: keep saving there
                self.remember();
            }
            match answer {
                crate::channels::creative::browser::Answer::Open => {}
                crate::channels::creative::browser::Answer::Cancel => self.browser = None,
                crate::channels::creative::browser::Answer::Chosen(choice) => {
                    self.browser = None;
                    self.browsed(choice);
                    ctx.focus(CANVAS);
                }
            }
            return Outcome::Used;
        }
        if let Some(input) = &mut self.cmdline {
            if key.code == KeyCode::Esc {
                self.cmdline = None;
                return Outcome::Used;
            }
            if input.key(&key) == InputResult::Submit {
                let line = input.value.clone();
                self.cmdline = None;
                self.command(&line, ctx);
                ctx.focus(CANVAS);
            }
            return Outcome::Used;
        }
        match section {
            CANVAS => self.canvas_key(&key, ctx),
            PALETTE => self.palette_key(&key, ctx),
            _ => Outcome::Ignored,
        }
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::ctrl(&key, 'o') {
            self.browse();
            return Outcome::Used;
        }
        if keys::is(&key, ':') {
            self.open_cmdline("");
            return Outcome::Used;
        }
        let _ = ctx;
        Outcome::Ignored
    }

    fn paste(&mut self, _section: usize, text: &str, _ctx: &mut Ctx) {
        if let Some(input) = &mut self.cmdline {
            input.insert(text);
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        if self.browser.is_some() {
            return Outcome::Used;
        }
        let at = (event.column, event.row);
        match (event.kind, section) {
            (MouseEventKind::Down(b @ (MouseButton::Left | MouseButton::Right)), Some(CANVAS)) => {
                if let Some((x, y)) = self.pixel_at(at.0, at.1) {
                    (self.cx, self.cy) = (x, y);
                    self.act(b == MouseButton::Right);
                }
            }
            (MouseEventKind::Drag(b @ (MouseButton::Left | MouseButton::Right)), _) => {
                if matches!(self.tool, Tool::Pencil | Tool::Eraser)
                    && let Some((x, y)) = self.pixel_at(at.0, at.1).filter(|p| *p != (self.cx, self.cy))
                {
                    self.stroke_to(x, y, b == MouseButton::Right);
                }
            }
            (MouseEventKind::Down(b), Some(PALETTE)) => {
                let r = self.palette_rect;
                let (x, y) = ((at.0 - r.x) as usize, (at.1 - r.y) as usize);
                let i = y * SWATCHES_PER_ROW + x / 2;
                if x < SWATCHES_PER_ROW * 2 && i < self.doc.palette.len() {
                    let c = Some(self.doc.palette[i]);
                    if b == MouseButton::Right {
                        self.bg = c;
                    } else {
                        self.fg = c;
                    }
                    ctx.focus(CANVAS);
                }
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        Some(CANVAS)
    }

    fn next_wake(&self) -> Option<Instant> {
        let now = Instant::now();
        let mut wake = None;
        if let Some((frames, i, since)) = &self.playing {
            let f = frames[*i % frames.len()];
            let ms = self.doc.frames.get(f).map_or(100, |fr| fr.ms).max(20) as u64;
            wake = Some(*since + Duration::from_millis(ms));
        }
        if self.zoom == 1 {
            // The half-block cursor blinks.
            let blink = now + Duration::from_millis(500);
            wake = Some(wake.map_or(blink, |w: Instant| w.min(blink)));
        }
        wake
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contrast_and_checker() {
        assert_eq!(contrast([255, 255, 255]), Color::Black);
        assert_eq!(contrast([0, 0, 64]), Color::White);
        assert_ne!(checker(0, 0), checker(2, 0));
    }
}
