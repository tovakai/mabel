//! A channel is one tab (CH-01 COMMS … CH-08 SYS). The app owns the frame around it and the
//! nav mode; a channel draws its panels, says where its sections are, and handles keys inside.
//! A suite (suite.rs) is a channel made of several apps, each of them a channel too.
//!
//! Until a channel is ported, `Placeholder` stands in: the same panels as the Python layout
//! (wide on the PC, stacked on the deck), so h j k l, i and esc can be tried on every channel.

use std::any::Any;
use std::time::Instant;

use crossterm::event::{KeyEvent, MouseEvent};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::Line;
use ratatui::widgets::{Paragraph, Wrap};
use tokio::sync::mpsc::UnboundedSender;

use crate::api::Mabel;
use crate::config::Config;
use crate::nav::Focus;
use crate::palette::Palette;
use crate::suite::{Suite, SuiteApp};
use crate::ui;

/// What a channel needs to draw.
pub struct View<'a> {
    pub pal: &'a Palette,
    /// The 7" deck layout (under 140 columns): panels stack.
    pub narrow: bool,
    pub focus: Focus,
    pub now: Instant,
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
    pub requests: Vec<Request>,
}

impl<'a> Ctx<'a> {
    pub fn new(api: &'a Mabel, cfg: &'a Config, tx: &'a UnboundedSender<Msg>) -> Self {
        Ctx { api, cfg, tx, requests: vec![] }
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
    /// When this channel next wants a redraw by itself (an animation frame, a spinner).
    fn next_wake(&self) -> Option<Instant> {
        None
    }
    /// Twice a second, for every channel: polling and timers. `visible` = on screen now.
    fn tick(&mut self, _now: Instant, _visible: bool, _ctx: &mut Ctx) {}
}

// ── stand-ins until each channel is ported ──────────────────────────────

/// Places panels for a width; the bool is the narrow (deck) layout.
type Arrange = fn(Rect, bool) -> Vec<Rect>;

pub struct Placeholder {
    id: &'static str,
    tab: &'static str,
    phase: u8,
    panels: &'static [(&'static str, &'static str)],
    arrange: Arrange,
    lands: Option<usize>,
}

impl Placeholder {
    fn new(
        id: &'static str,
        tab: &'static str,
        phase: u8,
        panels: &'static [(&'static str, &'static str)],
        arrange: Arrange,
    ) -> Self {
        Placeholder { id, tab, phase, panels, arrange, lands: None }
    }

    fn lands(mut self, section: usize) -> Self {
        self.lands = Some(section);
        self
    }
}

impl Channel for Placeholder {
    fn id(&self) -> &'static str {
        self.id
    }

    fn tab(&self) -> &'static str {
        self.tab
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        let rects = (self.arrange)(area, view.narrow);
        for (i, (rect, (title, what))) in rects.iter().zip(self.panels.iter()).enumerate() {
            let block = ui::panel(pal, title, view.panel_state(i))
                .title_bottom(Line::from(format!(" phase {} ", self.phase)).right_aligned());
            let text = vec![
                Line::styled(*what, Style::new().fg(pal.amber)),
                Line::raw(""),
                Line::styled("not ported yet: the Python MABEL-OS (mabel) has it.", Style::new().fg(pal.amber_dim)),
            ];
            frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: true }).block(block), *rect);
        }
        rects
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        self.lands
    }
}

fn split_h(area: Rect, c: &[Constraint]) -> Vec<Rect> {
    Layout::horizontal(c.to_vec()).split(area).to_vec()
}

fn split_v(area: Rect, c: &[Constraint]) -> Vec<Rect> {
    Layout::vertical(c.to_vec()).split(area).to_vec()
}

use Constraint::{Fill, Length};

fn boxed(c: impl Channel + 'static) -> Box<dyn Channel> {
    Box::new(c)
}

/// The eight channels, in tab order.
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
            ],
        )),
        boxed(crate::life::Life::new()),
        boxed(Placeholder::new(
            "audio",
            "AUDIO",
            3,
            &[("LIBRARY", "search, albums, artists, playlists, offline"), ("DECK", "cover, now playing, the queue")],
            |a, narrow| if narrow { split_v(a, &[Fill(1), Length(13)]) } else { split_h(a, &[Fill(3), Fill(2)]) },
        )),
        boxed(Placeholder::new(
            "inventory",
            "INVENTORY",
            5,
            &[("INVENTORY", "the Gremlin Index: rooms, places, items"), ("ITEM", "the open item's card")],
            |a, narrow| if narrow { split_v(a, &[Fill(3), Fill(2)]) } else { split_h(a, &[Fill(3), Fill(2)]) },
        )),
        boxed(Placeholder::new(
            "vault",
            "VAULT",
            4,
            &[
                ("VAULT", "folders and notes"),
                ("NOTE", "read, or edit vim-style"),
                ("OUTLINE · LINKS", "headings and links of the note"),
            ],
            |a, narrow| {
                if narrow {
                    let c = split_h(a, &[Length(28), Fill(1)]);
                    vec![c[0], c[1], Rect::default()]
                } else {
                    split_h(a, &[Length(32), Fill(1), Length(36)])
                }
            },
        )),
        boxed(Placeholder::new("term", "TERM", 4, &[("TERM", "shells in tabs: vim, htop, ssh all work")], |a, _| {
            vec![a]
        })),
        boxed(Suite::new(
            "creative",
            "CREATIVE",
            "F7",
            [
                ("pixel", "PIXEL", "sprites and animation"),
                ("tracker", "TRACKER", "chip music + MIDI · song → chain → phrase"),
            ]
            .into_iter()
            .map(|(key, label, hint)| {
                let page = Placeholder::new(
                    key,
                    "CREATIVE",
                    5,
                    &[("CANVAS", "the app's main view"), ("TOOLS", "its side panel")],
                    |a, _| {
                        let c = split_h(a, &[Length(24), Fill(1)]);
                        vec![c[1], c[0]]
                    },
                )
                .lands(0);
                (app(key, label, hint), boxed(page))
            })
            .collect(),
        )),
        boxed(Suite::new("sys", "SYS", "F8", sys_apps)),
    ]
}
