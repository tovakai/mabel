//! A channel is one tab (CH-01 COMMS … CH-08 SYS). The app owns the frame around it and the
//! nav mode; a channel draws its panels, says where its sections are, and handles keys inside.
//!
//! Until a channel is ported, `Placeholder` stands in: the same panels as the Python layout
//! (wide on the PC, stacked on the deck), so h j k l, i and esc can be tried on every channel.

use std::any::Any;

use crossterm::event::KeyEvent;
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Wrap};
use tokio::sync::mpsc::UnboundedSender;

use crate::api::Mabel;
use crate::config::Config;
use crate::nav::Focus;
use crate::palette::Palette;
use crate::ui;

/// What a channel needs to draw.
pub struct View<'a> {
    pub pal: &'a Palette,
    /// The 7" deck layout (under 140 columns): panels stack.
    pub narrow: bool,
    pub focus: Focus,
}

impl View<'_> {
    pub fn panel_state(&self, section: usize) -> ui::PanelState {
        match self.focus {
            Focus::Nav(Some(lit)) if lit == section => ui::PanelState::Lit,
            Focus::Inside(i) if i == section => ui::PanelState::Inside,
            _ => ui::PanelState::Idle,
        }
    }
}

/// Messages from background work to the app or a channel.
pub enum Msg {
    /// Does Mabel answer with this token? Ok(number of chats) or the error.
    Link(Result<usize, String>),
    Toast {
        title: String,
        text: String,
    },
    /// Data for one channel (by id), from a task it spawned; it downcasts in `on_msg`.
    For(&'static str, Box<dyn Any + Send>),
}

/// What a channel may use while handling a key: the API, the config, a way to send itself
/// results from spawned tasks, and toasts.
pub struct Ctx<'a> {
    pub api: &'a Mabel,
    pub cfg: &'a Config,
    pub tx: &'a UnboundedSender<Msg>,
}

