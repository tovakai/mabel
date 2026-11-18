//! MABEL-OS's state: the channels, where the keyboard is, and the casing around them.
//! Key routing follows the Python app: channel keys and power off first, then the section
//! you're inside, then nav mode (h j k l, i/a/enter, esc).

use std::time::{Duration, Instant};

use crossterm::event::{Event, KeyCode, KeyEvent, KeyEventKind, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::widgets::Block;
use tokio::sync::mpsc::UnboundedSender;

use crate::api::Mabel;
use crate::channel::{self, Channel, Ctx, Msg, Outcome, View};
use crate::config::Config;
use crate::keys;
use crate::nav::{self, Focus};
use crate::palette::Palette;
use crate::ui::{self, Toast};

/// Wide = the PC (as designed); narrow = the 7" cyberdeck (1024×600 ≈ 128×37): stacked panels.
pub const NARROW_BELOW: u16 = 140;
const TOAST_FOR: Duration = Duration::from_secs(5);

pub struct App {
    pub cfg: Config,
    pub pal: Palette,
    pub api: Mabel,
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
    /// The help card: whose keys, and how far it's scrolled.
    help: Option<(&'static str, u16)>,
    toasts: Vec<Toast>,
    status: String,
    link: Option<bool>,
    /// Draw again straight away (the focus changed during the last draw).
    pub redraw: bool,
    pub quit: bool,
    /// ctrl+z: the main loop hands the terminal to a plain shell.
    pub shell: bool,
}

impl App {
    pub fn new(cfg: Config, tx: UnboundedSender<Msg>) -> anyhow::Result<Self> {
        let pal = Palette::from_config(&cfg.raw);
        let api = Mabel::new(&cfg.url, &cfg.token, &cfg.preset)?;
        let channels = channel::all(&cfg);
        Ok(App {
            cfg,
            pal,
            api,
            tx,
            channels,
            active: 0,
            focus: Focus::Nav(None),
            sections: vec![],
            tab_row: Rect::default(),
            narrow: false,
            settle: true,
            help: None,
            toasts: vec![],
            status: String::new(),
            link: None,
            redraw: false,
            quit: false,
            shell: false,
        })
    }

    /// Background work at start: does Mabel answer with this token?
    pub fn start(&self) {
        let (api, tx) = (self.api.clone(), self.tx.clone());
        tokio::spawn(async move {
            let result = api.sessions().await.map(|s| s.len()).map_err(|e| e.to_string());
            let _ = tx.send(Msg::Link(result));
        });
    }

    fn host(&self) -> &str {
        self.cfg.url.split_once("//").map_or(&self.cfg.url, |(_, h)| h)
    }

    pub fn toast(&mut self, title: &str, text: &str) {
        self.toasts.push(Toast { title: title.into(), text: text.into(), until: Instant::now() + TOAST_FOR });
        if self.toasts.len() > 4 {
            self.toasts.remove(0);
        }
    }

    pub fn set_status(&mut self, text: &str) {
        self.status = text.into();
    }

    // ── events ───────────────────────────────────────────────────────────

    pub fn tick(&mut self) {
        let now = Instant::now();
        self.toasts.retain(|t| t.until > now);
    }

    pub fn on_msg(&mut self, msg: Msg) {
        match msg {
            Msg::Link(Ok(_)) => {
                self.link = Some(true);
                self.set_status("");
            }
            Msg::Link(Err(e)) => {
                self.link = Some(false);
                self.set_status(&format!("✖ no link to Mabel: {}", e.chars().take(80).collect::<String>()));
            }
            Msg::Toast { title, text } => self.toast(&title, &text),
            Msg::For(id, data) => {
                if let Some(ch) = self.channels.iter_mut().find(|c| c.id() == id) {
                    ch.on_msg(data);
                }
            }
        }
    }

    pub fn on_event(&mut self, event: Event) {
        match event {
            Event::Key(key) if key.kind != KeyEventKind::Release => self.on_key(key),
            Event::Mouse(mouse) => self.on_mouse(mouse),
            _ => {}
        }
    }

    fn ctx_parts(&mut self) -> (&mut Box<dyn Channel>, Ctx<'_>) {
        let ch = &mut self.channels[self.active];
        (ch, Ctx { api: &self.api, cfg: &self.cfg, tx: &self.tx })
    }

    fn on_key(&mut self, key: KeyEvent) {
        if keys::ctrl(&key, 'q') {
            self.quit = true;
            return;
        }
        if let Some((_, scroll)) = &mut self.help {
            match (keys::ch(&key), key.code) {
                (Some('?' | 'q'), _) | (_, KeyCode::Esc) => self.help = None,
                (Some('j'), _) | (_, KeyCode::Down) => *scroll += 1,
                (Some('k'), _) | (_, KeyCode::Up) => *scroll = scroll.saturating_sub(1),
                _ => {}
            }
            return;
        }
        // The channel keys work everywhere: F1–F8, and alt+1–8 for keyboards without F-keys.
        let channel = keys::alt_digit(&key)
            .or(keys::fkey(&key).map(usize::from))
            .filter(|n| (1..=self.channels.len()).contains(n));
        if let Some(n) = channel {
            self.switch(n - 1);
            return;
        }
        if keys::ctrl(&key, 'z') {
            self.shell = true;
            return;
        }
        if keys::ctrl(&key, 'p') {
            self.toast("AUDIO", "Music comes with the AUDIO port (phase 3).");
            return;
        }
        if keys::ctrl(&key, 't') {
            self.toast("COLOURS", "The colour tuner isn't ported yet. palette and [colors] in config.toml work.");
            return;
        }
        match self.focus {
            Focus::Inside(section) => self.key_inside(section, key),
            Focus::Nav(lit) => self.key_nav(lit, key),
        }
    }

    fn key_inside(&mut self, section: usize, key: KeyEvent) {
        let (ch, mut ctx) = self.ctx_parts();
        if ch.key(section, key, &mut ctx) == Outcome::Used {
            return;
        }
        // Inside, h j k l are the arrows wherever the section doesn't use the letter.
        if let Some(arrow) = keys::ch(&key).and_then(keys::arrow_for) {
            let (ch, mut ctx) = self.ctx_parts();
            ch.key(section, arrow, &mut ctx);
            return;
        }
        if key.code == KeyCode::Esc || keys::ctrl_bracket(&key) {
            self.focus = Focus::Nav(Some(section));
        } else if keys::is(&key, '?') {
            self.open_help();
        }
    }

    fn key_nav(&mut self, lit: Option<usize>, key: KeyEvent) {
        let c = keys::ch(&key);
        match (c, key.code) {
            (Some(k @ ('h' | 'j' | 'k' | 'l')), _) => match lit.filter(|&i| self.visible(i)) {
                None => self.start_nav(),
                Some(current) => {
                    if let Some(next) = nav::neighbour(current, &self.sections, k) {
                        self.focus = Focus::Nav(Some(next));
                    }
                }
            },
            (Some('i' | 'a'), _) | (_, KeyCode::Enter) => {
                if let Some(i) = lit.filter(|&i| self.visible(i)) {
                    self.focus = Focus::Inside(i);
                }
            }
            (_, KeyCode::Esc) => {
                if lit.is_none() {
                    self.start_nav();
                }
            }
            (Some('?'), _) => self.open_help(),
            _ => {
                let (ch, mut ctx) = self.ctx_parts();
                ch.nav_key(key, &mut ctx);
            }
        }
    }

    fn on_mouse(&mut self, mouse: MouseEvent) {
        let MouseEventKind::Down(MouseButton::Left) = mouse.kind else {
            return;
        };
        let (x, y) = (mouse.column, mouse.row);
        if self.help.is_some() {
            self.help = None;
            return;
        }
        if self.tab_row.contains((x, y).into()) {
            let labels: Vec<&str> = self.channels.iter().map(|c| c.tab()).collect();
            if let Some(i) =
                ui::tab_places(self.tab_row, &labels, self.active).iter().position(|(_, r)| r.contains((x, y).into()))
            {
                self.switch(i);
            }
            return;
        }
        if self.channels[self.active].click(x, y) {
            self.settle = true;
            return;
        }
        if let Some(i) = nav::hit(&self.sections, x, y) {
            self.focus = Focus::Inside(i); // clicked into it: inside now
        }
    }

    // ── channels and nav ─────────────────────────────────────────────────

    fn visible(&self, i: usize) -> bool {
        self.sections.get(i).is_some_and(|r| r.width > 0 && r.height > 0)
    }

    fn start_nav(&mut self) {
        let first = (0..self.sections.len()).find(|&i| self.visible(i));
        self.focus = Focus::Nav(first);
    }

    fn switch(&mut self, index: usize) {
        if index == self.active && self.channels[index].cycle() {
            self.settle = true; // F7 / F8 again: the next app in the suite
            return;
        }
        self.active = index;
        self.focus = Focus::Nav(None);
        self.settle = true;
    }

    fn open_help(&mut self) {
        self.help = Some((self.channels[self.active].help_id(), 0));
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

    // ── drawing ──────────────────────────────────────────────────────────

    pub fn render(&mut self, frame: &mut Frame) {
        let area = frame.area();
        let pal = self.pal;
        self.narrow = area.width < NARROW_BELOW;
        frame.render_widget(Block::new().style(Style::new().bg(pal.bg).fg(pal.amber)), area);
        let [casing, tabs, body, status, footer] = Layout::vertical([
            Constraint::Length(1),
            Constraint::Length(2),
            Constraint::Fill(1),
            Constraint::Length(1),
            Constraint::Length(1),
        ])
        .areas(area);

        let clock = chrono::Local::now().format("%a %d %b %Y  %H:%M:%S").to_string().to_uppercase();
        ui::casing(frame, casing, &pal, self.host(), self.link, &clock);
        let labels: Vec<&str> = self.channels.iter().map(|c| c.tab()).collect();
        ui::tabs(frame, tabs, &pal, &labels, self.active);
        self.tab_row = Rect { height: 1, ..tabs };

        let view = View { pal: &pal, narrow: self.narrow, focus: self.focus };
        self.sections = self.channels[self.active].render(frame, body, &view);
        // A resize can hide the section the keyboard was in (wide → narrow): back to nav mode.
        let current = match self.focus {
            Focus::Inside(i) | Focus::Nav(Some(i)) => Some(i),
            Focus::Nav(None) => None,
        };
        if self.settle {
            self.settle_focus();
        } else if current.is_some_and(|i| !self.visible(i)) {
            self.start_nav();
            self.redraw = true;
        }

        ui::statusline(frame, status, &pal, None, &self.status);
        let mut footer_keys = vec![("F1", "COMMS"), ("F2", "LIFE"), ("F3", "AUDIO"), ("F4", "INVENTORY")];
        if self.narrow {
            footer_keys.clear();
            footer_keys.push(("alt+1–8", "channels"));
        }
        footer_keys.extend([("?", "keys"), ("^p", "play/pause"), ("^q", "power off")]);
        ui::footer(frame, footer, &pal, &footer_keys);

        ui::toasts(frame, body, &pal, &self.toasts);
        if let Some((id, scroll)) = &mut self.help {
            ui::help_card(frame, area, &pal, id, scroll);
        }
    }
}
