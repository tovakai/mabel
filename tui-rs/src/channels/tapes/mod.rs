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

/// `mabel-os tape …`
pub mod cli;
/// .dtape files
pub mod dtape;
/// `mabel-os new-tape`
pub mod newtape;

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

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, bold};

// TAPES is split by job; the pieces add to `impl Tapes`.
/// LIBRARY: finding, installing, drawing the list
mod library;
/// running a tape
mod run;

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

/// Where installed tapes live: `~/.local/share/mabel-os/tapes/<name>/`.
pub fn installed_dir() -> PathBuf {
    data_dir().join("tapes")
}

/// When each tape was last played (shown on its card).
fn played_file() -> PathBuf {
    data_dir().join("tapes-played.json")
}

/// Which version of a tape is installed (the .dtape's sha16), written by `install`.
const SHA_FILE: &str = ".dtape-sha";

/// Install a .dtape as `~/.local/share/mabel-os/tapes/<name>`.
pub fn install(name: &str, bytes: &[u8]) -> anyhow::Result<PathBuf> {
    let dest = installed_dir().join(name);
    crate::channels::tapes::dtape::unpack(bytes, &dest)?;
    std::fs::write(dest.join(SHA_FILE), crate::channels::tapes::dtape::sha16(bytes))?;
    Ok(dest)
}

/// A tape in my repo on Mabel, as `/api/tapes` lists it.
/// A tape in my repo on Mabel, as `/api/tapes` lists it.
#[derive(Clone, Debug)]
pub struct Remote {
    pub name: String,
    pub sha: String,
    pub info: TapeInfo,
}

impl Remote {
    /// One entry of `/api/tapes` (None if it has no name).
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

/// Where a LIBRARY entry comes from.
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
    /// A tape folder on this device (its tape.toml, its wasm, its cover).
    fn read(dir: &Path, kind: Kind) -> Entry {
        let info = TapeInfo::read(dir);
        let wasm = if kind == Kind::Dev { dev_wasm(dir) } else { dir.join("tape.wasm") };
        let cover = info.as_ref().ok().and_then(|i| i.cover.as_ref()).and_then(|c| Sprite::load(&dir.join(c)).ok());
        Entry { kind, dir: dir.to_path_buf(), info, wasm, cover, update: false }
    }

    /// A tape only on Mabel (installing it would put it in `installed_dir`).
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

    /// Its folder name: what saves, last-played and the repo call it.
    fn key(&self) -> String {
        self.dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
    }

    /// Its title (the folder name if tape.toml can't be read).
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
        .map(|a| a.iter().filter_map(|v| v.as_str()).map(crate::os::config::expand).collect())
        .unwrap_or_default()
}

/// A dev tape's wasm: cargo's release build, named after the package.
pub fn dev_wasm(dir: &Path) -> PathBuf {
    let name = std::fs::read_to_string(dir.join("Cargo.toml"))
        .ok()
        .and_then(|t| t.parse::<toml::Table>().ok())
        .and_then(|t| t.get("package")?.get("name")?.as_str().map(str::to_string))
        .unwrap_or_else(|| dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
    // A binary keeps its package name as is ("first-light.wasm"); a library swaps - for _.
    let out = dir.join("target/wasm32-unknown-unknown/release");
    let plain = out.join(format!("{name}.wasm"));
    let lib = out.join(format!("{}.wasm", name.replace('-', "_")));
    if !plain.exists() && lib.exists() { lib } else { plain }
}

/// When a file last changed (how a rebuilt dev tape is noticed).
fn mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// Messages from background work (ctx.run → on_msg).
enum Note {
    Remote(Result<Vec<Remote>, String>),
    Installed { name: String, result: Result<Vec<u8>, String>, play: bool },
}

/// The tape that's running, and its frame clock.
struct Running {
    /// The tape in the wasm runner (mabel-console's host).
    tape: Tape,
    /// Which LIBRARY entry it is.
    entry: usize,
    /// Its keys → buttons (the defaults, with tape.toml's `[keys]`).
    keymap: Keymap,
    /// fill or 128×72.
    screen: Screen,
    /// Time between frames (1 / fps), when the next one is due, and when the last one ran.
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

/// The TAPES channel: the LIBRARY, and the tape running (if one is).
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
    /// An empty LIBRARY; it scans on first draw and asks Mabel for her tapes at start-up.
    pub fn new(cfg: &crate::os::config::Config) -> Self {
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
}

/// A palette colour as 0xRRGGBB (the console's colour format).
fn rgb(c: ratatui::style::Color) -> u32 {
    match c {
        ratatui::style::Color::Rgb(r, g, b) => (r as u32) << 16 | (g as u32) << 8 | b as u32,
        _ => 0,
    }
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
                                self.start_tape(ctx);
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
            (_, KeyCode::Enter) => self.start_tape(ctx),
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
            self.start_tape(ctx);
            ctx.toast("TAPES", "↻ rebuilt: the tape started again with the new code");
        }
    }
}
