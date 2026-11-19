//! CH-08 SYS: the machine MABEL-OS runs on (backend: sysinfo.rs).
//!
//! On the deck (config `deck = true`) it's a suite: STATUS · NETWORK · BLUETOOTH · AUDIO · TIME.
//! Elsewhere just STATUS: a fastfetch-style card for the faux OS, plus restart / shut down.
//!
//! Every page is a menu: j/k move · enter does the line · h/l (or -/+) adjust a value · r
//! refresh · a line's own keys are shown at the bottom. Commands run on a background thread.

use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use tokio::sync::mpsc::UnboundedSender;

use crate::channel::{Channel, Ctx, Msg, Outcome, Request, View};
use crate::keys;
use crate::palette::Palette;
use crate::sysinfo::{self as si, Done, PairNews};
use crate::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::wrap::width;

#[derive(Clone, Copy, PartialEq)]
pub enum Kind {
    Status,
    Network,
    Bluetooth,
    Audio,
    Time,
}

impl Kind {
    fn id(self) -> &'static str {
        match self {
            Kind::Status => "sys-status",
            Kind::Network => "sys-network",
            Kind::Bluetooth => "sys-bluetooth",
            Kind::Audio => "sys-audio",
            Kind::Time => "sys-time",
        }
    }

    fn title(self) -> &'static str {
        match self {
            Kind::Status => "STATUS",
            Kind::Network => "NETWORK",
            Kind::Bluetooth => "BLUETOOTH",
            Kind::Audio => "AUDIO + DISPLAY",
            Kind::Time => "DATE, TIME, LOCALE",
        }
    }

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
    Restart,
    Quit,
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

#[derive(Clone, Copy)]
enum Adjust {
    Volume,
    Brightness,
}

#[derive(Clone, Copy, PartialEq)]
enum Look {
    Plain,
    Live,
    Danger,
}

#[derive(Clone)]
struct Item {
    label: String,
    value: String,
    act: Option<Action>,
    adjust: Option<Adjust>,
    keys: Vec<(char, Action)>,
    look: Look,
    header: bool,
}

impl Item {
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

    fn header(label: &str) -> Self {
        Item { header: true, ..Item::new(label, "") }
    }

    fn act(mut self, a: Action) -> Self {
        self.act = Some(a);
        self
    }

    fn adjust(mut self, a: Adjust) -> Self {
        self.adjust = Some(a);
        self
    }

    fn key(mut self, k: char, a: Action) -> Self {
        self.keys.push((k, a));
        self
    }

