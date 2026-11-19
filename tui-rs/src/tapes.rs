//! CH-09 TAPES: DreamTapes, my own games, run inside MABEL-OS.
//!
//! LIBRARY lists the tapes: installed ones in `~/.local/share/mabel-os/tapes/<name>/`
//! (`tape.toml`, `tape.wasm`, `art/`, `music/`) and the ones I'm making, listed in the
//! config as `tapes_dev = ["~/code/tapes/snake"]` (their wasm is cargo's release build).
//!
//! A running tape gets the whole screen below the casing bar and every key except
//! ctrl+] (the pause menu) and alt+1–9 (switching channel pauses it). A dev tape
//! restarts by itself when it's rebuilt. A crash or an endless loop shows an error card.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use mabel_console::host::{Options, Tape};
use mabel_console::input::Keymap;
use mabel_console::tapefile::{FIXED, Screen, TapeInfo};
use mabel_console::{Frame as Pixels, Sprite};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Wrap};

use crate::channel::{Channel, Ctx, Outcome, View};
use crate::keys;
use crate::palette::Palette;
use crate::ui::{self, bold};

const LIST: usize = 0;
const CARD: usize = 1;
const MENU: [&str; 3] = ["resume", "reset", "quit to the library"];
/// The longest frame time a tape is told about (after a pause or a stall).
const MAX_DT: f32 = 0.25;

/// `~/.local/share/mabel-os`.
pub fn data_dir() -> PathBuf {
    let base = match std::env::var_os("XDG_DATA_HOME") {
        Some(d) if !d.is_empty() => PathBuf::from(d),
        _ => crate::config::home().join(".local/share"),
    };
    base.join("mabel-os")
}

pub fn installed_dir() -> PathBuf {
    data_dir().join("tapes")
}

fn played_file() -> PathBuf {
    data_dir().join("tapes-played.json")
}

/// One tape in the LIBRARY.
struct Entry {
    dir: PathBuf,
    info: Result<TapeInfo, String>,
    /// Where its wasm is (it may not be built yet).
    wasm: PathBuf,
    dev: bool,
    cover: Option<Sprite>,
}

impl Entry {
    fn read(dir: &Path, dev: bool) -> Entry {
        let info = TapeInfo::read(dir);
        let wasm = if dev { dev_wasm(dir) } else { dir.join("tape.wasm") };
        let cover = info.as_ref().ok().and_then(|i| i.cover.as_ref()).and_then(|c| Sprite::load(&dir.join(c)).ok());
        Entry { dir: dir.to_path_buf(), info, wasm, dev, cover }
    }

    fn key(&self) -> String {
        self.dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
    }

    fn title(&self) -> String {
        match &self.info {
            Ok(i) => i.title.clone(),
            Err(_) => self.key(),
        }
    }
}

/// `tapes_dev = ["~/code/tapes/snake", …]` from config.toml.
fn dev_dirs(table: &toml::Table) -> Vec<PathBuf> {
    table
        .get("tapes_dev")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|v| v.as_str()).map(crate::config::expand).collect())
        .unwrap_or_default()
}

