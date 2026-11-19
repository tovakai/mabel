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

use crate::channels::audio::music::Music;
use crate::channels::audio::player::Player;
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

pub struct App {
    pub cfg: Config,
    pub pal: Palette,
    pub api: Mabel,
    pub music: Music,
    pub player: Player,
    pub offline: crate::channels::audio::offline::Shared,
    mpris: crate::channels::audio::mpris::Mpris,
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
    pub fn new(cfg: Config, tx: UnboundedSender<Msg>) -> anyhow::Result<Self> {
        let pal = Palette::from_config(&cfg.raw);
        let api = Mabel::new(&cfg.url, &cfg.token, &cfg.preset)?;
        let music = Music::new(&api, cfg.navidrome.as_ref());
        let offline = std::sync::Arc::new(std::sync::Mutex::new(crate::channels::audio::offline::Offline::load(
            crate::channels::audio::offline::root(),
        )));
        // A downloaded song plays from disk; the rest streams.
        let locate = {
            let (music, offline) = (music.clone(), offline.clone());
            std::sync::Arc::new(move |id: &str| {
                offline.lock().expect("offline").local(id).unwrap_or_else(|| music.stream_url(id))
            }) as crate::channels::audio::player::Locate
        };
        let player = Player::new(music.clone(), locate, cfg.volume, tx.clone());
        let mpris = crate::channels::audio::mpris::Mpris::start(player.clone(), tx.clone());
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

    /// The tuner's s: the colours that differ from the preset, as [colors] in config.toml.
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

    fn host(&self) -> &str {
        self.cfg.url.split_once("//").map_or(&self.cfg.url, |(_, h)| h)
    }

    pub fn toast(&mut self, title: &str, text: &str, error: bool) {
        self.toasts.push(Toast { title: title.into(), text: text.into(), error, until: Instant::now() + TOAST_FOR });
        if self.toasts.len() > 4 {
            self.toasts.remove(0);
        }
    }

    // ── events ───────────────────────────────────────────────────────────

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

    // ── updates pushed from the PC (update.rs) ───────────────────────────

    fn check_update(&mut self, quiet: bool) {
        if quiet && crate::os::update::installed().is_none() {
            return; // run from the source tree: always current
        }
        let (tx, url) = (self.tx.clone(), self.cfg.url.clone());
        tokio::spawn(async move {
            let build = crate::os::update::served(url).await;
            let _ = tx.send(Msg::For("update", Box::new(crate::os::update::Note::Served { build, quiet })));
        });
    }

    fn update_key(&mut self) {
        if self.updating {
            return;
        }
        if crate::os::update::installed().is_none() {
            self.toast("UPDATE", "This copy runs from the source tree, so it's always current.", false);
        } else if self.update_ready.is_none() {
            self.check_update(false);
        } else {
            self.install_update();
        }
    }

    fn install_update(&mut self) {
        self.updating = true;
        let (tx, url) = (self.tx.clone(), self.cfg.url.clone());
        tokio::spawn(async move {
            let (ok, tail) = crate::os::update::apply(url).await;
            let _ = tx.send(Msg::For("update", Box::new(crate::os::update::Note::Installed { ok, tail })));
        });
    }

    fn on_update(&mut self, note: crate::os::update::Note) {
        match note {
            crate::os::update::Note::Served { build, quiet } => {
                if crate::os::update::newer(crate::os::update::installed().as_deref(), build.as_deref()) {
                    if self.update_ready != build {
                        self.update_ready = build;
                        self.toast("UPDATE", "A new MABEL-OS is ready. F12 installs it and restarts.", false);
                    }
                    let idle = self.last_input.elapsed() >= crate::os::update::IDLE_BEFORE_AUTO;
                    let playing = self.player.with(|s| s.current().is_some() && !s.paused);
                    if self.cfg.auto_update && idle && !playing && !self.updating {
                        self.install_update();
                    }
                } else if !quiet {
                    let text = if build.is_some() {
                        "Already the newest build."
                    } else {
                        "Couldn't ask Mabel for the newest build."
                    };
                    self.toast("UPDATE", text, false);
                }
            }
            crate::os::update::Note::Installed { ok, tail } => {
                self.updating = false;
                if ok {
                    self.restart = true; // main() starts the new build in this terminal
                    self.quit = true;
                } else {
                    let text = if tail.is_empty() { "the installer failed".to_string() } else { tail };
                    self.toast("UPDATE FAILED", &text, true);
                }
            }
        }
    }

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

    pub fn on_event(&mut self, event: Event) {
        if matches!(event, Event::Key(_) | Event::Mouse(_)) {
            self.last_input = Instant::now();
        }
        match event {
            Event::Key(key) if key.kind != KeyEventKind::Release => self.on_key(key),
            // Releases only go to a running tape (the terminal sends them only while it runs).
            Event::Key(key) => {
                if let Focus::Inside(section) = self.focus
                    && self.channels[self.active].key_releases()
                    && self.channels[self.active].raw_keys(section)
                {
                    let active = self.active;
                    self.with_channel(active, |ch, ctx| ch.key(section, key, ctx));
                }
            }
            Event::Mouse(mouse) => self.on_mouse(mouse),
            Event::Paste(text) => {
                if let Focus::Inside(section) = self.focus {
                    let active = self.active;
                    self.with_channel(active, |ch, ctx| ch.paste(section, &text, ctx));
                }
            }
            _ => {}
        }
    }

    fn on_key(&mut self, key: KeyEvent) {
        // Inside a TERM shell or a running tape, the program gets every key but alt+1–9 and ctrl+].
        if let Focus::Inside(section) = self.focus
            && self.help.is_none()
            && self.channels[self.active].raw_keys(section)
        {
            if let Some(n) = keys::alt_digit(&key).filter(|n| (1..=self.channels.len()).contains(n)) {
                self.switch(n - 1);
            } else if keys::ctrl_bracket(&key) {
                let active = self.active;
                if !self.with_channel(active, |ch, ctx| ch.escape(section, ctx)) {
                    self.focus = Focus::Nav(Some(section));
                }
            } else {
                let active = self.active;
                self.with_channel(active, |ch, ctx| ch.key(section, key, ctx));
            }
            return;
        }
        if keys::ctrl(&key, 'q') {
            self.quit = true;
            return;
        }
        if let Some(tuner) = &mut self.tuner {
            match tuner.key(&key, &mut self.pal) {
                crate::os::tuner::Outcome::Open => {}
                crate::os::tuner::Outcome::Close => self.tuner = None,
                crate::os::tuner::Outcome::Save => self.save_colours(),
                crate::os::tuner::Outcome::Reset => {
                    let raw =
                        std::fs::read_to_string(crate::os::config::config_path()).ok().and_then(|t| t.parse().ok());
                    self.pal = Palette::from_config(raw.as_ref().unwrap_or(&self.cfg.raw));
                }
            }
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
        // A popup in the channel (pick a playlist, name it…) takes the keys first.
        if self.channels[self.active].captures_keys() {
            let (active, section) = (
                self.active,
                match self.focus {
                    Focus::Inside(i) | Focus::Nav(Some(i)) => i,
                    Focus::Nav(None) => 0,
                },
            );
            if self.with_channel(active, |ch, ctx| ch.key(section, key, ctx)) == Outcome::Used {
                return;
            }
        }
        if keys::fkey(&key) == Some(12) {
            self.update_key();
            return;
        }
        // The channel keys work everywhere: F1–F9, and alt+1–9 for keyboards without F-keys.
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
            self.player.toggle();
            return;
        }
        if keys::ctrl(&key, 't') {
            self.tuner = Some(crate::os::tuner::Tuner::new());
            return;
        }
        match self.focus {
            Focus::Inside(section) => self.key_inside(section, key),
            Focus::Nav(lit) => self.key_nav(lit, key),
        }
    }

    fn key_inside(&mut self, section: usize, key: KeyEvent) {
        let active = self.active;
        if self.with_channel(active, |ch, ctx| ch.key(section, key, ctx)) == Outcome::Used {
            return;
        }
        // Inside, h j k l are the arrows wherever the section doesn't use the letter.
        if let Some(arrow) = keys::ch(&key).and_then(keys::arrow_for) {
            self.with_channel(active, |ch, ctx| ch.key(section, arrow, ctx));
            return;
        }
        if key.code == KeyCode::Esc || keys::ctrl_bracket(&key) {
            self.focus = Focus::Nav(Some(section));
        } else if keys::is(&key, '?') {
            self.open_help();
        }
    }

    fn key_nav(&mut self, lit: Option<usize>, key: KeyEvent) {
        // Navigation's own keys always mean navigation: h j k l move, i / a / enter go in, esc,
        // ?. Every other key goes to the channel (AUDIO's space pauses, / searches…).
        match (keys::ch(&key), key.code) {
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
                let active = self.active;
                self.with_channel(active, |ch, ctx| ch.nav_key(key, ctx));
            }
        }
    }

