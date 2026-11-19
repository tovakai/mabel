//! CH-08 SYS: the machine MABEL-OS runs on (backend: sysinfo.rs).
//!
//! On the deck (config `deck = true`) it's a suite: STATUS · NETWORK · BLUETOOTH · AUDIO · TIME.
//! Elsewhere just STATUS: a fastfetch-style card for the faux OS, plus restart / shut down.
//!
//! Every page is a menu: j/k move · enter does the line · h/l (or -/+) adjust a value · r
//! refresh · a line's own keys are shown at the bottom. Commands run on a background thread.

/// asking the system: Wi-Fi, Bluetooth, audio, time
pub mod sysinfo;

use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use tokio::sync::mpsc::UnboundedSender;

use crate::channels::sys::sysinfo::{self as si, Done, PairNews};
use crate::os::channel::{Channel, Ctx, Msg, Outcome, Request, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::wrap::width;

// SYS's code is split by job.
/// what the lines do
mod actions;
/// building each page from system info
mod pages;

use pages::build;

/// Which page this is (each is its own app in the SYS suite).
#[derive(Clone, Copy, PartialEq)]
pub enum Kind {
    Status,
    Network,
    Bluetooth,
    Audio,
    Time,
}

impl Kind {
    /// The app's id (messages and help use it).
    fn id(self) -> &'static str {
        match self {
            Kind::Status => "sys-status",
            Kind::Network => "sys-network",
            Kind::Bluetooth => "sys-bluetooth",
            Kind::Audio => "sys-audio",
            Kind::Time => "sys-time",
        }
    }

    /// The panel's title.
    fn title(self) -> &'static str {
        match self {
            Kind::Status => "STATUS",
            Kind::Network => "NETWORK",
            Kind::Bluetooth => "BLUETOOTH",
            Kind::Audio => "AUDIO + DISPLAY",
            Kind::Time => "DATE, TIME, LOCALE",
        }
    }

    /// The keys shown under the menu.
    fn hint(self) -> &'static str {
        match self {
            Kind::Status => "enter does it · r refresh",
            Kind::Network => "enter connect · r rescan",
            Kind::Bluetooth => "enter connect / disconnect · r refresh",
            Kind::Audio => "h/l volume · enter choose",
            Kind::Time => "enter change",
        }
    }

    /// Reload this often while on screen.
    fn refresh(self) -> Option<Duration> {
        match self {
            Kind::Status => Some(Duration::from_secs(5)),
            Kind::Time => Some(Duration::from_secs(1)),
            _ => None,
        }
    }
}

/// What enter (or a line's own key) does.
#[derive(Clone)]
enum Action {
    /// Restart or quit MABEL-OS itself.
    Restart,
    Quit,
    /// The machine: "suspend", "reboot" or "poweroff" (deck only; enter twice).
    Power(&'static str),
    WifiRadio(bool),
    WifiConnect(si::Network),
    WifiForget(String),
    BtPower(bool),
    BtScan,
    BtPair(si::Device),
    /// Paired without a stored key (no input gets through): remove and pair properly.
    BtRepair(si::Device),
    BtConnect(si::Device),
    BtDisconnect(si::Device),
    BtRemove(si::Device),
    Mute,
    Output(si::Output),
    Tuner,
    TimeZone,
    Ntp(bool),
    Layout,
}

/// A value h/l (or -/+) changes.
#[derive(Clone, Copy)]
enum Adjust {
    Volume,
    Brightness,
}

/// How a line is coloured: plain, live (connected, on: teal), danger (red).
#[derive(Clone, Copy, PartialEq)]
enum Look {
    Plain,
    Live,
    Danger,
}

/// One line of a page's menu: a label, a value, and what (if anything) you can do with it.
/// Pages are built with the little builder methods below:
/// `Item::new("Wi-Fi", "on").act(Action::WifiRadio(false)).look(Look::Live)`.
#[derive(Clone)]
struct Item {
    label: String,
    value: String,
    /// What enter does.
    act: Option<Action>,
    /// What h/l change.
    adjust: Option<Adjust>,
    /// The line's own keys (x forgets a network…), shown at the bottom when it's selected.
    keys: Vec<(char, Action)>,
    look: Look,
    /// A section heading, not a line you can select.
    header: bool,
}

impl Item {
    /// A plain line: a label and its value.
    fn new(label: impl Into<String>, value: impl Into<String>) -> Self {
        Item {
            label: label.into(),
            value: value.into(),
            act: None,
            adjust: None,
            keys: vec![],
            look: Look::Plain,
            header: false,
        }
    }

