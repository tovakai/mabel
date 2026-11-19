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

/// The id messages and help use.
const ID: &str = "pixel";
// The two sections you can go into (the other panels only show things).
const CANVAS: usize = 0;
const PALETTE: usize = 1;
/// Colours per row in PALETTE.
const SWATCHES_PER_ROW: usize = 8;
/// The two browns of the checkerboard that shows through transparent pixels.
const CHECKER: [[u8; 3]; 2] = [[0x1b, 0x15, 0x10], [0x25, 0x1d, 0x15]];

/// `:help`'s list of commands.
const COMMANDS: &str = ":new W H [palette] · :w [file] · :e file · :avatar · :e! / :new! (drop changes) · \
:export png|strip|sheet [scale] [file] [columns] · :palette NAME (pico8 db16 db32 gameboy grayscale phosphor) · \
:flip h|v · :rotate cw|ccw · :resize W H · :ms N · :ms all N · :tag NAME FROM TO · :untag NAME · \
:layer name NAME · :layer del · :layer up|down · :frame del · :frame left|right · :hex #rrggbb";

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
/// What space/enter does on the canvas.
pub enum Tool {
    Pencil,
    Eraser,
    Fill,
    Line,
    Rect,
    Ellipse,
    Pick,
}

/// Each tool's key and name.
const TOOLS: [(char, Tool, &str); 7] = [
    ('b', Tool::Pencil, "pencil"),
    ('e', Tool::Eraser, "eraser"),
    ('f', Tool::Fill, "fill"),
    ('n', Tool::Line, "line"),
    ('r', Tool::Rect, "rect"),
    ('o', Tool::Ellipse, "ellipse"),
    ('c', Tool::Pick, "pick"),
];

/// A tool's name, for the status line.
fn tool_name(t: Tool) -> &'static str {
    TOOLS.iter().find(|(_, x, _)| *x == t).map_or("?", |(_, _, n)| n)
}

/// A picture colour as a terminal colour.
fn rgb(c: [u8; 3]) -> Color {
    Color::Rgb(c[0], c[1], c[2])
}

/// Black or white, whichever reads on `c`.
fn contrast(c: [u8; 3]) -> Color {
    let lum = 0.299 * c[0] as f64 + 0.587 * c[1] as f64 + 0.114 * c[2] as f64;
    if lum > 140.0 { Color::Black } else { Color::White }
}

/// Blend `a` towards `b` (`amount` 0 = a, 1 = b): onion skin fades the last frame with it.
fn mix(a: [u8; 3], b: [u8; 3], amount: f64) -> [u8; 3] {
    let m = |x: u8, y: u8| (x as f64 + (y as f64 - x as f64) * amount) as u8;
    [m(a[0], b[0]), m(a[1], b[1]), m(a[2], b[2])]
}

/// The checkerboard colour behind transparent pixel (x, y), in 2×2 blocks.
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

// PIXEL's code is split by job; every piece adds to `impl PixelView`.
/// the `:` command line
mod commands;
/// what each key and click does
mod controls;
/// drawing the panels
mod draw;
/// tools, strokes, colours, undo
mod editing;

pub use draw::thumbnail;

/// The PIXEL screen: the picture being edited, the cursor, the tool and colours, the view.
pub struct PixelView {
    /// The picture (`mabel-media`'s pixel model: layers × frames of pixels, a palette, tags).
    pub doc: Doc,
    /// Undo and redo.
    hist: History,
    /// The cursor, in picture pixels.
    cx: i64,
    cy: i64,
    tool: Tool,
    /// The tool to go back to after `c` picks a colour.
    prev_tool: Tool,
    /// The colours space draws with (and with shift: bg). None = transparent.
    fg: Px,
    bg: Px,
    /// 1 = half-blocks (1×2 pixels per cell), 2+ = bigger square pixels, 0 = fit on next draw.
    zoom: i64,
    /// The layer and frame being edited.
    layer: usize,
    frame: usize,
    /// F: rectangles and ellipses come out filled.
    filled: bool,
    /// m: everything drawn is mirrored left-right too.
    mirror: bool,
    /// g: lines between pixels when zoomed in.
    grid: bool,
    /// O: the previous frame shows faintly under this one.
    onion: bool,
    /// The first corner of a line, rectangle or ellipse being drawn (the second is the cursor).
    anchor: Option<(i64, i64)>,
    /// t: pen down, so moving draws.
    pen: bool,
    /// p: the animation playing in PREVIEW: (frames in order, which one, since when).
    playing: Option<(Vec<usize>, usize, Instant)>,
    /// Changed since the last save.
    pub dirty: bool,
    /// The status line's message.
    message: String,
    /// The `:` line, while it's open.
    cmdline: Option<Input>,
    /// PALETTE's cursor.
    palette_at: usize,
    /// The canvas's scroll, in pixels, and where it was drawn.
    ox: i64,
    oy: i64,
    canvas_rect: Rect,
    palette_rect: Rect,
    /// Where pictures are saved (`pixel_dir` in config.toml).
    dir: PathBuf,
    /// `avatar` in config.toml: where `:avatar` finds Mabel's face.
    avatar: String,
    /// The file browser (ctrl+o).
    browser: Option<crate::channels::creative::browser::Browser>,
    /// The picture open last time has been reopened (done once, on first draw).
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

    /// Where the last opened picture's path is kept, so PIXEL reopens it next time.
    fn last_file() -> PathBuf {
        crate::os::config::cache_dir().join("pixel-last")
    }

    /// Write the open picture's path to `last_file`.
    fn remember(&self) {
        if let Some(p) = &self.doc.path {
            let _ = std::fs::create_dir_all(crate::os::config::cache_dir());
            let _ = std::fs::write(Self::last_file(), p.display().to_string());
        }
    }

    /// A typed name → a full path (in `pixel_dir` unless absolute).
    fn path(&self, name: &str) -> PathBuf {
        let p = crate::os::config::expand(name);
        if p.is_absolute() { p } else { self.dir.join(p) }
    }

    /// Open a picture (.mpx, .png, or an avatar folder): fresh undo, first frame and layer,
    /// zoomed to fit.
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

    /// What the canvas shows: the frame's layers merged, the onion skin under it, and the
    /// shape being drawn on top (it only becomes pixels when finished).
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

    /// The pixels of the current shape tool between two corners.
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