    fn on_mouse(&mut self, mouse: MouseEvent) {
        let (x, y) = (mouse.column, mouse.row);
        let left = mouse.kind == MouseEventKind::Down(MouseButton::Left);
        if let Some(tuner) = &mut self.tuner {
            if left {
                tuner.click(y);
            }
            return;
        }
        if self.help.is_some() {
            if left {
                self.help = None;
            }
            return;
        }
        if left && self.tab_row.contains((x, y).into()) {
            let labels: Vec<&str> = self.channels.iter().map(|c| c.tab()).collect();
            let places = ui::tab_places(self.tab_row, &labels, self.active);
            if let Some(i) = places.iter().position(|(_, r)| r.contains((x, y).into())) {
                self.switch(i);
            }
            return;
        }
        let section = nav::hit(&self.sections, x, y);
        if left && let Some(i) = section {
            self.focus = Focus::Inside(i); // clicked into it: inside now
        }
        let active = self.active;
        let before = self.help_id_now();
        let used = self.with_channel(active, |ch, ctx| ch.mouse(mouse, section, ctx));
        if used == Outcome::Used && self.help_id_now() != before {
            self.settle = true; // a suite's bar was clicked: another app is on screen
        }
    }

    // ── channels and nav ─────────────────────────────────────────────────