    /// A section heading.
    fn header(label: &str) -> Self {
        Item { header: true, ..Item::new(label, "") }
    }

    /// …that enter does `a` on.
    fn act(mut self, a: Action) -> Self {
        self.act = Some(a);
        self
    }

    /// …whose value h/l adjust.
    fn adjust(mut self, a: Adjust) -> Self {
        self.adjust = Some(a);
        self
    }

    /// …with its own key `k` doing `a`.
    fn key(mut self, k: char, a: Action) -> Self {
        self.keys.push((k, a));
        self
    }

    /// …coloured as `l`.
    fn look(mut self, l: Look) -> Self {
        self.look = l;
        self
    }

    /// Can the cursor stop here? (Headings and plain facts are skipped.)
    fn selectable(&self) -> bool {
        !self.header && (self.act.is_some() || self.adjust.is_some() || !self.keys.is_empty())
    }
}

/// A question asked in the line under the menu.
enum Ask {
    WifiPassword(String),
    TimeZone,
    Layout,
}

/// Background work reporting back.
enum Got {
    /// A page finished building.
    Built {
        items: Vec<Item>,
        top: Vec<Line<'static>>,
    },
    Done {
        result: Done,
        reload: bool,
    },
    /// Pairing progress (a PIN to type, a passkey to confirm…).
    Pairing(PairNews),
}

/// What a page's build needs from the app (it runs on another thread).
#[derive(Clone)]
struct BuildCtx {
    pal: Palette,
    deck: bool,
    link: String,
    armed: String,
}

/// One SYS page: a menu of `Item`s, rebuilt from the system in the background.
pub struct Page {
    kind: Kind,
    /// On the deck (config `deck = true`): every page, and power actions for the machine.
    deck: bool,
    /// Mabel's address, for STATUS.
    link: String,
    pal: Palette,
    /// The menu, and the lines above it (STATUS's fastfetch-style card).
    items: Vec<Item>,
    top: Vec<Line<'static>>,
    /// What the last action said.
    message: String,
    /// What's running now ("connecting…"), empty when idle: one action at a time.
    busy: String,
    /// The PIN or passkey shown while pairing a Bluetooth device.
    pair_code: Option<String>,
    /// Built at least once / a build is running.
    loaded: bool,
    loading: bool,
    /// The cursor (an index into `items`) and the first line on screen.
    index: usize,
    scroll: usize,
    /// A power action (or an input device's disconnect) waiting for its second enter.
    armed: String,
    /// The question being asked under the menu, and what's typed.
    ask: Option<Ask>,
    input: Input,
    /// When the page was last rebuilt (STATUS and TIME refresh while shown).
    last_load: Option<Instant>,
    /// On screen last tick (coming back rebuilds the page).
    was_visible: bool,
}

impl Page {
    /// A page; it builds itself the first time it's shown.
    pub fn new(kind: Kind, deck: bool, link: &str, pal: Palette) -> Self {
        Page {
            kind,
            deck,
            link: link.into(),
            pal,
            items: vec![],
            top: vec![],
            message: String::new(),
            busy: String::new(),
            pair_code: None,
            loaded: false,
            loading: false,
            index: 0,
            scroll: 0,
            armed: String::new(),
            ask: None,
            input: Input::default(),
            last_load: None,
            was_visible: false,
        }
    }

    /// Rebuild the page in the background (skipped if a build is already running).
    fn reload(&mut self, tx: &UnboundedSender<Msg>) {
        if self.loading {
            return;
        }
        self.loading = true;
        self.last_load = Some(Instant::now());
        let (kind, id, tx) = (self.kind, self.kind.id(), tx.clone());
        let bc = BuildCtx { pal: self.pal, deck: self.deck, link: self.link.clone(), armed: self.armed.clone() };
        tokio::task::spawn_blocking(move || {
            let (items, top) = build(kind, &bc);
            let _ = tx.send(Msg::For(id, Box::new(Got::Built { items, top })));
        });
    }
}

impl Page {
    /// The lines the cursor can stop on.
    fn selectable(&self) -> Vec<usize> {
        self.items.iter().enumerate().filter(|(_, it)| it.selectable()).map(|(i, _)| i).collect()
    }