/// A dev tape's wasm: cargo's release build, named after the package.
fn dev_wasm(dir: &Path) -> PathBuf {
    let name = std::fs::read_to_string(dir.join("Cargo.toml"))
        .ok()
        .and_then(|t| t.parse::<toml::Table>().ok())
        .and_then(|t| t.get("package")?.get("name")?.as_str().map(str::to_string))
        .unwrap_or_else(|| dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
    dir.join("target/wasm32-unknown-unknown/release").join(format!("{}.wasm", name.replace('-', "_")))
}

fn mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// A terminal key as the name `tape.toml` and the keymap use: "left", "z", "space", "enter".
pub fn key_name(key: &KeyEvent) -> Option<String> {
    Some(match key.code {
        KeyCode::Left => "left".into(),
        KeyCode::Right => "right".into(),
        KeyCode::Up => "up".into(),
        KeyCode::Down => "down".into(),
        KeyCode::Enter => "enter".into(),
        KeyCode::Backspace => "backspace".into(),
        KeyCode::Tab => "tab".into(),
        KeyCode::Esc => "esc".into(),
        KeyCode::Char(' ') => "space".into(),
        KeyCode::Char(c) => c.to_lowercase().to_string(),
        _ => return None,
    })
}

struct Running {
    tape: Tape,
    entry: usize,
    keymap: Keymap,
    screen: Screen,
    period: Duration,
    next_frame: Instant,
    last_frame: Instant,
    /// The wasm's time when it was loaded (a newer one means: rebuilt, restart).
    built: Option<SystemTime>,
    /// The pause menu, with the selected line.
    menu: Option<usize>,
    crashed: Option<String>,
}

pub struct Tapes {
    entries: Vec<Entry>,
    sel: usize,
    scanned: bool,
    run: Option<Running>,
    played: BTreeMap<String, i64>,
    /// The last run's console: its `log` lines, and how it ended.
    console: Vec<String>,
    /// The tape the console belongs to.
    last_run: Option<PathBuf>,
    dev_dirs: Vec<PathBuf>,
    rate: u32,
    /// The body's size in cells, from the last draw.
    area: (u16, u16),
}

impl Tapes {
    pub fn new(cfg: &crate::config::Config) -> Self {
        let dev_dirs = dev_dirs(&cfg.raw);
        let played =
            std::fs::read_to_string(played_file()).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
        Tapes {
            entries: vec![],
            sel: 0,
            scanned: false,
            run: None,
            played,
            console: vec![],
            last_run: None,
            dev_dirs,
            rate: cfg.tracker_rate.clamp(8000, 96000) as u32,
            area: (128, 36),
        }
    }

    fn scan(&mut self) {
        // config.toml again: `mabel-os new-tape` adds to tapes_dev while MABEL-OS runs.
        let fresh =
            std::fs::read_to_string(crate::config::config_path()).ok().and_then(|t| t.parse::<toml::Table>().ok());
        if let Some(table) = fresh {
            self.dev_dirs = dev_dirs(&table);
        }
        let keep = self.entries.get(self.sel).map(|e| e.dir.clone());
        let mut entries: Vec<Entry> =
            self.dev_dirs.iter().filter(|d| d.is_dir()).map(|d| Entry::read(d, true)).collect();
        let mut installed: Vec<PathBuf> = std::fs::read_dir(installed_dir())
            .map(|rd| rd.flatten().map(|e| e.path()).filter(|p| p.is_dir()).collect())
            .unwrap_or_default();
        installed.sort();
        entries.extend(installed.iter().map(|d| Entry::read(d, false)));
        self.entries = entries;
        self.sel = keep.and_then(|k| self.entries.iter().position(|e| e.dir == k)).unwrap_or(0);
        self.scanned = true;
    }

    fn start(&mut self, ctx: &mut Ctx, area_cells: (u16, u16)) {
        let Some(entry) = self.entries.get(self.sel) else { return };
        let info = match &entry.info {
            Ok(i) => i.clone(),
            Err(e) => {
                ctx.error("TAPES", e);
                return;
            }
        };
        let wasm = match std::fs::read(&entry.wasm) {
            Ok(w) => w,
            Err(_) if entry.dev => {
                ctx.error("TAPES", "Not built yet: cargo build --release --target wasm32-unknown-unknown");
                return;
            }
            Err(e) => {
                ctx.error("TAPES", &format!("{}: {e}", entry.wasm.display()));
                return;
            }
        };
        self.last_run = Some(entry.dir.clone());
        let (w, h) = pixel_size(info.screen, area_cells);
        let opts = Options { w, h, root: entry.dir.clone(), rate: self.rate };
        self.console.clear();
        match Tape::load(&wasm, opts) {
            Ok(mut tape) => {
                self.console.extend(tape.take_log());
                let now = Instant::now();
                let period = Duration::from_secs_f64(1.0 / info.fps as f64);
                self.played.insert(entry.key(), chrono::Utc::now().timestamp());
                if let Ok(text) = serde_json::to_string_pretty(&self.played) {
                    let _ = std::fs::create_dir_all(data_dir());
                    let _ = std::fs::write(played_file(), text);
                }
                self.run = Some(Running {
                    tape,
                    entry: self.sel,
                    keymap: Keymap::new(&info.keys),
                    screen: info.screen,
                    period,
                    next_frame: now,
                    last_frame: now,
                    built: mtime(&entry.wasm),
                    menu: None,
                    crashed: None,
                });
                ctx.focus(LIST);
            }
            Err(e) => {
                self.console.push(format!("couldn't start: {e}"));
                ctx.error("TAPE CRASHED", &e);
            }
        }
    }

    fn stop(&mut self) {
        if let Some(mut run) = self.run.take() {
            self.console.extend(run.tape.take_log());
            if let Some(e) = run.crashed {
                self.console.push(format!("crashed: {e}"));
            }
        }
    }

    fn pause(&mut self) {
        if let Some(run) = &mut self.run
            && run.menu.is_none()
            && run.crashed.is_none()
        {
            run.menu = Some(0);
            run.tape.pause(true);
        }
    }

    fn resume(run: &mut Running) {
        run.menu = None;
        run.tape.pause(false);
        let now = Instant::now();
        (run.next_frame, run.last_frame) = (now, now); // no catch-up jump after the pause
    }

    /// Step the tape if a frame is due.
    fn advance(&mut self, now: Instant) {
        let Some(run) = &mut self.run else { return };
        if run.menu.is_some() || run.crashed.is_some() || now < run.next_frame {
            return;
        }
        let dt = now.duration_since(run.last_frame).as_secs_f32().min(MAX_DT);
        run.last_frame = now;
        run.next_frame += run.period;
        if run.next_frame < now {
            run.next_frame = now + run.period; // fell behind: don't try to catch up
        }
        if let Err(e) = run.tape.step(dt, now) {
            run.tape.pause(true);
            run.crashed = Some(e);
        }
        let log = run.tape.take_log();
        self.console.extend(log);
        let excess = self.console.len().saturating_sub(200);
        self.console.drain(..excess);
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn render_run(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        self.advance(Instant::now());
        let Some(run) = &mut self.run else { return };
        frame.render_widget(Block::new().style(Style::new().bg(pal.bg)), area);
        let (w, h) = pixel_size(run.screen, (area.width, area.height));
        if run.screen == Screen::Fill {
            run.tape.resize(w, h);
        }
        let scale = match run.screen {
            Screen::Fill => 1,
            Screen::Fixed => (area.width as usize / FIXED.0).min(area.height as usize * 2 / FIXED.1).max(1),
        };
        let (cols, rows) = ((w * scale) as u16, (h * scale).div_ceil(2) as u16);
        let place = Rect {
            x: area.x + area.width.saturating_sub(cols) / 2,
            y: area.y + area.height.saturating_sub(rows) / 2,
            width: cols.min(area.width),
            height: rows.min(area.height),
        };
        mabel_console::render(run.tape.frame(), run.tape.texts(), frame.buffer_mut(), place, scale);

        if let Some(sel) = run.menu {
            let title = self.entries.get(run.entry).map(Entry::title).unwrap_or_default();
            let mut lines = vec![Line::styled(format!(" {title}"), bold(pal.beige)), Line::raw("")];
            for (i, item) in MENU.iter().enumerate() {
                let style = if i == sel { bold(pal.bg).bg(pal.amber) } else { Style::new().fg(pal.amber) };
                lines.push(Line::styled(format!("  {item}  "), style));
            }
            lines.push(Line::raw(""));
            lines.push(Line::styled(" ctrl+] or esc: resume", Style::new().fg(pal.amber_dim)));
            card(frame, area, pal, "▌▌ PAUSED", lines, false);
        } else if let Some(e) = &run.crashed {
            let lines = vec![
                Line::styled(" The tape stopped:", bold(pal.beige)),
                Line::raw(""),
                Line::styled(format!(" {e}"), Style::new().fg(pal.amber)),
                Line::raw(""),
                Line::styled(
                    " enter: back to the library (the console is on its card)",
                    Style::new().fg(pal.amber_dim),
                ),
            ];
            card(frame, area, pal, "TAPE CRASHED", lines, true);
        }
    }

    fn render_library(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        let (list, detail) = if view.narrow {
            let [a, b] = Layout::vertical([Constraint::Percentage(40), Constraint::Fill(1)]).areas(area);
            (a, b)
        } else {
            let [a, b] = Layout::horizontal([Constraint::Percentage(38), Constraint::Fill(1)]).areas(area);
            (a, b)
        };

        let block = ui::panel(pal, "LIBRARY", view.panel_state(LIST));
        let inner = block.inner(list);
        frame.render_widget(block, list);
        let mut lines = vec![];
        if self.entries.is_empty() {
            lines.push(Line::styled(" No DreamTapes yet.", bold(pal.beige)));
            lines.push(Line::raw(""));
            lines.push(Line::styled(" Make one:  mabel-os new-tape snake", Style::new().fg(pal.amber)));
            lines.push(Line::styled(" (on the PC or laptop; then r here)", Style::new().fg(pal.amber_dim)));
            lines.push(Line::styled(" Installed tapes live in", Style::new().fg(pal.amber_dim)));
            lines.push(Line::styled(format!(" {}", installed_dir().display()), Style::new().fg(pal.amber_dim)));
        }
        let rows = inner.height as usize;
        let top = self.sel.saturating_sub(rows.saturating_sub(1));
        for (i, e) in self.entries.iter().enumerate().skip(top).take(rows) {
            let on = i == self.sel;
            let style = if on && view.inside(LIST) {
                bold(pal.bg).bg(pal.amber)
            } else if on {
                bold(pal.amber_hi)
            } else {
                Style::new().fg(pal.amber)
            };
            let mut spans = vec![Span::styled(format!(" ◘ {} ", e.title()), style)];
            if e.dev {
                spans.push(Span::styled(" DEV", Style::new().fg(pal.teal)));
            }
            if e.info.is_err() {
                spans.push(Span::styled(" !", Style::new().fg(pal.red)));
            }
            lines.push(Line::from(spans));
        }
        frame.render_widget(Paragraph::new(lines), inner);

        let block = ui::panel(pal, "TAPE", view.panel_state(CARD));
        let inner = block.inner(detail);
        frame.render_widget(block, detail);
        if let Some(e) = self.entries.get(self.sel) {
            self.render_card(frame, inner, pal, e);
        }
        vec![list, detail]
    }

    fn render_card(&self, frame: &mut Frame, area: Rect, pal: &Palette, e: &Entry) {
        let mut y = area.y;
        if let Some(cover) = &e.cover {
            let max_rows = (area.height / 2).max(4) as usize;
            let scale = (area.width as usize / cover.w.max(1)).min(max_rows * 2 / cover.h.max(1)).clamp(1, 4);
            let mut px = Pixels::new(cover.w, cover.h);
            px.cls(rgb(pal.bg));
            cover.draw(&mut px, 0, 0, 0, false, false);
            let rows = (cover.h * scale).div_ceil(2) as u16;
            let place = Rect { x: area.x + 1, y, width: area.width.saturating_sub(1), height: rows.min(area.height) };
            mabel_console::render(&px, &[], frame.buffer_mut(), place, scale);
            y += rows + 1;
        }
        let mut lines = vec![];
        match &e.info {
            Ok(info) => {
                lines.push(Line::styled(format!(" {}", info.title), bold(pal.beige)));
                if !info.author.is_empty() {
                    lines.push(Line::styled(format!(" by {}", info.author), Style::new().fg(pal.amber_dim)));
                }
                if !info.description.is_empty() {
                    lines.push(Line::raw(""));
                    lines.push(Line::styled(format!(" {}", info.description), Style::new().fg(pal.amber)));
                }
                lines.push(Line::raw(""));
                let played = self.played.get(&e.key()).and_then(|t| chrono::DateTime::from_timestamp(*t, 0));
                let when = played
                    .map(|t| t.with_timezone(&chrono::Local).format("%d %b %Y %H:%M").to_string())
                    .unwrap_or_else(|| "never".into());
                lines.push(Line::styled(format!(" last played: {when}"), Style::new().fg(pal.amber_dim)));
                let screen = match info.screen {
                    Screen::Fill => "fills the screen",
                    Screen::Fixed => "128×72",
                };
                lines.push(Line::styled(format!(" {screen} · {} fps", info.fps), Style::new().fg(pal.amber_dim)));
                if e.dev {
                    let built = if e.wasm.exists() { "built" } else { "not built yet" };
                    lines
                        .push(Line::styled(format!(" DEV · {} · {built}", e.dir.display()), Style::new().fg(pal.teal)));
                }
                lines.push(Line::raw(""));
                lines.push(Line::styled(" enter: play", Style::new().fg(pal.amber)));
            }
            Err(err) => lines.push(Line::styled(format!(" {err}"), Style::new().fg(pal.red))),
        }
        let is_last = self.run.is_none() && !self.console.is_empty() && e.dir == self.last_dir();
        if is_last {
            lines.push(Line::raw(""));
            lines.push(Line::styled(" CONSOLE (last run)", bold(pal.beige)));
            let room = (area.height as usize).saturating_sub(lines.len() + (y - area.y) as usize).max(1);
            for l in self.console.iter().rev().take(room).rev() {
                let colour = if l.starts_with("crashed") || l.starts_with("panic") { pal.red } else { pal.amber_dim };
                lines.push(Line::styled(format!(" {l}"), Style::new().fg(colour)));
            }
        }
        let rest = Rect { y, height: area.height.saturating_sub(y - area.y), ..area };
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), rest);
    }

    fn last_dir(&self) -> PathBuf {
        self.last_run.clone().unwrap_or_default()
    }
}