    fn help_id_now(&self) -> &'static str {
        self.channels[self.active].help_id()
    }

    fn visible(&self, i: usize) -> bool {
        self.sections.get(i).is_some_and(|r| r.width > 0 && r.height > 0)
    }

    fn start_nav(&mut self) {
        let first = (0..self.sections.len()).find(|&i| self.visible(i));
        self.focus = Focus::Nav(first);
    }

    fn switch(&mut self, index: usize) {
        if index == self.active && self.channels[index].cycle() {
            self.settle = true; // F1 / F7 / F8 again: the next app in the suite
            return;
        }
        self.active = index;
        self.focus = Focus::Nav(None);
        self.settle = true;
    }

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
        let badge = if self.updating {
            Some("▲ UPDATING…")
        } else if self.update_ready.is_some() {
            Some("▲ UPDATE F12")
        } else {
            None
        };
        ui::casing(frame, casing, &pal, self.host(), self.link, badge, &clock);
        // A running tape: everything under the casing bar is the game.
        if self.channels[self.active].takes_screen() {
            let game = Rect { y: area.y + 1, height: area.height.saturating_sub(1), ..area };
            let view =
                View { pal: &pal, narrow: self.narrow, focus: self.focus, now: Instant::now(), player: &self.player };
            self.sections = self.channels[self.active].render(frame, game, &view);
            if self.settle {
                self.settle_focus();
            }
            ui::toasts(frame, game, &pal, &self.toasts);
            if let Some((id, scroll)) = &mut self.help {
                ui::help_card(frame, area, &pal, id, scroll);
            }
            if let Some(tuner) = &mut self.tuner {
                tuner.render(frame, area, &pal);
            }
            return;
        }
        let labels: Vec<&str> = self.channels.iter().map(|c| c.tab()).collect();
        ui::tabs(frame, tabs, &pal, &labels, self.active);
        self.tab_row = Rect { height: 1, ..tabs };

        let view =
            View { pal: &pal, narrow: self.narrow, focus: self.focus, now: Instant::now(), player: &self.player };
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

        let playing = self.player.with(|st| {
            st.current().map(|song| {
                let title = song["title"].as_str().unwrap_or("");
                (format!("{title} · {}", song["artist"].as_str().unwrap_or("")), st.paused)
            })
        });
        ui::statusline(frame, status, &pal, playing.as_ref().map(|(t, p)| (t.as_str(), *p)), &self.status);
        let mut footer_keys = vec![("F1", "COMMS"), ("F2", "LIFE"), ("F3", "AUDIO"), ("F4", "INVENTORY")];
        if self.narrow {
            footer_keys = vec![("alt+1–9", "channels")];
        }
        footer_keys.extend([("?", "keys"), ("^p", "play/pause"), ("^q", "power off")]);
        ui::footer(frame, footer, &pal, &footer_keys);

        ui::toasts(frame, body, &pal, &self.toasts);
        if let Some((id, scroll)) = &mut self.help {
            ui::help_card(frame, area, &pal, id, scroll);
        }
        if let Some(tuner) = &mut self.tuner {
            tuner.render(frame, area, &pal);
        }
    }
}
