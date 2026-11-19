//! CH-09 TAPES: DreamTapes, my own games, run inside MABEL-OS.
//!
//! LIBRARY lists three kinds of tape:
//! - DEV: the ones I'm making, listed in config.toml as `tapes_dev = ["~/code/tapes/snake"]`
//!   (their wasm is cargo's release build; a rebuild restarts a running one);
//! - installed: `~/.local/share/mabel-os/tapes/<name>/` (`tape.toml`, `tape.wasm`, `art/`, `music/`);
//! - ON MABEL: in my tape repo (`mabel-os tape publish` on the PC) and not installed here yet:
//!   enter installs it. An installed tape with a newer copy on Mabel says so; `u` updates it.
//!
//! A running tape gets the whole screen below the casing bar and every key except ctrl+]
//! (the pause menu: resume, reset, screenshot, console, quit) and alt+1–9 (switching channel
//! pauses it). A crash or an endless loop shows an error card.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime};

use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use mabel_console::host::{Options, Tape};
use mabel_console::input::{Keymap, key_name};
use mabel_console::tapefile::{Screen, TapeInfo};
use mabel_console::{Frame as Pixels, Sprite};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Clear, Paragraph, Wrap};
use serde_json::Value;

use crate::channel::{Channel, Ctx, Outcome, View};
use crate::keys;
use crate::palette::Palette;
use crate::ui::{self, bold};

const LIST: usize = 0;
const CARD: usize = 1;
const MENU: [&str; 5] = ["resume", "reset", "screenshot", "console", "quit to the library"];
/// The longest frame time a tape is told about (after a pause or a stall).
const MAX_DT: f32 = 0.25;
/// Screenshots are saved this many times bigger than the tape's pixels.
const SHOT_SCALE: u32 = 4;

/// `~/.local/share/mabel-os`.
pub fn data_dir() -> PathBuf {
    mabel_console::saves::data_dir()
}

pub fn installed_dir() -> PathBuf {
    data_dir().join("tapes")
}

fn played_file() -> PathBuf {
    data_dir().join("tapes-played.json")
}

/// Which version of a tape is installed (the .dtape's sha16), written by `install`.
const SHA_FILE: &str = ".dtape-sha";

/// Install a .dtape as `~/.local/share/mabel-os/tapes/<name>`.
pub fn install(name: &str, bytes: &[u8]) -> anyhow::Result<PathBuf> {
    let dest = installed_dir().join(name);
    crate::dtape::unpack(bytes, &dest)?;
    std::fs::write(dest.join(SHA_FILE), crate::dtape::sha16(bytes))?;
    Ok(dest)
}

/// A tape in my repo on Mabel, as `/api/tapes` lists it.
#[derive(Clone, Debug)]
pub struct Remote {
    pub name: String,
    pub sha: String,
    pub info: TapeInfo,
}

impl Remote {
    pub fn from_json(v: &Value) -> Option<Remote> {
        let s = |k: &str| v[k].as_str().unwrap_or("").to_string();
        let name = s("name");
        if name.is_empty() {
            return None;
        }
        let info = TapeInfo {
            title: if s("title").is_empty() { name.clone() } else { s("title") },
            author: s("author"),
            description: s("description"),
            screen: if s("screen") == "128x72" { Screen::Fixed } else { Screen::Fill },
            fps: v["fps"].as_u64().unwrap_or(30) as u32,
            ..TapeInfo::default()
        };
        Some(Remote { name, sha: s("sha"), info })
    }
}

#[derive(Clone, Debug, PartialEq)]
enum Kind {
    Dev,
    Installed,
    /// On Mabel, not installed here.
    Remote,
}

/// One tape in the LIBRARY.
struct Entry {
    kind: Kind,
    /// Its folder (for a Remote one: where it would be installed).
    dir: PathBuf,
    info: Result<TapeInfo, String>,
    /// Where its wasm is (it may not be built yet).
    wasm: PathBuf,
    cover: Option<Sprite>,
    /// Installed, and Mabel has a different version of it.
    update: bool,
}