fn rgb(c: ratatui::style::Color) -> u32 {
    match c {
        ratatui::style::Color::Rgb(r, g, b) => (r as u32) << 16 | (g as u32) << 8 | b as u32,
        _ => 0,
    }
}

/// The tape's screen in pixels for a body of `cells`.
fn pixel_size(screen: Screen, cells: (u16, u16)) -> (usize, usize) {
    match screen {
        Screen::Fill => (cells.0.max(1) as usize, cells.1.max(1) as usize * 2),
        Screen::Fixed => FIXED,
    }
}

/// A small centred card over the game (the pause menu, the crash).
fn card(frame: &mut Frame, area: Rect, pal: &Palette, title: &str, lines: Vec<Line>, error: bool) {
    let w = 46.min(area.width);
    let h = (lines.len() as u16 + 2).min(area.height);
    let r = Rect { x: area.x + (area.width - w) / 2, y: area.y + (area.height - h) / 2, width: w, height: h };
    frame.render_widget(Clear, r);
    let colour = if error { pal.red } else { pal.amber };
    let block = Block::bordered()
        .title(Span::styled(format!(" {title} "), bold(colour)))
        .border_style(Style::new().fg(colour))
        .style(Style::new().bg(pal.panel));
    frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }).block(block), r);
}