    /// After a rebuild, keep the cursor on a selectable line, as close as possible to where it was.
    fn clamp(&mut self) {
        let sel = self.selectable();
        if !sel.is_empty() && !sel.contains(&self.index) {
            self.index = *sel.iter().min_by_key(|i| i.abs_diff(self.index)).unwrap_or(&0);
        }
    }

    /// The line under the menu: what's running (and the pairing PIN), else the last message,
    /// else the page's keys and the selected line's own keys.
    fn status_line(&self, pal: &Palette) -> Line<'static> {
        if !self.busy.is_empty() {
            let mut spans = vec![Span::styled(format!(" ◌ {}…", self.busy), bold(pal.teal))];
            if let Some(code) = &self.pair_code {
                spans.push(Span::styled(format!("  type {code} on it, then Enter"), bold(pal.amber_hi)));
            }
            return Line::from(spans);
        }
        if !self.message.is_empty() {
            let colour = if self.message.starts_with('✖') { pal.red } else { pal.sand };
            return Line::styled(format!(" {}", self.message), bold(colour));
        }
        let extra: Vec<String> = self
            .items
            .get(self.index)
            .map(|it| it.keys.iter().map(|(k, _)| k.to_string()).collect())
            .unwrap_or_default();
        let tail = if extra.is_empty() { String::new() } else { format!(" · {}", extra.join("  ")) };
        Line::styled(format!(" {}{tail}", self.kind.hint()), Style::new().fg(pal.amber_dim))
    }
}

