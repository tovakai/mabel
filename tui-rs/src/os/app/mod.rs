//! MABEL-OS's state: the channels, where the keyboard is, and the casing around them.
//! Key routing follows the Python app: channel keys and power off first, then the section
//! you're inside, then nav mode (h j k l, i/a/enter, esc).
//!
//! This file holds the state and the plumbing; the keys are in `input.rs`, updates in
//! `updates.rs`, drawing in `draw.rs`.

/// Drawing the screen.
mod draw;
/// Keys, mouse, paste.
mod input;
/// Updates pushed from the PC.
mod updates;

use std::time::{Duration, Instant};

use crossterm::event::{Event, KeyCode, KeyEvent, KeyEventKind, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::widgets::Block;
use tokio::sync::mpsc::UnboundedSender;

use crate::channels::media::audio::music::Music;
use crate::channels::media::audio::player::Player;
use crate::os::api::Mabel;
use crate::os::channel::{self, Channel, Ctx, Msg, Outcome, Request, View};
use crate::os::config::Config;
use crate::os::keys;
use crate::os::nav::{self, Focus};
use crate::os::palette::Palette;
use crate::os::ui::{self, Toast};

/// Wide = the PC (as designed); narrow = the 7" cyberdeck (1024×600 ≈ 128×37): stacked panels.
pub const NARROW_BELOW: u16 = 140;
const TOAST_FOR: Duration = Duration::from_secs(5);

/// Everything MABEL-OS is while it runs: config, colours, the channels, focus, toasts, the
/// player. `main.rs` feeds it events; it hands them on to the channels.
pub struct App {
    pub cfg: Config,
    pub pal: Palette,
    pub api: Mabel,
    pub music: Music,
    pub player: Player,
    pub offline: crate::channels::media::audio::offline::Shared,
    mpris: crate::channels::media::audio::mpris::Mpris,
    tx: UnboundedSender<Msg>,
    channels: Vec<Box<dyn Channel>>,
    active: usize,
    focus: Focus,
    /// Where the active channel's sections were drawn last frame (nav and clicks use them).
    sections: Vec<Rect>,
    tab_row: Rect,
    narrow: bool,
    /// After a channel switch: pick the starting section once the new layout is known.
    settle: bool,
    /// ctrl+t: the colour tuner, over everything.
    tuner: Option<crate::os::tuner::Tuner>,
    /// The help card: whose keys, and how far it's scrolled.
    help: Option<(&'static str, u16)>,
    toasts: Vec<Toast>,
    status: String,
    link: Option<bool>,
    /// Draw again straight away (the focus changed during the last draw).
    pub redraw: bool,
    pub quit: bool,
    /// Start again after quitting (SYS ▸ restart MABEL-OS).
    pub restart: bool,
    /// ctrl+z: the main loop hands the terminal to a plain shell.
    pub shell: bool,
    /// F12: the build Mabel serves when it's newer than this one (update.rs), and an install running.
    update_ready: Option<String>,
    updating: bool,
    next_update_check: Instant,
    last_input: Instant,
}

impl App {
    /// Build the app: the API client, the music player (playing from disk when a song is
    /// downloaded), MPRIS, and every channel. `tx` is how background work reaches `on_msg`.
    pub fn new(cfg: Config, tx: UnboundedSender<Msg>) -> anyhow::Result<Self> {
        let pal = Palette::from_config(&cfg.raw);
        let api = Mabel::new(&cfg.url, &cfg.token, &cfg.preset)?;
        let music = Music::new(&api, cfg.navidrome.as_ref());
        let offline = std::sync::Arc::new(std::sync::Mutex::new(
            crate::channels::media::audio::offline::Offline::load(crate::channels::media::audio::offline::root()),
        ));
        // A downloaded song plays from disk; the rest streams.
        let locate = {
            let (music, offline) = (music.clone(), offline.clone());
            std::sync::Arc::new(move |id: &str| {
                offline.lock().expect("offline").local(id).unwrap_or_else(|| music.stream_url(id))
            }) as crate::channels::media::audio::player::Locate
        };
        let player = Player::new(music.clone(), locate, cfg.volume, tx.clone());
        let mpris = crate::channels::media::audio::mpris::Mpris::start(player.clone(), tx.clone());
        let channels = channel::all(&cfg);
        Ok(App {
            cfg,
            pal,
            api,
            music,
            player,
            offline,
            mpris,
            tx,
            channels,
            active: 0,
            focus: Focus::Nav(None),
            sections: vec![],
            tab_row: Rect::default(),
            narrow: false,
            settle: true,
            tuner: None,
            help: None,
            toasts: vec![],
            status: String::new(),
            link: None,
            redraw: false,
            quit: false,
            restart: false,
            shell: false,
            update_ready: None,
            updating: false,
            next_update_check: Instant::now() + crate::os::update::FIRST_CHECK,
            last_input: Instant::now(),
        })
    }

    /// Every channel's first loads (COMMS: the chat list, which also tells whether Mabel answers).
    pub fn start(&mut self) {
        for i in 0..self.channels.len() {
            self.with_channel(i, |ch, ctx| ch.start(ctx));
        }
    }

    /// Call into channel `index` with a context, then carry out what it asked for.
    fn with_channel<R>(&mut self, index: usize, f: impl FnOnce(&mut dyn Channel, &mut Ctx) -> R) -> R {
        let mut ctx = Ctx::new(&self.api, &self.cfg, &self.tx, &self.music, &self.player, &self.offline, self.pal);
        let result = f(self.channels[index].as_mut(), &mut ctx);
        let requests = ctx.requests;
        for request in requests {
            match request {
                // Only the channel on screen moves the keyboard.
                Request::Focus(section) if index == self.active => self.focus = Focus::Inside(section),
                Request::Focus(_) => {}
                Request::Status(text) => self.status = text,
                Request::Toast { title, text, error } => self.toast(&title, &text, error),
                Request::Link(ok) => self.link = Some(ok),
                Request::Deliver { to, msg } => {
                    let _ = self.tx.send(Msg::Deliver(to, msg));
                }
                Request::Tuner => self.tuner = Some(crate::os::tuner::Tuner::new()),
                Request::Quit { restart } => {
                    self.quit = true;
                    self.restart = restart;
                }
            }
        }
        result
    }

    /// The tuner's s: the colours that differ from the preset, as `[colors]` in config.toml.
    fn save_colours(&mut self) {
        let path = crate::os::config::config_path();
        let mut base = self.cfg.raw.clone();
        base.remove("colors");
        let preset = Palette::from_config(&base);
        let result = std::fs::read_to_string(&path)
            .map(|text| crate::os::tuner::save_colours(&self.pal, &text, &preset))
            .and_then(|text| std::fs::write(&path, text));
        match result {
            Ok(()) => self.toast("COLOURS", &format!("✓ colours saved to {}", path.display()), false),
            Err(e) => self.toast("COLOURS", &format!("couldn't save: {e}"), true),
        }
    }

    /// Mabel's address without "http://" (for the casing).
    fn host(&self) -> &str {
        self.cfg.url.split_once("//").map_or(&self.cfg.url, |(_, h)| h)
    }

    /// Show a card for 5 seconds (at most 4 at once; the oldest goes).
    pub fn toast(&mut self, title: &str, text: &str, error: bool) {
        self.toasts.push(Toast { title: title.into(), text: text.into(), error, until: Instant::now() + TOAST_FOR });
        if self.toasts.len() > 4 {
            self.toasts.remove(0);
        }
    }

    // ── events ───────────────────────────────────────────────────────────

    /// Twice a second: check for updates now and then, expire toasts, tick every channel.
    pub fn tick(&mut self) {
        let now = Instant::now();
        if now >= self.next_update_check {
            self.next_update_check = now + crate::os::update::CHECK_EVERY;
            self.check_update(true);
        }
        self.toasts.retain(|t| t.until > now);
        for i in 0..self.channels.len() {
            let visible = i == self.active;
            self.with_channel(i, |ch, ctx| ch.tick(now, visible, ctx));
        }
    }

    /// When the screen next needs drawing without any input (an animation frame, a spinner).
    pub fn next_wake(&self) -> Option<Instant> {
        self.channels[self.active].next_wake()
    }

    /// Should the terminal send key releases now? (A running tape on screen.)
    pub fn wants_key_releases(&self) -> bool {
        self.help.is_none() && self.tuner.is_none() && self.channels[self.active].key_releases()
    }

    /// A message from background work: hand it to the channel it's for (by id), switch
    /// channel for a Deliver, quit, or handle an update note.
    pub fn on_msg(&mut self, msg: Msg) {
        let (id, data) = match msg {
            Msg::Quit => {
                self.quit = true;
                return;
            }
            Msg::Deliver(to, data) => {
                // Another channel's request: go there, bring its app on screen, hand it over.
                if let Some(i) = self.channels.iter().position(|c| c.accepts(to)) {
                    self.switch(i);
                    self.channels[i].show_app(to);
                    self.settle = false;
                    self.with_channel(i, |ch, ctx| ch.on_msg(to, data, ctx));
                    self.redraw = true;
                }
                return;
            }
            Msg::For("update", data) => {
                if let Ok(note) = data.downcast::<crate::os::update::Note>() {
                    self.on_update(*note);
                }
                return;
            }
            Msg::For(id, data) => (id, data),
        };
        if id == "player" {
            self.mpris.changed(&self.player); // the desktop's widget follows the deck
        }
        if let Some(i) = self.channels.iter().position(|c| c.accepts(id)) {
            self.with_channel(i, |ch, ctx| ch.on_msg(id, data, ctx));
        }
    }

    // ── channels and nav ─────────────────────────────────────────────────

    /// Each tab's number (CH-nn) and name, in tab order.
    pub(super) fn tab_labels(&self) -> Vec<(usize, &'static str)> {
        self.channels.iter().enumerate().map(|(i, c)| (channel::number(c.id(), i), c.tab())).collect()
    }

    /// The tab with this number (from F-keys and alt+digits), if there is one.
    pub(super) fn channel_numbered(&self, n: usize) -> Option<usize> {
        self.channels.iter().enumerate().position(|(i, c)| channel::number(c.id(), i) == n)
    }

    /// Whose keys `?` would show now (the suite app on screen).
    fn help_id_now(&self) -> &'static str {
        self.channels[self.active].help_id()
    }

    /// Was section `i` drawn with any size? (A hidden list has a zero rect.)
    fn visible(&self, i: usize) -> bool {
        self.sections.get(i).is_some_and(|r| r.width > 0 && r.height > 0)
    }

    /// Nav mode with the first visible section lit.
    fn start_nav(&mut self) {
        let first = (0..self.sections.len()).find(|&i| self.visible(i));
        self.focus = Focus::Nav(first);
    }

    /// Go to channel `index` (the same one again on a suite: its next app).
    fn switch(&mut self, index: usize) {
        if index == self.active && self.channels[index].cycle() {
            self.settle = true; // a suite's key again (F2, F3…): its next app
            return;
        }
        self.active = index;
        self.focus = Focus::Nav(None);
        self.settle = true;
    }

    /// Open the `?` card.
    fn open_help(&mut self) {
        self.help = Some((self.help_id_now(), 0));
    }

    /// Where a channel starts once its layout is known: straight inside (COMMS: the prompt) or nav mode.
    fn settle_focus(&mut self) {
        self.settle = false;
        match self.channels[self.active].lands_inside(self.narrow).filter(|&i| self.visible(i)) {
            Some(i) => self.focus = Focus::Inside(i),
            None => self.start_nav(),
        }
        self.redraw = true;
    }
}