impl Entry {
    fn read(dir: &Path, kind: Kind) -> Entry {
        let info = TapeInfo::read(dir);
        let wasm = if kind == Kind::Dev { dev_wasm(dir) } else { dir.join("tape.wasm") };
        let cover = info.as_ref().ok().and_then(|i| i.cover.as_ref()).and_then(|c| Sprite::load(&dir.join(c)).ok());
        Entry { kind, dir: dir.to_path_buf(), info, wasm, cover, update: false }
    }

    fn remote(r: &Remote) -> Entry {
        let dir = installed_dir().join(&r.name);
        Entry {
            kind: Kind::Remote,
            wasm: dir.join("tape.wasm"),
            dir,
            info: Ok(r.info.clone()),
            cover: None,
            update: false,
        }
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
pub fn dev_wasm(dir: &Path) -> PathBuf {
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

/// Where screenshots go: ~/Pictures/mabel-os if there's a Pictures folder, else next to the tapes.
fn screenshot_dir() -> PathBuf {
    let pictures = crate::config::home().join("Pictures");
    if pictures.is_dir() { pictures.join("mabel-os") } else { data_dir().join("screenshots") }
}

/// The tape's screen as a PNG, `SHOT_SCALE` times bigger.
fn screenshot(px: &Pixels, name: &str) -> anyhow::Result<PathBuf> {
    let (w, h) = (px.w as u32, px.h as u32);
    let img = image::RgbImage::from_fn(w * SHOT_SCALE, h * SHOT_SCALE, |x, y| {
        let c = px.px[(y / SHOT_SCALE * w + x / SHOT_SCALE) as usize];
        image::Rgb([(c >> 16) as u8, (c >> 8) as u8, c as u8])
    });
    let dir = screenshot_dir();
    std::fs::create_dir_all(&dir)?;
    let path = dir.join(format!("{name}-{}.png", chrono::Local::now().format("%Y%m%d-%H%M%S")));
    img.save(&path)?;
    Ok(path)
}

/// Messages from background work (ctx.run → on_msg).
enum Note {
    Remote(Result<Vec<Remote>, String>),
    Installed { name: String, result: Result<Vec<u8>, String>, play: bool },
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
    /// The console over the paused game (from the pause menu).
    console: bool,
    crashed: Option<String>,
}

pub struct Tapes {
    entries: Vec<Entry>,
    sel: usize,
    scanned: bool,
    run: Option<Running>,
    played: BTreeMap<String, i64>,
    /// The current or last run's console: its `log` lines, and how it ended.
    console: Vec<String>,
    /// The tape the console belongs to.
    last_run: Option<PathBuf>,
    dev_dirs: Vec<PathBuf>,
    /// What Mabel's tape repo had at the last look (None: not asked yet, or unreachable).
    remote: Option<Vec<Remote>>,
    remote_error: Option<String>,
    /// A download running (its tape name).
    installing: Option<String>,
    rate: u32,
    /// The body's size in cells, from the last draw.
    area: Rect,
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
            remote: None,
            remote_error: None,
            installing: None,
            rate: cfg.tracker_rate.clamp(8000, 96000) as u32,
            area: Rect::new(0, 0, 128, 36),
        }
    }

    /// Ask Mabel what's in the tape repo (the answer arrives in on_msg).
    fn fetch_remote(&self, ctx: &mut Ctx) {
        ctx.spawn("tapes", |api| async move {
            let result = api.tapes().await.map(|list| list.iter().filter_map(Remote::from_json).collect());
            Note::Remote(result.map_err(|e| format!("{e:#}")))
        });
    }

    fn download(&mut self, ctx: &mut Ctx, name: String, play: bool) {
        if self.installing.is_some() {
            return;
        }
        self.installing = Some(name.clone());
        ctx.status(&format!("installing {name}…"));
        ctx.spawn("tapes", move |api| async move {
            let result = api.tape_download(&name).await.map_err(|e| format!("{e:#}"));
            Note::Installed { name, result, play }
        });
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
            self.dev_dirs.iter().filter(|d| d.is_dir()).map(|d| Entry::read(d, Kind::Dev)).collect();
        let mut installed: Vec<PathBuf> = std::fs::read_dir(installed_dir())
            .map(|rd| {
                rd.flatten()
                    .map(|e| e.path())
                    .filter(|p| p.is_dir() && !p.file_name().is_some_and(|n| n.to_string_lossy().starts_with('.')))
                    .collect()
            })
            .unwrap_or_default();
        installed.sort();
        for dir in &installed {
            let mut e = Entry::read(dir, Kind::Installed);
            let have = std::fs::read_to_string(dir.join(SHA_FILE)).unwrap_or_default();
            e.update = self
                .remote
                .iter()
                .flatten()
                .any(|r| r.name == e.key() && !have.trim().is_empty() && r.sha != have.trim());
            entries.push(e);
        }
        for r in self.remote.iter().flatten() {
            if !installed.iter().any(|d| d.file_name().is_some_and(|n| n.to_string_lossy() == r.name)) {
                entries.push(Entry::remote(r));
            }
        }
        self.entries = entries;
        self.sel = keep.and_then(|k| self.entries.iter().position(|e| e.dir == k)).unwrap_or(0);
        self.scanned = true;
    }

    fn rescan(&mut self, ctx: &mut Ctx) {
        self.scan();
        self.fetch_remote(ctx);
        ctx.status(&format!("{} tapes · asking Mabel for hers", self.entries.len()));
    }

    fn start(&mut self, ctx: &mut Ctx) {
        let Some(entry) = self.entries.get(self.sel) else { return };
        if entry.kind == Kind::Remote {
            let name = entry.key();
            self.download(ctx, name, true);
            return;
        }
        let info = match &entry.info {
            Ok(i) => i.clone(),
            Err(e) => {
                ctx.error("TAPES", e);
                return;
            }
        };
        let wasm = match std::fs::read(&entry.wasm) {
            Ok(w) => w,
            Err(_) if entry.kind == Kind::Dev => {
                ctx.error("TAPES", "Not built yet: cargo tape (in the tape's folder)");
                return;
            }
            Err(e) => {
                ctx.error("TAPES", &format!("{}: {e}", entry.wasm.display()));
                return;
            }
        };
        self.last_run = Some(entry.dir.clone());
        let place = mabel_console::place(info.screen, self.area);
        let opts = Options { w: place.w, h: place.h, root: entry.dir.clone(), rate: self.rate, save_key: entry.key() };
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
                    console: false,
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
        run.console = false;
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

    fn menu_choice(&mut self, choice: usize, ctx: &mut Ctx) {
        let Some(run) = &mut self.run else { return };
        match MENU[choice] {
            "resume" => Self::resume(run),
            "reset" => {
                let entry = run.entry;
                self.stop();
                self.sel = entry;
                self.start(ctx);
            }
            "screenshot" => {
                let name = self.entries.get(run.entry).map(Entry::key).unwrap_or_else(|| "tape".into());
                match screenshot(run.tape.frame(), &name) {
                    Ok(path) => ctx.toast("SCREENSHOT", &format!("saved {}", path.display())),
                    Err(e) => ctx.error("SCREENSHOT", &format!("{e:#}")),
                }
            }
            "console" => run.console = true,
            _ => self.stop(),
        }
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn render_run(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        self.advance(Instant::now());
        let Some(run) = &mut self.run else { return };
        frame.render_widget(Block::new().style(Style::new().bg(pal.bg)), area);
        let place = mabel_console::place(run.screen, area);
        if run.screen == Screen::Fill {
            run.tape.resize(place.w, place.h);
        }
        mabel_console::render(run.tape.frame(), run.tape.texts(), frame.buffer_mut(), place.cells, place.scale);

        if run.console {
            let room = area.height.saturating_sub(6) as usize;
            let mut lines: Vec<Line> = if self.console.is_empty() {
                vec![Line::styled(
                    " (nothing logged yet: log(\"…\") in the tape writes here)",
                    Style::new().fg(pal.amber_dim),
                )]
            } else {
                self.console
                    .iter()
                    .rev()
                    .take(room)
                    .rev()
                    .map(|l| Line::styled(format!(" {l}"), Style::new().fg(pal.amber)))
                    .collect()
            };
            lines.push(Line::raw(""));
            lines.push(Line::styled(" esc: back to the menu", Style::new().fg(pal.amber_dim)));
            card(frame, area, pal, "CONSOLE", lines, false, 90);
        } else if let Some(sel) = run.menu {
            let title = self.entries.get(run.entry).map(Entry::title).unwrap_or_default();
            let mut lines = vec![Line::styled(format!(" {title}"), bold(pal.beige)), Line::raw("")];
            for (i, item) in MENU.iter().enumerate() {
                let style = if i == sel { bold(pal.bg).bg(pal.amber) } else { Style::new().fg(pal.amber) };
                lines.push(Line::styled(format!("  {item}  "), style));
            }
            lines.push(Line::raw(""));
            lines.push(Line::styled(" ctrl+] or esc: resume", Style::new().fg(pal.amber_dim)));
            card(frame, area, pal, "▌▌ PAUSED", lines, false, 46);
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
            card(frame, area, pal, "TAPE CRASHED", lines, true, 46);
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
            lines.push(Line::styled(" Share one: mabel-os tape publish", Style::new().fg(pal.amber)));
            lines.push(Line::styled(" (then it shows up here on every device)", Style::new().fg(pal.amber_dim)));
        }
        let rows = inner.height as usize;
        let top = self.sel.saturating_sub(rows.saturating_sub(1));
        for (i, e) in self.entries.iter().enumerate().skip(top).take(rows) {
            let on = i == self.sel;
            let style = if on && view.inside(LIST) {
                bold(pal.bg).bg(pal.amber)
            } else if on {
                bold(pal.amber_hi)
            } else if e.kind == Kind::Remote {
                Style::new().fg(pal.amber_dim)
            } else {
                Style::new().fg(pal.amber)
            };
            let mark = if e.kind == Kind::Remote { "○" } else { "◘" };
            let mut spans = vec![Span::styled(format!(" {mark} {} ", e.title()), style)];
            match e.kind {
                Kind::Dev => spans.push(Span::styled(" DEV", Style::new().fg(pal.teal))),
                Kind::Remote => spans.push(Span::styled(" ON MABEL", Style::new().fg(pal.sand))),
                Kind::Installed if e.update => spans.push(Span::styled(" ▲ NEWER ON MABEL", Style::new().fg(pal.sand))),
                Kind::Installed => {}
            }
            if self.installing.as_deref() == Some(e.key().as_str()) {
                spans.push(Span::styled(" ↓ installing…", Style::new().fg(pal.mint)));
            }
            if e.info.is_err() {
                spans.push(Span::styled(" !", Style::new().fg(pal.red)));
            }
            lines.push(Line::from(spans));
        }
        if let Some(err) = &self.remote_error {
            lines.push(Line::raw(""));
            lines.push(Line::styled(format!(" Mabel's tapes: {err}"), Style::new().fg(pal.amber_dim)));
        }
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);

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
            let max_rows = (area.height / 3).max(4) as usize;
            let scale = (area.width as usize / cover.w.max(1)).min(max_rows * 2 / cover.h.max(1)).clamp(1, 4);
            let mut px = Pixels::new(cover.w, cover.h);
            px.cls(rgb(pal.panel));
            cover.draw(&mut px, 0, 0, 0, false, false);
            let rows = (cover.h * scale).div_ceil(2) as u16;
            let place = Rect { x: area.x + 1, y, width: area.width.saturating_sub(1), height: rows.min(area.height) };
            mabel_console::render(&px, &[], frame.buffer_mut(), place, scale);
            y += rows + 1;
        }
        let dim = Style::new().fg(pal.amber_dim);
        let mut lines = vec![];
        match &e.info {
            Ok(info) => {
                lines.push(Line::styled(format!(" {}", info.title), bold(pal.beige)));
                if !info.author.is_empty() {
                    lines.push(Line::styled(format!(" by {}", info.author), dim));
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
                lines.push(Line::styled(format!(" last played: {when}"), dim));
                let screen = match info.screen {
                    Screen::Fill => "fills the screen",
                    Screen::Fixed => "128×72",
                };
                lines.push(Line::styled(format!(" {screen} · {} fps", info.fps), dim));
                match e.kind {
                    Kind::Dev => {
                        let built = if e.wasm.exists() { "built" } else { "not built yet: cargo tape" };
                        lines.push(Line::styled(
                            format!(" DEV · {} · {built}", e.dir.display()),
                            Style::new().fg(pal.teal),
                        ));
                    }
                    Kind::Remote => {
                        lines.push(Line::styled(" ON MABEL: not on this device yet", Style::new().fg(pal.sand)));
                    }
                    Kind::Installed if e.update => {
                        lines.push(Line::styled(" ▲ Mabel has a newer version: u updates", Style::new().fg(pal.sand)));
                    }
                    Kind::Installed => {}
                }
                lines.push(Line::raw(""));
                let action = if e.kind == Kind::Remote { " enter: install and play" } else { " enter: play" };
                lines.push(Line::styled(action, Style::new().fg(pal.amber)));
            }
            Err(err) => lines.push(Line::styled(format!(" {err}"), Style::new().fg(pal.red))),
        }
        let is_last = self.run.is_none() && !self.console.is_empty() && Some(&e.dir) == self.last_run.as_ref();
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
}

fn rgb(c: ratatui::style::Color) -> u32 {
    match c {
        ratatui::style::Color::Rgb(r, g, b) => (r as u32) << 16 | (g as u32) << 8 | b as u32,
        _ => 0,
    }
}

/// A centred card over the game (the pause menu, the console, the crash), `width` wide at most.
fn card(frame: &mut Frame, area: Rect, pal: &Palette, title: &str, lines: Vec<Line>, error: bool, width: u16) {
    let w = width.min(area.width);
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

    fn start(&mut self, ctx: &mut Ctx) {
        self.fetch_remote(ctx);
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        if !self.scanned {
            self.scan();
        }
        self.area = area;
        if self.run.is_some() {
            self.render_run(frame, area, view.pal);
            return vec![area];
        }
        self.render_library(frame, area, view)
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(note) = msg.downcast::<Note>() else { return };
        match *note {
            Note::Remote(Ok(list)) => {
                self.remote = Some(list);
                self.remote_error = None;
                self.scan();
            }
            Note::Remote(Err(e)) => {
                self.remote_error = Some(if e.contains("403") {
                    "this device's token has no tapes scope".into()
                } else {
                    "Mabel isn't answering".into()
                });
            }
            Note::Installed { name, result, play } => {
                self.installing = None;
                match result.map_err(anyhow::Error::msg).and_then(|bytes| install(&name, &bytes)) {
                    Ok(dir) => {
                        ctx.toast("TAPES", &format!("◘ {name} installed"));
                        ctx.status("");
                        self.scan();
                        if let Some(i) = self.entries.iter().position(|e| e.dir == dir) {
                            self.sel = i;
                            if play && self.run.is_none() {
                                self.start(ctx);
                            }
                        }
                    }
                    Err(e) => ctx.error("TAPES", &format!("couldn't install {name}: {e:#}")),
                }
            }
        }
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if let Some(run) = &mut self.run {
            if run.console {
                if key.kind != KeyEventKind::Release && matches!(key.code, KeyCode::Esc | KeyCode::Enter) {
                    run.console = false;
                }
                return Outcome::Used;
            }
            if let Some(sel) = &mut run.menu {
                if key.kind == KeyEventKind::Release {
                    return Outcome::Used;
                }
                match (keys::ch(&key), key.code) {
                    (Some('j'), _) | (_, KeyCode::Down) => *sel = (*sel + 1) % MENU.len(),
                    (Some('k'), _) | (_, KeyCode::Up) => *sel = (*sel + MENU.len() - 1) % MENU.len(),
                    (_, KeyCode::Esc) => Self::resume(run),
                    (_, KeyCode::Enter) => {
                        let choice = *sel;
                        self.menu_choice(choice, ctx);
                    }
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
            (_, KeyCode::Enter) => self.start(ctx),
            (Some('u'), _) => match self.entries.get(self.sel) {
                Some(e) if e.update => {
                    let name = e.key();
                    self.download(ctx, name, false);
                }
                _ => ctx.status("nothing to update: Mabel has the same version"),
            },
            (Some('r'), _) => self.rescan(ctx),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::is(&key, 'r') && self.run.is_none() {
            self.rescan(ctx);
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
            (e.kind == Kind::Dev && mtime(&e.wasm).is_some_and(|t| Some(t) != run.built)).then_some(run.entry)
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
            self.entries[entry] = Entry::read(&self.entries[entry].dir.clone(), Kind::Dev);
            self.sel = entry;
            self.start(ctx);
            ctx.toast("TAPES", "↻ rebuilt: the tape started again with the new code");
        }
    }
}