impl Ctx<'_> {
    pub fn toast(&self, title: &str, text: &str) {
        let _ = self.tx.send(Msg::Toast { title: title.into(), text: text.into() });
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Outcome {
    Used,
    Ignored,
}

pub trait Channel {
    /// "comms", "life", …: help lookups and messages.
    fn id(&self) -> &'static str;
    /// The tab's name, "COMMS".
    fn tab(&self) -> &'static str;
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
    fn on_msg(&mut self, _msg: Box<dyn Any + Send>) {}
    /// A mouse click (screen coordinates) the sections didn't take: suite bars, tabs inside.
    fn click(&mut self, _x: u16, _y: u16) -> bool {
        false
    }
}

// ── suites: a bar of apps over the one on screen ───────────────────────

pub struct SuiteApp {
    pub key: &'static str,
    pub label: &'static str,
    pub hint: &'static str,
}

/// The bar over a suite's apps (only drawn when it has more than one).
pub fn suite_bar(frame: &mut Frame, area: Rect, pal: &Palette, apps: &[SuiteApp], current: usize, fkey: &str) {
    let mut spans = vec![];
    for (i, app) in apps.iter().enumerate() {
        if i == current {
            spans.push(Span::styled(
                format!(" ◆ {} ", app.label),
                Style::new().fg(pal.bg).bg(pal.amber).add_modifier(Modifier::BOLD),
            ));
        } else {
            spans.push(Span::styled(
                format!("   {} ", app.label),
                Style::new().fg(pal.amber_dim).add_modifier(Modifier::BOLD),
            ));
        }
        spans.push(Span::raw("  "));
    }
    let tail = if fkey.is_empty() { String::new() } else { format!("   {fkey}: next app") };
    spans.push(Span::styled(format!("  {}{tail}", apps[current].hint), Style::new().fg(pal.amber_dim)));
    frame.render_widget(Paragraph::new(Line::from(spans)).style(Style::new().bg(pal.panel)), area);
}

/// Which suite app a click on the bar at column `x` (relative to the bar) hits.
pub fn suite_bar_hit(apps: &[SuiteApp], x: u16) -> Option<usize> {
    let mut left = 0u16;
    for (i, app) in apps.iter().enumerate() {
        let width = app.label.chars().count() as u16 + 6;
        if x >= left && x < left + width {
            return Some(i);
        }
        left += width;
    }
    None
}

// ── stand-ins until each channel is ported ──────────────────────────────

/// Panels of a not-yet-ported channel: (title, what goes there), placed by `layout`.
type Layout_ = fn(Rect, bool) -> Vec<Rect>;

pub struct Placeholder {
    id: &'static str,
    tab: &'static str,
    phase: u8,
    panels: &'static [(&'static str, &'static str)],
    layout: Layout_,
    apps: Vec<SuiteApp>,
    fkey: &'static str,
    current: usize,
    lands: Option<usize>,
    bar: Rect,
}

impl Placeholder {
    fn new(
        id: &'static str,
        tab: &'static str,
        phase: u8,
        panels: &'static [(&'static str, &'static str)],
        layout: Layout_,
    ) -> Self {
        Placeholder {
            id,
            tab,
            phase,
            panels,
            layout,
            apps: vec![],
            fkey: "",
            current: 0,
            lands: None,
            bar: Rect::default(),
        }
    }

    fn suite(mut self, fkey: &'static str, apps: Vec<SuiteApp>) -> Self {
        self.fkey = fkey;
        self.apps = apps;
        self
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
        let mut body = area;
        self.bar = Rect::default();
        if self.apps.len() > 1 {
            let [bar, rest] = Layout::vertical([Constraint::Length(1), Constraint::Fill(1)]).areas(area);
            suite_bar(frame, bar, pal, &self.apps, self.current, self.fkey);
            self.bar = bar;
            body = rest;
        }
        let rects = (self.layout)(body, view.narrow);
        let app = self.apps.get(self.current).map(|a| a.label);
        for (i, (rect, (title, what))) in rects.iter().zip(self.panels.iter()).enumerate() {
            let title = match (i, app) {
                (0, Some(label)) if self.apps.len() > 1 => format!("{label} ░ {title}"),
                _ => title.to_string(),
            };
            let block = ui::panel(pal, &title, view.panel_state(i))
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

    fn cycle(&mut self) -> bool {
        if self.apps.len() < 2 {
            return false;
        }
        self.current = (self.current + 1) % self.apps.len();
        true
    }

    fn help_id(&self) -> &'static str {
        self.apps.get(self.current).map_or(self.id, |a| a.key)
    }

    fn click(&mut self, x: u16, y: u16) -> bool {
        if self.apps.len() > 1
            && self.bar.contains((x, y).into())
            && let Some(i) = suite_bar_hit(&self.apps, x - self.bar.x)
        {
            self.current = i;
            return true;
        }
        false
    }
}

fn split_h(area: Rect, c: &[Constraint]) -> Vec<Rect> {
    Layout::horizontal(c.to_vec()).split(area).to_vec()
}

fn split_v(area: Rect, c: &[Constraint]) -> Vec<Rect> {
    Layout::vertical(c.to_vec()).split(area).to_vec()
}

use Constraint::{Fill, Length};

/// The eight channels, in tab order.
pub fn all(cfg: &Config) -> Vec<Box<dyn Channel>> {
    let app = |key, label, hint| SuiteApp { key, label, hint };
    let sys_apps = vec![
        app("status", "STATUS", "the machine and MABEL-OS"),
        app("network", "NETWORK", "Wi-Fi, wired, tailnet"),
        app("bluetooth", "BLUETOOTH", "pair and connect"),
        app("audio", "AUDIO", "sound out, volume, screen"),
        app("time", "TIME", "date, time zone, keyboard"),
    ];
    let sys_apps = if cfg.deck { sys_apps } else { sys_apps.into_iter().take(1).collect() };
    let channels = vec![
        Placeholder::new(
            "comms",
            "COMMS",
            2,
            &[
                ("COMMS", "the conversation with Mabel, streamed"),
                ("PROMPT", "write to her here"),
                ("CHANNELS", "your chats with her"),
            ],
            |a, narrow| {
                let (list, main) = if narrow {
                    (Rect::default(), a) // the chat list hides on the deck (ctrl+s shows it)
                } else {
                    let c = split_h(a, &[Length(30), Fill(1)]);
                    (c[0], c[1])
                };
                let m = split_v(main, &[Fill(1), Length(3)]);
                vec![m[0], m[1], list]
            },
        )
        .suite(
            "F1",
            vec![
                app("mabel", "MABEL", "talk with her"),
                app("inbox", "INBOX", "your chats via Beeper · WhatsApp, Signal, Telegram…"),
            ],
        )
        .lands(1),
        Placeholder::new(
            "life",
            "LIFE",
            2,
            &[
                ("SYSTEMS", "what she's doing, and why each job waits"),
                ("JOURNAL", "her last song, paper note, book and news"),
                ("LISTS", "groceries, and what's checked out of the Gremlin Index"),
                ("LAST DREAM", "her latest dream, with its picture"),
            ],
            |a, narrow| {
                if narrow {
                    let r = split_v(a, &[Fill(1), Fill(1)]);
                    let top = split_h(r[0], &[Fill(1), Fill(1)]);
                    let bottom = split_h(r[1], &[Fill(1), Fill(1)]);
                    vec![top[0], bottom[0], bottom[1], top[1]]
                } else {
                    let c = split_h(a, &[Fill(1), Fill(1), Fill(1)]);
                    let mid = split_v(c[1], &[Fill(1), Fill(1)]);
                    vec![c[0], mid[0], mid[1], c[2]]
                }
            },
        ),
        Placeholder::new(
            "audio",
            "AUDIO",
            3,
            &[("LIBRARY", "search, albums, artists, playlists, offline"), ("DECK", "cover, now playing, the queue")],
            |a, narrow| {
                if narrow { split_v(a, &[Fill(1), Length(13)]) } else { split_h(a, &[Fill(3), Fill(2)]) }
            },
        ),
        Placeholder::new(
            "inventory",
            "INVENTORY",
            5,
            &[("INVENTORY", "the Gremlin Index: rooms, places, items"), ("ITEM", "the open item's card")],
            |a, narrow| {
                if narrow { split_v(a, &[Fill(3), Fill(2)]) } else { split_h(a, &[Fill(3), Fill(2)]) }
            },
        ),
        Placeholder::new(
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
        ),
        Placeholder::new("term", "TERM", 4, &[("TERM", "shells in tabs: vim, htop, ssh all work")], |a, _| vec![a]),
        Placeholder::new(
            "creative",
            "CREATIVE",
            5,
            &[("CANVAS", "the app's main view"), ("TOOLS", "its side panel")],
            |a, _| {
                let c = split_h(a, &[Length(24), Fill(1)]);
                vec![c[1], c[0]]
            },
        )
        .suite(
            "F7",
            vec![
                app("pixel", "PIXEL", "sprites and animation"),
                app("tracker", "TRACKER", "chip music + MIDI · song → chain → phrase"),
            ],
        )
        .lands(0),
        Placeholder::new(
            "sys",
            "SYS",
            2,
            &[("MENU", "the lines you can act on"), ("DETAILS", "what the line is about")],
            |a, _| split_h(a, &[Fill(2), Fill(3)]),
        )
        .suite("F8", sys_apps)
        .lands(0),
    ];
    channels.into_iter().map(|c| Box::new(c) as Box<dyn Channel>).collect()
}
