//! A channel is one tab (CH-01 COMMS … CH-09 TAPES). The app owns the frame around it and the
//! nav mode; a channel draws its panels, says where its sections are, and handles keys inside.
//! A suite (suite.rs) is a channel made of several apps, each of them a channel too.

use std::any::Any;
use std::time::Instant;

use crossterm::event::{KeyEvent, MouseEvent};
use ratatui::Frame;
use ratatui::layout::Rect;
use tokio::sync::mpsc::UnboundedSender;

use crate::api::Mabel;
use crate::config::Config;
use crate::music::Music;
use crate::nav::Focus;
use crate::palette::Palette;
use crate::player::Player;
use crate::suite::{Suite, SuiteApp};
use crate::ui;

/// What a channel needs to draw.
pub struct View<'a> {
    pub pal: &'a Palette,
    /// The 7" deck layout (under 140 columns): panels stack.
    pub narrow: bool,
    pub focus: Focus,
    pub now: Instant,
    pub player: &'a Player,
}

impl View<'_> {
    pub fn panel_state(&self, section: usize) -> ui::PanelState {
        match self.focus {
            Focus::Nav(Some(lit)) if lit == section => ui::PanelState::Lit,
            Focus::Inside(i) if i == section => ui::PanelState::Inside,
            _ => ui::PanelState::Idle,
        }
    }

    pub fn inside(&self, section: usize) -> bool {
        self.focus == Focus::Inside(section)
    }
}

/// Messages from background work to a channel.
pub enum Msg {
    /// Data for one channel or app (by id), from a task it spawned; it downcasts in `on_msg`.
    For(&'static str, Box<dyn Any + Send>),
    /// Quit MABEL-OS (the desktop's media widget asked, over MPRIS).
    Quit,
    /// Switch to channel/app `to` and hand it this (see Request::Deliver).
    Deliver(&'static str, Box<dyn Any + Send>),
}

/// Open a note in VAULT (sent with Request::Deliver).
pub struct OpenNote {
    /// "core of truth" or "Mabel's journal".
    pub vault: String,
    pub rel: String,
    pub line: Option<usize>,
}

/// The next COMMS message goes to Mabel with this note attached (VAULT's M).
pub struct AskAbout {
    pub name: String,
    pub rel: String,
    pub content: String,
}

/// What a channel asks of the app while handling something.
pub enum Request {
    /// Keys go inside this section now.
    Focus(usize),
    /// The status strip's message ("" clears it).
    Status(String),
    Toast {
        title: String,
        text: String,
        error: bool,
    },
    /// Does Mabel answer? (Shown in the casing.)
    Link(bool),
    /// Switch to the channel (or suite app) with this id and hand it `msg` (VAULT → COMMS's
    /// "ask Mabel about this note", LIFE → VAULT's "open her dream").
    Deliver {
        to: &'static str,
        msg: Box<dyn Any + Send>,
    },
    /// Open the colour tuner (SYS ▸ AUDIO's "colour tuner" line).
    Tuner,
    /// Shut MABEL-OS down, or restart it (the same binary, so a new build takes over).
    Quit {
        restart: bool,
    },
}

/// What a channel may use while handling something: the API, the config, a way to send itself
/// results from spawned tasks, and requests to the app.
pub struct Ctx<'a> {
    pub api: &'a Mabel,
    pub cfg: &'a Config,
    pub tx: &'a UnboundedSender<Msg>,
    /// Where AUDIO's music comes from (Mabel's proxy or Navidrome).
    pub music: &'a Music,
    pub player: &'a Player,
    /// Downloaded music (AUDIO's offline library).
    pub offline: &'a crate::offline::Shared,
    /// The colours as they are now (the tuner changes them live).
    pub pal: Palette,
    pub requests: Vec<Request>,
}

impl<'a> Ctx<'a> {
    pub fn new(
        api: &'a Mabel,
        cfg: &'a Config,
        tx: &'a UnboundedSender<Msg>,
        music: &'a Music,
        player: &'a Player,
        offline: &'a crate::offline::Shared,
        pal: Palette,
    ) -> Self {
        Ctx { api, cfg, tx, music, player, offline, pal, requests: vec![] }
    }

    /// Run a future in the background; its result comes back to `on_msg` of channel `id`.
    pub fn run<T: Send + 'static>(&self, id: &'static str, fut: impl std::future::Future<Output = T> + Send + 'static) {
        let tx = self.tx.clone();
        tokio::spawn(async move {
            let _ = tx.send(Msg::For(id, Box::new(fut.await)));
        });
    }

    pub fn toast(&mut self, title: &str, text: &str) {
        self.requests.push(Request::Toast { title: title.into(), text: text.into(), error: false });
    }

    pub fn error(&mut self, title: &str, text: &str) {
        self.requests.push(Request::Toast { title: title.into(), text: text.into(), error: true });
    }

    pub fn status(&mut self, text: &str) {
        self.requests.push(Request::Status(text.into()));
    }

    pub fn focus(&mut self, section: usize) {
        self.requests.push(Request::Focus(section));
    }