    fn look(mut self, l: Look) -> Self {
        self.look = l;
        self
    }

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

enum Got {
    Built { items: Vec<Item>, top: Vec<Line<'static>> },
    Done { result: Done, reload: bool },
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

pub struct Page {
    kind: Kind,
    deck: bool,
    link: String,
    pal: Palette,
    items: Vec<Item>,
    top: Vec<Line<'static>>,
    message: String,
    busy: String,
    pair_code: Option<String>,
    loaded: bool,
    loading: bool,
    index: usize,
    scroll: usize,
    /// A power action (or an input device's disconnect) waiting for its second enter.
    armed: String,
    ask: Option<Ask>,
    input: Input,
    last_load: Option<Instant>,
    was_visible: bool,
}

impl Page {
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

    /// Run a slow action in the background, show what it said, then reload the page.
    fn run(&mut self, label: &str, ctx: &Ctx, reload: bool, f: impl FnOnce() -> Done + Send + 'static) {
        if !self.busy.is_empty() {
            return;
        }
        self.busy = label.into();
        self.message.clear();
        let (id, tx) = (self.kind.id(), ctx.tx.clone());
        tokio::task::spawn_blocking(move || {
            let result = f();
            let _ = tx.send(Msg::For(id, Box::new(Got::Done { result, reload })));
        });
    }

    fn pair(&mut self, d: si::Device, repair: bool, ctx: &Ctx) {
        if !self.busy.is_empty() {
            return;
        }
        self.busy = format!("pairing {}", d.name);
        self.pair_code = None;
        self.message.clear();
        let (id, tx) = (self.kind.id(), ctx.tx.clone());
        tokio::task::spawn_blocking(move || {
            if repair {
                let _ = si::bt_do("remove", &d.mac);
            }
            let news_tx = tx.clone();
            let result = si::bt_pair(&d.mac, move |news| {
                let _ = news_tx.send(Msg::For(id, Box::new(Got::Pairing(news))));
            });
            let _ = tx.send(Msg::For(id, Box::new(Got::Done { result, reload: true })));
        });
    }

    /// Input devices: the keyboard you type with may be this one. Ask twice.
    fn careful(&mut self, d: &si::Device, what: &str) -> bool {
        let key = format!("{what}:{}", d.mac);
        if d.icon.starts_with("input") && self.armed != key {
            self.armed = key;
            self.message = format!("{} is an input device: {what} it? enter again", d.name);
            return false;
        }
        self.armed.clear();
        true
    }

    fn perform(&mut self, action: Action, ctx: &mut Ctx) {
        match action {
            Action::Restart => ctx.requests.push(Request::Quit { restart: true }),
            Action::Quit => ctx.requests.push(Request::Quit { restart: false }),
            Action::Power(what) => {
                if self.armed != what {
                    self.armed = what.into();
                    self.reload(ctx.tx);
                    return;
                }
                self.armed.clear();
                self.run(what, ctx, true, move || si::power(what));
            }
            Action::WifiRadio(on) => self.run("switching Wi-Fi", ctx, true, move || si::set_wifi_radio(on)),
            Action::WifiConnect(net) => {
                if net.active {
                    self.message = format!("already on {}", net.ssid);
                } else if net.known || net.open() {
                    let label = format!("connecting to {}", net.ssid);
                    self.run(&label, ctx, true, move || si::wifi_connect(&net.ssid, None));
                } else {
                    self.ask(Ask::WifiPassword(net.ssid.clone()), &format!("password for {}", net.ssid), "");
                }
            }
            Action::WifiForget(ssid) => {
                let label = format!("forgetting {ssid}");
                self.run(&label, ctx, true, move || si::wifi_forget(&ssid));
            }
            Action::BtPower(on) => self.run("switching Bluetooth", ctx, true, move || si::bt_power(on)),
            Action::BtScan => self.run("scanning", ctx, true, || si::bt_scan(8)),
            Action::BtPair(d) => self.pair(d, false, ctx),
            Action::BtRepair(d) => self.pair(d, true, ctx),
            Action::BtConnect(d) => {
                let label = format!("connecting {}", d.name);
                self.run(&label, ctx, true, move || si::bt_do("connect", &d.mac));
            }
            Action::BtDisconnect(d) => {
                if self.careful(&d, "disconnect") {
                    let label = format!("disconnecting {}", d.name);
                    self.run(&label, ctx, true, move || si::bt_do("disconnect", &d.mac));
                }
            }
            Action::BtRemove(d) => {
                if self.careful(&d, "remove") {
                    let label = format!("removing {}", d.name);
                    self.run(&label, ctx, true, move || si::bt_do("remove", &d.mac));
                }
            }
            Action::Mute => self.run("muting", ctx, true, || si::Audio::detect().toggle_mute()),
            Action::Output(o) => {
                let label = format!("switching to {}", o.name);
                self.run(&label, ctx, true, move || si::Audio::detect().set_default(&o));
            }
            Action::Tuner => ctx.requests.push(Request::Tuner),
            Action::TimeZone => self.ask(Ask::TimeZone, "time zone (e.g. Stockholm, Tokyo, Europe/Berlin)", ""),
            Action::Ntp(on) => self.run("network time", ctx, true, move || si::set_ntp(on)),
            Action::Layout => self.ask(Ask::Layout, "keyboard layout (se, us, de…)", ""),
        }
    }

    fn adjust(&mut self, a: Adjust, step: i32, ctx: &Ctx) {
        match a {
            Adjust::Volume => self.run("volume", ctx, true, move || si::Audio::detect().change(5 * step)),
            Adjust::Brightness => self.run("brightness", ctx, true, move || si::set_brightness(10 * step)),
        }
    }

    fn ask(&mut self, ask: Ask, label: &str, value: &str) {
        self.input = Input::new(label);
        self.input.mask = matches!(ask, Ask::WifiPassword(_));
        self.input.set(value);
        self.ask = Some(ask);
    }

    fn answered(&mut self, ctx: &mut Ctx) {
        let text = self.input.value.trim().to_string();
        let Some(ask) = self.ask.take() else { return };
        match ask {
            Ask::WifiPassword(ssid) => {
                let label = format!("connecting to {ssid}");
                self.run(&label, ctx, true, move || si::wifi_connect(&ssid, Some(&text)));
            }
            Ask::TimeZone if !text.is_empty() => {
                self.run("time zone", ctx, true, move || {
                    let zones = si::timezones();
                    let low = text.to_lowercase();
                    let hit = zones
                        .iter()
                        .find(|z| z.to_lowercase() == low)
                        .or_else(|| zones.iter().find(|z| z.to_lowercase().contains(&low)));
                    match hit {
                        Some(z) => si::set_timezone(z),
                        None => Err(format!("no time zone like '{text}'")),
                    }
                });
            }
            Ask::Layout if !text.is_empty() => self.run("keyboard", ctx, true, move || si::set_keyboard(&text)),
            _ => {}
        }
    }

    fn selectable(&self) -> Vec<usize> {
        self.items.iter().enumerate().filter(|(_, it)| it.selectable()).map(|(i, _)| i).collect()
    }

    fn clamp(&mut self) {
        let sel = self.selectable();
        if !sel.is_empty() && !sel.contains(&self.index) {
            self.index = *sel.iter().min_by_key(|i| i.abs_diff(self.index)).unwrap_or(&0);
        }
    }

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

// ── building the pages (on a background thread) ─────────────────────────

fn build(kind: Kind, bc: &BuildCtx) -> (Vec<Item>, Vec<Line<'static>>) {
    match kind {
        Kind::Status => build_status(bc),
        Kind::Network => (build_network(), vec![]),
        Kind::Bluetooth => (build_bluetooth(), vec![]),
        Kind::Audio => (build_audio(), vec![]),
        Kind::Time => (build_time(), vec![]),
    }
}

fn build_status(bc: &BuildCtx) -> (Vec<Item>, Vec<Line<'static>>) {
    let pal = &bc.pal;
    let facts = si::facts();
    let mut rows = vec![
        ("mabel-os".to_string(), format!("2.6 · rust {}", env!("CARGO_PKG_VERSION"))),
        ("link".to_string(), bc.link.clone()),
    ];
    rows.extend(facts.rows);
    let w = rows.iter().map(|(k, _)| width(k)).max().unwrap_or(0);
    let mut info: Vec<Line<'static>> = rows
        .into_iter()
        .map(|(k, v)| {
            Line::from(vec![
                Span::styled(format!("{k}{}  ", " ".repeat(w - width(&k))), bold(pal.amber_hi)),
                Span::styled(v, Style::new().fg(pal.amber)),
            ])
        })
        .collect();
    for warn in facts.warnings {
        info.push(Line::styled(format!("⚠ {warn}"), bold(pal.red)));
    }
    info.push(Line::raw(""));
    let swatches: Vec<Color> =
        vec![pal.bg, pal.rust, pal.amber_dim, pal.amber, pal.amber_hi, pal.beige, pal.teal, pal.red];
    info.push(Line::from(swatches.into_iter().map(|c| Span::styled("███", Style::new().fg(c))).collect::<Vec<_>>()));

    let logo = crate::avatar::still_lines();
    let logo_w = logo.first().map_or(0, |l| l.width());
    let mut top = vec![];
    for i in 0..logo.len().max(info.len()) {
        let mut spans = vec![Span::raw(" ")];
        match logo.get(i) {
            Some(l) => spans.extend(l.spans.clone()),
            None => spans.push(Span::raw(" ".repeat(logo_w))),
        }
        spans.push(Span::raw("   "));
        if let Some(l) = info.get(i) {
            spans.extend(l.spans.clone());
        }
        top.push(Line::from(spans));
    }

    let mut items = vec![
        Item::header("MABEL-OS"),
        Item::new("restart MABEL-OS", "start it again (picks up changes)").act(Action::Restart),
        Item::new("shut down MABEL-OS", "quit to the shell").act(Action::Quit),
    ];
    if bc.deck {
        items.push(Item::header("THE MACHINE"));
        for (action, label) in [("suspend", "suspend"), ("reboot", "reboot"), ("poweroff", "power off")] {
            let armed = bc.armed == action;
            let item = Item::new(label, if armed { "enter again to confirm" } else { "" }).act(Action::Power(action));
            items.push(if armed { item.look(Look::Danger) } else { item });
        }
    }
    (items, top)
}

fn bars(signal: u32) -> String {
    let n = (signal / 25 + 1).clamp(1, 4) as usize;
    format!("{:<4}", "▂▄▆█".chars().take(n).collect::<String>())
}

fn build_network() -> Vec<Item> {
    let (hw, sw) = si::wifi_radio();
    let mut items = vec![Item::header("WIRED / TAILNET")];
    for (dev, kind, state) in si::wired() {
        if kind != "wifi" {
            items.push(Item::new(format!("{dev} ({kind})"), state));
        }
    }
    items.push(Item::header("WI-FI"));
    if !hw {
        items.push(Item::new("Wi-Fi hardware", "off (a hardware switch or no Wi-Fi chip)"));
        return items;
    }
    items.push(Item::new("Wi-Fi", if sw { "on" } else { "off: enter turns it on" }).act(Action::WifiRadio(!sw)));
    if sw {
        for net in si::wifi_networks() {
            let lock = if net.open() { "□" } else { "▣" };
            let state = if net.active {
                "connected"
            } else if net.known {
                "known"
            } else {
                ""
            };
            let ssid: String = net.ssid.chars().take(22).collect();
            let label = format!("{}{ssid}", if net.active { "▸ " } else { "  " });
            let mut item = Item::new(label, format!("{} {:>3}% {lock} {state}", bars(net.signal), net.signal))
                .act(Action::WifiConnect(net.clone()));
            if net.known {
                item = item.key('x', Action::WifiForget(net.ssid.clone()));
            }
            items.push(if net.active { item.look(Look::Live) } else { item });
        }
    }
    items
}

fn build_bluetooth() -> Vec<Item> {
    let st = si::bt_state();
    if !st.present {
        return vec![Item::new("no Bluetooth adapter found", "")];
    }
    let state = if st.powered {
        "on"
    } else if st.blocked {
        "off (blocked: enter unblocks and turns it on)"
    } else {
        "off"
    };
    let mut items = vec![
        Item::header("ADAPTER"),
        Item::new(format!("Bluetooth · {}", st.name), state).act(Action::BtPower(!st.powered)),
    ];
    if st.powered {
        items
            .push(Item::new("look for devices", "8 s scan · put the device in pairing mode first").act(Action::BtScan));
        items.push(Item::header("DEVICES"));
        for d in si::bt_devices() {
            // Paired but not bonded: BlueZ refuses its input (the 2026-11-19 keyboard).
            let half = d.paired && !d.bonded && d.icon.starts_with("input");
            let (state, act) = if d.connected && !half {
                ("connected", Action::BtDisconnect(d.clone()))
            } else if half {
                ("paired, not bonded: enter re-pairs", Action::BtRepair(d.clone()))
            } else if d.paired {
                ("paired", Action::BtConnect(d.clone()))
            } else {
                ("new: enter pairs", Action::BtPair(d.clone()))
            };
            let name: String = d.name.chars().take(22).collect();
            let label = format!("{}{name}", if d.connected { "▸ " } else { "  " });
            let item = Item::new(label, format!("{state} {}", d.icon)).act(act).key('x', Action::BtRemove(d.clone()));
            items.push(match (d.connected, half) {
                (_, true) => item.look(Look::Danger),
                (true, _) => item.look(Look::Live),
                _ => item,
            });
        }
    }
    items
}

fn build_audio() -> Vec<Item> {
    let audio = si::Audio::detect();
    let (vol, muted) = audio.volume();
    let bar = vol.map_or(String::new(), |v| {
        let n = (v / 10).min(10) as usize;
        format!("{}{}", "█".repeat(n), "░".repeat(10 - n))
    });
    let value = format!("{bar} {}%{}", vol.map_or("?".into(), |v| v.to_string()), if muted { "  MUTED" } else { "" });
    let mut items = vec![
        Item::header(&format!("SOUND · {}", audio.backend)),
        Item::new("volume", value).act(Action::Mute).adjust(Adjust::Volume),
    ];
    for out in audio.outputs() {
        let name: String = out.name.chars().take(24).collect();
        let label = format!("{}{name}", if out.default { "▸ " } else { "  " });
        let item = Item::new(label, if out.default { "in use" } else { "" }).act(Action::Output(out.clone()));
        items.push(if out.default { item.look(Look::Live) } else { item });
    }
    items.push(Item::header("DISPLAY"));
    match si::brightness() {
        None => items.push(Item::new("brightness", "this screen has no brightness control")),
        Some(level) => items.push(Item::new("brightness", format!("{level}%")).adjust(Adjust::Brightness)),
    }
    items.push(Item::new("colour tuner", "for screens that show colours their own way").act(Action::Tuner));
    items
}

fn build_time() -> Vec<Item> {
    let tm = si::time_state();
    let get = |k: &str| tm.iter().find(|(key, _)| key == k).map(|(_, v)| v.as_str()).unwrap_or("");
    let ntp = get("NTP") == "yes";
    let (locale, layout) = si::locale_state();
    let synced = if get("NTPSynchronized") == "yes" { " · synchronised" } else { "" };
    vec![
        Item::header("TIME"),
        Item::new("now", chrono::Local::now().format("%a %d %b %Y  %H:%M:%S").to_string()),
        Item::new(
            "time zone",
            match get("Timezone") {
                "" => "?",
                z => z,
            },
        )
        .act(Action::TimeZone),
        Item::new("network time", format!("{}{synced}", if ntp { "on" } else { "off" })).act(Action::Ntp(!ntp)),
        Item::header("LOCALE"),
        Item::new("language", locale),
        Item::new("keyboard layout", if layout.is_empty() { "?".into() } else { layout }).act(Action::Layout),
    ]
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
    pub fn key(self) -> &'static str {
        self.id()
    }
}

#[cfg(test)]
mod tests {
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