impl Channel for Tapes {
    fn id(&self) -> &'static str {
        "tapes"
    }

    fn tab(&self) -> &'static str {
        "TAPES"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        if !self.scanned {
            self.scan();
        }
        self.area = (area.width, area.height);
        if self.run.is_some() {
            self.render_run(frame, area, view.pal);
            return vec![area];
        }
        self.render_library(frame, area, view)
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if let Some(run) = &mut self.run {
            if let Some(sel) = &mut run.menu {
                if key.kind == KeyEventKind::Release {
                    return Outcome::Used;
                }
                match (keys::ch(&key), key.code) {
                    (Some('j'), _) | (_, KeyCode::Down) => *sel = (*sel + 1) % MENU.len(),
                    (Some('k'), _) | (_, KeyCode::Up) => *sel = (*sel + MENU.len() - 1) % MENU.len(),
                    (_, KeyCode::Esc) => Self::resume(run),
                    (_, KeyCode::Enter) => match *sel {
                        0 => Self::resume(run),
                        1 => {
                            let entry = run.entry;
                            self.stop();
                            self.sel = entry;
                            self.start(ctx, self.area);
                        }
                        _ => self.stop(),
                    },
                    _ => {}
                }
                return Outcome::Used;
            }
            if run.crashed.is_some() {
                if key.kind != KeyEventKind::Release && matches!(key.code, KeyCode::Enter | KeyCode::Esc) {
                    self.stop();
                }
                return Outcome::Used;
            }
            if let Some(b) = key_name(&key).and_then(|n| run.keymap.button(&n)) {
                match key.kind {
                    KeyEventKind::Release => run.tape.buttons().release(b),
                    kind => run.tape.buttons().press(b, kind == KeyEventKind::Repeat, Instant::now()),
                }
            }
            return Outcome::Used;
        }
        match (keys::ch(&key), key.code) {
            (Some('j'), _) | (_, KeyCode::Down) if section == LIST => {
                self.sel = (self.sel + 1).min(self.entries.len().saturating_sub(1));
            }
            (Some('k'), _) | (_, KeyCode::Up) if section == LIST => self.sel = self.sel.saturating_sub(1),
            // h / l hop between LIBRARY and the TAPE card.
            (Some('l'), _) | (_, KeyCode::Right) if section == LIST => ctx.focus(CARD),
            (Some('h'), _) | (_, KeyCode::Left) if section == CARD => ctx.focus(LIST),
            (_, KeyCode::Enter) => self.start(ctx, self.area),
            (Some('r'), _) => {
                self.scan();
                ctx.status(&format!("{} tapes", self.entries.len()));
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::is(&key, 'r') && self.run.is_none() {
            self.scan();
            ctx.status(&format!("{} tapes", self.entries.len()));
            return Outcome::Used;
        }
        Outcome::Ignored
    }

    /// Inside while a tape runs (its keys are raw); otherwise nav mode like LIFE or AUDIO.
    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        self.run.is_some().then_some(0)
    }

    fn raw_keys(&self, _section: usize) -> bool {
        self.run.is_some()
    }

    fn escape(&mut self, _section: usize, _ctx: &mut Ctx) -> bool {
        match &mut self.run {
            None => false,
            Some(run) if run.menu.is_some() => {
                Self::resume(run);
                true
            }
            Some(_) => {
                self.pause();
                true
            }
        }
    }

    fn takes_screen(&self) -> bool {
        self.run.is_some()
    }

    fn key_releases(&self) -> bool {
        self.run.as_ref().is_some_and(|r| r.menu.is_none() && r.crashed.is_none())
    }

    fn next_wake(&self) -> Option<Instant> {
        self.run.as_ref().filter(|r| r.menu.is_none() && r.crashed.is_none()).map(|r| r.next_frame)
    }

    fn tick(&mut self, _now: Instant, visible: bool, ctx: &mut Ctx) {
        if !visible {
            self.pause(); // another channel came on screen
            return;
        }
        // A dev tape that was rebuilt starts again with the new code.
        let rebuilt = self.run.as_ref().and_then(|run| {
            let e = self.entries.get(run.entry)?;
            (e.dev && mtime(&e.wasm).is_some_and(|t| Some(t) != run.built)).then_some(run.entry)
        });
        if let Some(entry) = rebuilt {
            // Wait until cargo has finished writing it (the size stops changing).
            let path = self.entries[entry].wasm.clone();
            let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            std::thread::sleep(Duration::from_millis(150));
            if std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0) != size {
                return;
            }
            self.stop();
            self.entries[entry] = Entry::read(&self.entries[entry].dir.clone(), true);
            self.sel = entry;
            self.start(ctx, self.area);
            ctx.toast("TAPES", "↻ rebuilt: the tape started again with the new code");
        }
    }
}