impl Channel for Page {
    fn id(&self) -> &'static str {
        self.kind.id()
    }

    fn tab(&self) -> &'static str {
        "SYS"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        self.pal = *pal;
        let ask_h = if self.ask.is_some() { 3 } else { 0 };
        let [menu, ask] = Layout::vertical([Constraint::Fill(1), Constraint::Length(ask_h)]).areas(area);
        let block = ui::panel(pal, self.kind.title(), view.panel_state(0));
        let inner = block.inner(menu);
        frame.render_widget(block, menu);
        let focused = view.inside(0) && self.ask.is_none();

        let mut lines: Vec<Line> = self.top.clone();
        if !self.loaded {
            lines.push(Line::styled(" looking…", Style::new().fg(pal.amber_dim)));
        }
        let rows = (inner.height as usize).saturating_sub(lines.len() + 2).max(1);
        // Headers take two lines (a gap above): count them so the cursor stays in view.
        let line_count = |it: &Item| if it.header { 2 } else { 1 };
        if !self.items.is_empty() {
            self.index = self.index.min(self.items.len() - 1);
            self.scroll = self.scroll.min(self.index);
            let shown = |from: usize, to: usize| self.items[from..=to].iter().map(line_count).sum::<usize>();
            while self.scroll < self.index && shown(self.scroll, self.index) > rows {
                self.scroll += 1;
            }
        }
        let mut used = 0;
        for (i, it) in self.items.iter().enumerate().skip(self.scroll) {
            if used + line_count(it) > rows {
                break;
            }
            used += line_count(it);
            if it.header {
                lines.push(Line::raw(""));
                lines.push(Line::styled(it.label.clone(), bold(pal.beige)));
                continue;
            }
            let on = i == self.index && focused && it.selectable();
            let label_style = match (on, it.look) {
                (true, _) => bold(pal.bg).bg(pal.amber),
                (false, Look::Live) => bold(pal.teal),
                (false, Look::Danger) => bold(pal.red),
                (false, Look::Plain) => Style::new().fg(pal.amber),
            };
            let label = format!(" {}{}", it.label, " ".repeat(26usize.saturating_sub(width(&it.label))));
            lines.push(Line::from(vec![
                Span::styled(label, label_style),
                Span::styled(format!(" {}", it.value), Style::new().fg(if on { pal.amber_hi } else { pal.amber_dim })),
            ]));
        }
        lines.push(Line::raw(""));
        lines.push(self.status_line(pal));
        frame.render_widget(Paragraph::new(lines), inner);

        if self.ask.is_some() {
            let block = ui::panel(pal, "", ui::PanelState::Inside);
            let field = block.inner(ask);
            frame.render_widget(block, ask);
            self.input.render(
                frame,
                field,
                Style::new().fg(pal.amber_hi),
                Style::new().fg(pal.amber_dim),
                view.inside(0),
            );
        }
        vec![menu]
    }

    fn key(&mut self, _section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.ask.is_some() {
            if key.code == KeyCode::Esc {
                self.ask = None;
                return Outcome::Used;
            }
            if self.input.key(&key) == InputResult::Submit {
                self.answered(ctx);
            }
            return Outcome::Used; // typing stays in the question
        }
        let sel = self.selectable();
        let cur = self.items.get(self.index).filter(|it| it.selectable()).cloned();
        let step = |i: usize, by: isize| -> usize {
            let pos = sel.iter().position(|s| *s == i).unwrap_or(0) as isize + by;
            sel[pos.clamp(0, sel.len() as isize - 1) as usize]
        };
        match (keys::ch(&key), key.code) {
            (_, KeyCode::Down) if !sel.is_empty() => self.index = step(self.index, 1),
            (_, KeyCode::Up) if !sel.is_empty() => self.index = step(self.index, -1),
            (Some(' '), _) | (_, KeyCode::Enter) => match cur.and_then(|c| c.act) {
                Some(a) => self.perform(a, ctx),
                None => return Outcome::Ignored,
            },
            (Some('-'), _) | (_, KeyCode::Left) if cur.as_ref().is_some_and(|c| c.adjust.is_some()) => {
                let a = cur.and_then(|c| c.adjust).expect("checked");
                self.adjust(a, -1, ctx);
            }
            (Some('+'), _) | (_, KeyCode::Right) if cur.as_ref().is_some_and(|c| c.adjust.is_some()) => {
                let a = cur.and_then(|c| c.adjust).expect("checked");
                self.adjust(a, 1, ctx);
            }
            (Some('r'), _) => {
                self.message.clear();
                self.reload(ctx.tx);
            }
            (Some(c), _) => match cur.and_then(|it| it.keys.into_iter().find(|(k, _)| *k == c)) {
                Some((_, a)) => self.perform(a, ctx),
                None => return Outcome::Ignored,
            },
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn paste(&mut self, _section: usize, text: &str, _ctx: &mut Ctx) {
        if self.ask.is_some() {
            self.input.insert(text);
        }
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        Some(0)
    }

    fn tick(&mut self, now: Instant, visible: bool, ctx: &mut Ctx) {
        let appeared = visible && !self.was_visible;
        self.was_visible = visible;
        if !visible {
            return;
        }
        let due =
            self.kind.refresh().is_some_and(|every| self.last_load.is_none_or(|t| now.duration_since(t) >= every));
        if appeared || (due && self.busy.is_empty() && self.ask.is_none()) {
            self.reload(ctx.tx);
        }
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Built { items, top } => {
                self.items = items;
                self.top = top;
                self.loaded = true;
                self.loading = false;
                self.clamp();
            }
            Got::Done { result, reload } => {
                self.busy.clear();
                self.pair_code = None;
                self.message = match result {
                    Ok(m) if m.is_empty() => String::new(),
                    Ok(m) => m,
                    Err(e) => format!("✖ {e}"),
                };
                if reload {
                    self.reload(ctx.tx);
                }
            }
            Got::Pairing(PairNews::Code(code)) => self.pair_code = Some(code),
            Got::Pairing(PairNews::Step(step)) => self.busy = step,
        }
    }
}

/// SYS's apps for this machine: all five on the deck, STATUS elsewhere.
pub fn kinds(deck: bool) -> Vec<(Kind, &'static str, &'static str)> {
    let all = [
        (Kind::Status, "STATUS", "the machine and MABEL-OS"),
        (Kind::Network, "NETWORK", "Wi-Fi, wired, tailnet"),
        (Kind::Bluetooth, "BLUETOOTH", "pair and connect"),
        (Kind::Audio, "AUDIO", "sound out, volume, screen"),
        (Kind::Time, "TIME", "date, time zone, keyboard"),
    ];
    all.into_iter().take(if deck { 5 } else { 1 }).collect()
}

impl Kind {
    /// The suite app's key (`os/channel.rs` builds SYS's apps with it).
    pub fn key(self) -> &'static str {
        self.id()
    }
}

#[cfg(test)]
mod tests {
    use super::pages::bars;
    use super::*;

    #[test]
    fn signal_bars_and_selectable_lines() {
        assert_eq!(bars(10), "▂   ");
        assert_eq!(bars(80), "▂▄▆█");
        assert!(!Item::header("X").selectable());
        assert!(!Item::new("now", "12:00").selectable());
        assert!(Item::new("Wi-Fi", "on").act(Action::WifiRadio(false)).selectable());
    }
}