    /// Run `work` in the background; its result comes back to `on_msg` of channel `id`.
    pub fn spawn<T, F>(&self, id: &'static str, work: impl FnOnce(Mabel) -> F)
    where
        T: Send + 'static,
        F: std::future::Future<Output = T> + Send + 'static,
    {
        let tx = self.tx.clone();
        let fut = work(self.api.clone());
        tokio::spawn(async move {
            let _ = tx.send(Msg::For(id, Box::new(fut.await)));
        });
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Used,
    Ignored,
}

pub trait Channel {
    /// "comms", "mabel", "life", …: help lookups and messages.
    fn id(&self) -> &'static str;
    /// The tab's name, "COMMS".
    fn tab(&self) -> &'static str;
    /// Once at start: first loads.
    fn start(&mut self, _ctx: &mut Ctx) {}
    /// Draw into `area` and return the sections' places, in a fixed order (index = section).
    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect>;
    /// A key while inside `section`.
    fn key(&mut self, _section: usize, _key: KeyEvent, _ctx: &mut Ctx) -> Outcome {
        Outcome::Ignored
    }
    /// A key in nav mode the app doesn't use itself (space, /, z… work without going in first).
    fn nav_key(&mut self, _key: KeyEvent, _ctx: &mut Ctx) -> Outcome {
        Outcome::Ignored
    }
    /// Pasted text while inside `section`.
    fn paste(&mut self, _section: usize, _text: &str, _ctx: &mut Ctx) {}
    /// A mouse event (screen coordinates). `section` is the one under the pointer; a left click
    /// has already put the keys inside it.
    fn mouse(&mut self, _event: MouseEvent, _section: Option<usize>, _ctx: &mut Ctx) -> Outcome {
        Outcome::Ignored
    }
    /// Switching here lands inside this section (COMMS: the prompt); None = nav mode.
    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        None
    }
    /// The channel's key again (F1, F7, F8): the next app of a suite. False if not a suite.
    fn cycle(&mut self) -> bool {
        false
    }
    /// Whose keys `?` shows: the app on screen for a suite.
    fn help_id(&self) -> &'static str {
        self.id()
    }
    /// Does a message for `id` belong here? (A suite takes its apps' messages.)
    fn accepts(&self, id: &str) -> bool {
        id == self.id()
    }
    fn on_msg(&mut self, _id: &str, _msg: Box<dyn Any + Send>, _ctx: &mut Ctx) {}
    /// Bring the suite app with this id on screen (a no-op for plain channels).
    fn show_app(&mut self, _id: &str) {}
    /// When this channel next wants a redraw by itself (an animation frame, a spinner).
    fn next_wake(&self) -> Option<Instant> {
        None
    }
    /// Inside this section every key is the channel's (TERM's shells, a running tape): only alt+1–9 and ctrl+]
    /// stay with MABEL-OS. F-keys and ctrl+q go to the program.
    fn raw_keys(&self, _section: usize) -> bool {
        false
    }
    /// A popup is open: every key comes here first (before channel keys and nav).
    fn captures_keys(&self) -> bool {
        false
    }
    /// ctrl+] inside a raw-keys section. True = the channel used it (TAPES: the pause menu);
    /// false = back out to the sections (TERM).
    fn escape(&mut self, _section: usize, _ctx: &mut Ctx) -> bool {
        false
    }
    /// Draw over everything below the casing bar: no tabs, status line or footer (a running tape).
    fn takes_screen(&self) -> bool {
        false
    }
    /// Wants key releases now (a running tape's "held" buttons). The terminal is asked for them
    /// only while this is true, and they reach `key` with `KeyEventKind::Release`.
    fn key_releases(&self) -> bool {
        false
    }
    /// Twice a second, for every channel: polling and timers. `visible` = on screen now.
    fn tick(&mut self, _now: Instant, _visible: bool, _ctx: &mut Ctx) {}
}

// ── the channels ─────────────────────────────────────────────────────────

fn boxed(c: impl Channel + 'static) -> Box<dyn Channel> {
    Box::new(c)
}

/// The nine channels, in tab order.
pub fn all(cfg: &Config) -> Vec<Box<dyn Channel>> {
    let app = |key, label, hint| SuiteApp { key, label, hint };
    let pal = crate::palette::Palette::from_config(&cfg.raw);
    let host = cfg.url.split_once("//").map_or(cfg.url.as_str(), |(_, h)| h).to_string();
    let sys_apps = crate::sysview::kinds(cfg.deck)
        .into_iter()
        .map(|(kind, label, hint)| {
            (app(kind.key(), label, hint), boxed(crate::sysview::Page::new(kind, cfg.deck, &host, pal)))
        })
        .collect();

    vec![
        boxed(Suite::new(
            "comms",
            "COMMS",
            "F1",
            vec![
                (app("mabel", "MABEL", "talk with her"), boxed(crate::comms::Comms::new(cfg))),
                (
                    app("inbox", "INBOX", "your chats via Beeper · WhatsApp, Signal, Telegram…"),
                    boxed(crate::inbox::Inbox::new()),
                ),
                (
                    app("course", "COURSE", "courses from the vault · Mabel as your tutor"),
                    boxed(crate::course::CourseApp::new(cfg)),
                ),
            ],
        )),
        boxed(crate::life::Life::new()),
        boxed(crate::audio::Audio::new()),
        boxed(crate::inventory::Inventory::new(&cfg.index_url, &cfg.vault)),
        boxed(crate::vaultview::VaultView::new(&cfg.vault, &cfg.journal)),
        boxed(crate::term::Term::new()),
        boxed(Suite::new(
            "creative",
            "CREATIVE",
            "F7",
            vec![
                (app("pixel", "PIXEL", "sprites and animation"), boxed(crate::pixelview::PixelView::new(cfg))),
                (
                    app("tracker", "TRACKER", "chip music + MIDI · song → chain → phrase"),
                    boxed(crate::trackerview::TrackerView::new(cfg)),
                ),
            ],
        )),
        boxed(Suite::new("sys", "SYS", "F8", sys_apps)),
        boxed(crate::tapes::Tapes::new(cfg)),
    ]
}
