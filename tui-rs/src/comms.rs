//! COMMS ▸ MABEL: chat with her. Chats on the left (ctrl+s), the conversation, the prompt at the
//! bottom, her portrait and state on the right.
//!
//! Her reply streams in as Markdown. Tool calls show as one dim line each; an "Allow this
//! action?" card takes y (allow once) or n (deny), or a click, and resumes the turn.

use std::time::{Duration, Instant};

use chrono::Local;
use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use futures::StreamExt;
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, List, ListItem, ListState, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState};
use serde_json::Value;

use crate::api::ChatEvent;
use crate::avatar::Avatar;
use crate::channel::{Channel, Ctx, Msg, Outcome, Request, View};
use crate::config::Config;
use crate::keys;
use crate::palette::Palette;
use crate::ui::{self, PanelState, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::{markdown, wrap};

const ID: &str = "mabel";
const LIST: usize = 0;
const LOG: usize = 1;
const PROMPT: usize = 2;

/// Keep talking this long after the last bit of text arrived.
const TALK_HOLD: Duration = Duration::from_millis(600);
/// Characters a second: talk time for a reply that lands all at once.
const READ_SPEED: f64 = 45.0;
const PLACEHOLDER: &str = "talk to Mabel…";

/// Results of background work, back in `on_msg`.
enum Got {
    Sessions { result: Result<Vec<Value>, String>, select_first: bool },
    History { generation: u64, result: Result<Vec<Value>, String> },
    Created(Result<Value, String>),
    Stream { turn: u64, event: ChatEvent },
    TurnDone { turn: u64 },
}

#[derive(Clone, Copy, PartialEq)]
enum Role {
    You,
    Mabel,
}

enum Item {
    Msg { role: Role, text: String, when: String, cache: Option<(u16, usize, Vec<Line<'static>>)> },
    Tool(String),
    Error(String),
    Approval(Value),
}

/// "12:34" from an ISO timestamp (UTC or local), or now.
fn stamp(value: Option<&str>) -> String {
    value.and_then(crate::timefmt::parse_local).unwrap_or_else(Local::now).format("%H:%M").to_string()
}

pub struct Comms {
    sessions: Vec<Value>,
    list: ListState,
    session: Option<Value>,
    title: String,
    items: Vec<Item>,
    prompt: Input,
    busy: bool,
    link: Option<bool>,
    talk_until: Instant,
    avatar: Avatar,
    /// ctrl+s: on the PC the list is shown unless toggled; on the deck hidden unless toggled.
    list_toggled: bool,
    /// Lines scrolled up from the bottom of the conversation (0 = following it).
    scroll_up: usize,
    log_page: usize,
    /// Which chat a history load is for (a quicker second pick wins).
    generation: u64,
    turn: u64,
    /// The reply being streamed, and whether any text arrived.
    reply: Option<usize>,
    got_text: bool,
    /// Where the approval buttons were drawn: (area, approval id, decision).
    buttons: Vec<(Rect, String, &'static str)>,
    rects: Vec<Rect>,
    /// VAULT's M: the next message goes with this note (name, path, content).
    pub pending_note: Option<(String, String, String)>,
}

impl Comms {
    pub fn new(cfg: &Config) -> Self {
        Comms {
            sessions: vec![],
            list: ListState::default(),
            session: None,
            title: "COMMS".into(),
            items: vec![],
            prompt: Input::new(PLACEHOLDER),
            busy: false,
            link: None,
            talk_until: Instant::now(),
            avatar: Avatar::load(&cfg.avatar, cfg.avatar_fps),
            list_toggled: false,
            scroll_up: 0,
            log_page: 10,
            generation: 0,
            turn: 0,
            reply: None,
            got_text: false,
            buttons: vec![],
            rects: vec![],
            pending_note: None,
        }
    }

    fn talk(&mut self, d: Duration) {
        self.talk_until = self.talk_until.max(Instant::now() + d);
    }

    fn list_visible(&self, narrow: bool) -> bool {
        if narrow { self.list_toggled } else { !self.list_toggled }
    }

    // ── chats ────────────────────────────────────────────────────────────

    fn load_sessions(&self, ctx: &Ctx, select_first: bool) {
        ctx.spawn(ID, move |api| async move {
            Got::Sessions { result: api.sessions().await.map_err(|e| e.to_string()), select_first }
        });
    }

    fn open_session(&mut self, session: Value, ctx: &mut Ctx) {
        self.generation += 1;
        let generation = self.generation;
        let name = session["name"].as_str().filter(|n| !n.is_empty()).unwrap_or("untitled");
        self.title = format!("COMMS ░ {}", name.to_uppercase().chars().take(40).collect::<String>());
        self.items.clear();
        self.scroll_up = 0;
        let id = session["id"].as_str().unwrap_or("").to_string();
        self.session = Some(session);
        ctx.spawn(ID, move |api| async move {
            Got::History { generation, result: api.history(&id).await.map_err(|e| e.to_string()) }
        });
    }

    fn new_session(&self, ctx: &Ctx) {
        let like = self.session.clone();
        let name = format!("Terminal {}", Local::now().format("%d %b %H:%M"));
        ctx.spawn(ID, move |api| async move {
            Got::Created(api.new_session(&name, like.as_ref()).await.map_err(|e| e.to_string()))
        });
    }

    // ── talking ──────────────────────────────────────────────────────────

    fn send(&mut self, ctx: &mut Ctx) {
        let text = self.prompt.value.trim().to_string();
        if text.is_empty() || self.busy {
            return;
        }
        if self.session.is_none() {
            ctx.toast("COMMS", "No chat open: ctrl+n starts one");
            return;
        }
        self.prompt.clear();
        if let Some((name, rel, content)) = self.pending_note.take() {
            self.prompt.placeholder = PLACEHOLDER.into();
            let body: String = content.chars().take(8000).collect();
            let full = format!("About my note “{name}” ({rel}):\n\n{body}\n\n---\n{text}");
            self.run_turn(&full, None, Some(format!("[about “{name}”] {text}")), ctx);
            return;
        }
        self.run_turn(&text, None, None, ctx);
    }

    fn run_turn(&mut self, text: &str, approval: Option<(String, String)>, shown: Option<String>, ctx: &mut Ctx) {
        let Some(session_id) = self.session.as_ref().and_then(|s| s["id"].as_str()).map(String::from) else { return };
        self.busy = true;
        self.turn += 1;
        ctx.status("◉ MABEL IS TYPING");
        if !text.is_empty() {
            self.items.push(Item::Msg {
                role: Role::You,
                text: shown.unwrap_or_else(|| text.into()),
                when: stamp(None),
                cache: None,
            });
        }
        self.items.push(Item::Msg { role: Role::Mabel, text: String::new(), when: stamp(None), cache: None });
        self.reply = Some(self.items.len() - 1);
        self.got_text = false;
        self.scroll_up = 0;

        let (api, tx, turn, text) = (ctx.api.clone(), ctx.tx.clone(), self.turn, text.to_string());
        tokio::spawn(async move {
            let approval = approval.as_ref().map(|(id, d)| (id.as_str(), d.as_str()));
            let mut stream = api.stream(&session_id, &text, approval).await;
            while let Some(event) = stream.next().await {
                if tx.send(Msg::For(ID, Box::new(Got::Stream { turn, event }))).is_err() {
                    return;
                }
            }
            let _ = tx.send(Msg::For(ID, Box::new(Got::TurnDone { turn })));
        });
    }

    fn reply_text(&mut self) -> Option<&mut String> {
        match self.reply.and_then(|i| self.items.get_mut(i)) {
            Some(Item::Msg { text, cache, .. }) => {
                *cache = None;
                Some(text)
            }
            _ => None,
        }
    }

    fn on_stream(&mut self, event: ChatEvent, ctx: &mut Ctx) {
        match event {
            ChatEvent::Delta(delta) => {
                self.got_text = true;
                if !delta.trim().is_empty() {
                    self.talk(TALK_HOLD);
                }
                if let Some(text) = self.reply_text() {
                    text.push_str(&delta);
                }
            }
            ChatEvent::Event { kind, body } => match kind.as_str() {
                "final_response" => {
                    let content = body.get("content").and_then(|c| c.as_str()).unwrap_or("").to_string();
                    if !self.got_text && !content.trim().is_empty() {
                        // It all arrived at once: say it now, about as long as it takes to read.
                        let secs = (content.chars().count() as f64 / READ_SPEED).clamp(1.5, 8.0);
                        self.talk(Duration::from_secs_f64(secs));
                    }
                    if let Some(text) = self.reply_text() {
                        *text = content;
                    }
                    self.got_text = true;
                }
                "tool_start" => {
                    let name = body.get("tool").or(body.get("name")).and_then(|t| t.as_str()).unwrap_or("tool");
                    let at = self.reply.unwrap_or(self.items.len());
                    self.items.insert(at, Item::Tool(name.into()));
                    self.reply = self.reply.map(|i| i + 1);
                }
                "tool_output" => {
                    let card =
                        body.get("ask_user").filter(|a| a["approval_id"].as_str().is_some_and(|i| !i.is_empty()));
                    if let Some(card) = card {
                        self.items.push(Item::Approval(card.clone()));
                        self.scroll_up = 0;
                        ctx.focus(LOG); // y / n answer it
                    }
                }
                "ui_control" => {
                    if let Some(command) =
                        body.get("data").and_then(|d| d.get("music_command")).filter(|c| c.is_object())
                    {
                        ctx.player.apply(command);
                        ctx.toast("♪", "Mabel put something on");
                    }
                }
                "error" => {
                    let e =
                        body.get("error").map(|e| e.as_str().map_or(e.to_string(), String::from)).unwrap_or_default();
                    self.items.push(Item::Error(e));
                }
                _ => {}
            },
        }
    }

    fn turn_done(&mut self, ctx: &mut Ctx) {
        if !self.got_text
            && let Some(i) = self.reply
            && matches!(self.items.get(i), Some(Item::Msg { role: Role::Mabel, text, .. }) if text.is_empty())
        {
            self.items.remove(i);
        }
        self.reply = None;
        self.busy = false;
        ctx.status("");
    }

    /// Answer the newest approval card.
    fn decide(&mut self, decision: &'static str, ctx: &mut Ctx) -> bool {
        if self.busy {
            return false;
        }
        let Some(i) = self.items.iter().rposition(|it| matches!(it, Item::Approval(_))) else { return false };
        let Item::Approval(card) = self.items.remove(i) else { return false };
        let id = card["approval_id"].as_str().unwrap_or("").to_string();
        self.run_turn("", Some((id, decision.into())), None, ctx);
        ctx.focus(PROMPT);
        true
    }

    fn decide_id(&mut self, id: &str, decision: &'static str, ctx: &mut Ctx) {
        if self.busy {
            return;
        }
        if let Some(i) = self.items.iter().position(|it| matches!(it, Item::Approval(c) if c["approval_id"] == id)) {
            self.items.remove(i);
            self.run_turn("", Some((id.into(), decision.into())), None, ctx);
            ctx.focus(PROMPT);
        }
    }

    // ── keys ─────────────────────────────────────────────────────────────

    /// ctrl+n and ctrl+s work everywhere in COMMS.
    fn common_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::ctrl(key, 'n') {
            self.new_session(ctx);
        } else if keys::ctrl(key, 's') {
            self.list_toggled = !self.list_toggled;
        } else {
            return Outcome::Ignored;
        }
        Outcome::Used
    }

    fn scroll(&mut self, up: isize) {
        self.scroll_up = (self.scroll_up as isize + up).max(0) as usize; // clamped when drawn
    }

    fn move_list(&mut self, by: isize) {
        if self.sessions.is_empty() {
            return;
        }
        let at = self.list.selected().unwrap_or(0) as isize + by;
        self.list.select(Some(at.clamp(0, self.sessions.len() as isize - 1) as usize));
    }

    fn open_selected(&mut self, ctx: &mut Ctx) {
        if let Some(s) = self.list.selected().and_then(|i| self.sessions.get(i)).cloned() {
            self.open_session(s, ctx);
            ctx.focus(PROMPT);
        }
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn item_lines(item: &mut Item, width: u16, pal: &Palette) -> Vec<Line<'static>> {
        match item {
            Item::Msg { role, text, when, cache } => {
                if let Some((w, len, lines)) = cache
                    && *w == width
                    && *len == text.len()
                {
                    return lines.clone();
                }
                let (head, head_style, body) = match role {
                    Role::You => ("◢ YOU", bold(pal.teal), Style::new().fg(pal.amber_hi)),
                    Role::Mabel => ("◣ MABEL", bold(pal.beige_dark), Style::new().fg(pal.amber)),
                };
                let mut lines = vec![Line::raw(""), Line::styled(format!("{head}  {when}"), head_style)];
                lines.extend(markdown::render(text, width, pal, body));
                *cache = Some((width, text.len(), lines.clone()));
                lines
            }
            Item::Tool(name) => vec![Line::styled(format!("  ▸ {name}"), Style::new().fg(pal.amber_dim))],
            Item::Error(e) => {
                let spans = [Span::styled(format!("✖ {e}"), Style::new().fg(pal.red))];
                wrap::wrap(&spans, width as usize, &[], &[Span::raw("  ")])
            }
            Item::Approval(card) => approval_lines(card, width, pal),
        }
    }

    fn render_log(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let block = ui::panel(pal, &self.title, view.panel_state(LOG));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        self.buttons.clear();
        if inner.width < 4 || inner.height == 0 {
            return;
        }
        let width = inner.width - 1; // the scrollbar's column
        let mut all: Vec<Line<'static>> = vec![];
        let mut button_rows = vec![]; // (line index, approval id)
        for item in &mut self.items {
            let lines = Self::item_lines(item, width, pal);
            if let Item::Approval(card) = item {
                button_rows.push((all.len() + lines.len() - 2, card["approval_id"].as_str().unwrap_or("").to_string()));
            }
            all.extend(lines);
        }
        let height = inner.height as usize;
        self.log_page = height.saturating_sub(2).max(1);
        let max_up = all.len().saturating_sub(height);
        self.scroll_up = self.scroll_up.min(max_up);
        let start = all.len().saturating_sub(height + self.scroll_up);
        let shown: Vec<Line> = all[start..(start + height).min(all.len())].to_vec();
        let text_area = Rect { width, ..inner };
        frame.render_widget(Paragraph::new(shown), text_area);

        for (row, id) in button_rows {
            if row >= start && row < start + height {
                let y = inner.y + (row - start) as u16;
                let (allow, deny) = button_spots();
                self.buttons.push((Rect::new(inner.x + allow.0, y, allow.1, 1), id.clone(), "approve_once"));
                self.buttons.push((Rect::new(inner.x + deny.0, y, deny.1, 1), id, "deny"));
            }
        }
        if max_up > 0 {
            let mut state = ScrollbarState::new(max_up).position(max_up - self.scroll_up);
            let bar = Scrollbar::new(ScrollbarOrientation::VerticalRight)
                .begin_symbol(None)
                .end_symbol(None)
                .track_symbol(Some(" "))
                .thumb_symbol("▐")
                .track_style(Style::new().fg(pal.amber_faint))
                .thumb_style(Style::new().fg(pal.amber_dim));
            frame.render_stateful_widget(bar, inner, &mut state);
        }
    }

    fn render_list(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let block = ui::panel(pal, "CHANNELS", view.panel_state(LIST));
        let open = self.session.as_ref().and_then(|s| s["id"].as_str());
        let items: Vec<ListItem> = self
            .sessions
            .iter()
            .map(|s| {
                let name: String =
                    s["name"].as_str().filter(|n| !n.is_empty()).unwrap_or("untitled").chars().take(26).collect();
                let style = if s["id"].as_str() == open { bold(pal.amber_hi) } else { Style::new().fg(pal.amber) };
                ListItem::new(Line::styled(name, style))
            })
            .collect();
        let list = List::new(items).block(block).highlight_style(Style::new().bg(pal.amber_faint).fg(pal.amber_hi));
        frame.render_stateful_widget(list, area, &mut self.list);
    }

    fn render_side(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let [portrait, _, state] =
            Layout::vertical([Constraint::Length(16), Constraint::Length(1), Constraint::Length(1)]).areas(area);
        let talking = view.now < self.talk_until;
        self.avatar.set_state(if talking { "talk" } else { "idle" });
        self.avatar.advance(view.now);
        let block = ui::panel(pal, "MABEL", PanelState::Idle).padding(ratatui::widgets::Padding::ZERO);
        let inner = block.inner(portrait);
        frame.render_widget(block, portrait);
        let (w, h) = self.avatar.size();
        let spot = Rect {
            x: inner.x + inner.width.saturating_sub(w) / 2,
            y: inner.y + inner.height.saturating_sub(h) / 2,
            width: w.min(inner.width),
            height: h.min(inner.height),
        };
        self.avatar.render(spot, frame.buffer_mut());

        let (text, colour) = if talking {
            ("◆ SPEAKING".to_string(), pal.amber_hi)
        } else if self.busy {
            let frames = ['◐', '◓', '◑', '◒'];
            let quarter =
                std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() / 250);
            (format!("{} THINKING", frames[(quarter % 4) as usize]), pal.teal)
        } else {
            match self.link {
                Some(false) => ("✖ LINK DOWN".into(), pal.red),
                Some(true) => ("◆ ONLINE".into(), pal.amber),
                None => ("◌ CONNECTING".into(), pal.amber_dim),
            }
        };
        frame.render_widget(Paragraph::new(Span::styled(text, bold(colour))).alignment(Alignment::Center), state);
    }
}

/// Where the two buttons sit on their line, as (x, width) from the log's left edge.
fn button_spots() -> ((u16, u16), (u16, u16)) {
    ((2, 18), (22, 12))
}

fn approval_lines(card: &Value, width: u16, pal: &Palette) -> Vec<Line<'static>> {
    let w = width.max(20) as usize;
    let border = Style::new().fg(pal.red);
    let ground = Style::new().bg(Color::Rgb(0x1e, 0x0c, 0x05));
    let inner = w - 4;
    let action = &card["action"];
    let content: String = action["content"].as_str().map_or_else(|| action["content"].to_string(), String::from);
    let what: String =
        format!("{}: {}", action["tool"].as_str().unwrap_or("?"), content.chars().take(200).collect::<String>());
    let question = card["question"].as_str().filter(|q| !q.is_empty()).unwrap_or("Allow this action?");

    let row = |spans: Vec<Span<'static>>| {
        let used: usize = spans.iter().map(|s| wrap::width(&s.content)).sum();
        let mut line = vec![Span::styled("┃ ", border)];
        line.extend(spans);
        line.push(Span::raw(" ".repeat(inner.saturating_sub(used))));
        line.push(Span::styled(" ┃", border));
        Line::from(line).style(ground)
    };
    let mut lines = vec![Line::raw(""), Line::styled(format!("┏{}┓", "━".repeat(w - 2)), border).style(ground)];
    lines.push(row(vec![Span::styled(format!("▲ {question}"), bold(pal.red))]));
    for l in wrap::wrap(&[Span::styled(what, Style::new().fg(pal.amber_dim))], inner, &[], &[]) {
        lines.push(row(l.spans));
    }
    let allow = Style::new().fg(pal.bg).bg(pal.amber).add_modifier(Modifier::BOLD);
    let deny = Style::new().fg(pal.red).bg(Color::Rgb(0x4a, 0x14, 0x08)).add_modifier(Modifier::BOLD);
    lines.push(row(vec![
        Span::styled(" y · Allow once ", allow),
        Span::raw("  "),
        Span::styled(" n · Deny ", deny),
        Span::styled("  (in the conversation)", Style::new().fg(pal.amber_dim)),
    ]));
    lines.push(Line::styled(format!("┗{}┛", "━".repeat(w - 2)), border).style(ground));
    lines
}

impl Channel for Comms {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "COMMS"
    }

    fn start(&mut self, ctx: &mut Ctx) {
        self.load_sessions(ctx, true);
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let list_w = match (self.list_visible(view.narrow), view.narrow) {
            (false, _) => 0,
            (true, true) => 26,
            (true, false) => 30,
        };
        let side_w = if area.width >= 90 { 30 } else { 0 };
        let [list, chat, side] =
            Layout::horizontal([Constraint::Length(list_w), Constraint::Fill(1), Constraint::Length(side_w)])
                .areas(area);
        let [log, prompt] = Layout::vertical([Constraint::Fill(1), Constraint::Length(3)]).areas(chat);

        if list_w > 0 {
            self.render_list(frame, list, view);
        }
        self.render_log(frame, log, view);

        let state = view.panel_state(PROMPT);
        let block = ui::panel(view.pal, "", state);
        let inner = block.inner(prompt);
        frame.render_widget(block, prompt);
        let placeholder = if state == PanelState::Lit { view.pal.amber_dim } else { view.pal.amber_faint };
        self.prompt.render(
            frame,
            inner,
            Style::new().fg(view.pal.amber_hi),
            Style::new().fg(placeholder),
            view.inside(PROMPT),
        );

        if side_w > 0 {
            self.render_side(frame, side, view);
        } else {
            frame.render_widget(Block::new(), side);
        }
        self.rects = vec![list, log, prompt];
        self.rects.clone()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.common_key(&key, ctx) == Outcome::Used {
            return Outcome::Used;
        }
        match section {
            PROMPT => match self.prompt.key(&key) {
                InputResult::Submit => {
                    self.send(ctx);
                    Outcome::Used
                }
                InputResult::Used => Outcome::Used,
                InputResult::Ignored => match key.code {
                    // The conversation scrolls from the prompt too.
                    KeyCode::PageUp => {
                        self.scroll(self.log_page as isize);
                        Outcome::Used
                    }
                    KeyCode::PageDown => {
                        self.scroll(-(self.log_page as isize));
                        Outcome::Used
                    }
                    _ => Outcome::Ignored,
                },
            },
            LOG => {
                match (keys::ch(&key), key.code) {
                    (Some('y'), _) => {
                        return if self.decide("approve_once", ctx) { Outcome::Used } else { Outcome::Ignored };
                    }
                    (Some('n'), _) => return if self.decide("deny", ctx) { Outcome::Used } else { Outcome::Ignored },
                    (Some('g'), _) | (_, KeyCode::Home) => self.scroll_up = usize::MAX,
                    (Some('G'), _) | (_, KeyCode::End) => self.scroll_up = 0,
                    (_, KeyCode::Up) => self.scroll(1),
                    (_, KeyCode::Down) => self.scroll(-1),
                    (_, KeyCode::PageUp) => self.scroll(self.log_page as isize),
                    (_, KeyCode::PageDown) | (Some(' '), _) => self.scroll(-(self.log_page as isize)),
                    (_, KeyCode::Enter) | (Some('i'), _) => ctx.focus(PROMPT),
                    _ => return Outcome::Ignored,
                }
                Outcome::Used
            }
            LIST => {
                match (keys::ch(&key), key.code) {
                    (_, KeyCode::Up) => self.move_list(-1),
                    (_, KeyCode::Down) => self.move_list(1),
                    (_, KeyCode::PageUp) => self.move_list(-10),
                    (_, KeyCode::PageDown) => self.move_list(10),
                    (Some('g'), _) | (_, KeyCode::Home) => self.list.select(Some(0)),
                    (Some('G'), _) | (_, KeyCode::End) => self.move_list(isize::MAX / 2),
                    (_, KeyCode::Enter) => self.open_selected(ctx),
                    _ => return Outcome::Ignored,
                }
                Outcome::Used
            }
            _ => Outcome::Ignored,
        }
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.common_key(&key, ctx)
    }

    fn paste(&mut self, section: usize, text: &str, _ctx: &mut Ctx) {
        if section == PROMPT {
            self.prompt.insert(text);
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        let (x, y) = (event.column, event.row);
        match (event.kind, section) {
            (MouseEventKind::Down(MouseButton::Left), Some(LOG)) => {
                let hit = self.buttons.iter().find(|(r, _, _)| r.contains((x, y).into())).cloned();
                if let Some((_, id, decision)) = hit {
                    self.decide_id(&id, decision, ctx);
                }
            }
            (MouseEventKind::Down(MouseButton::Left), Some(LIST)) => {
                let list = self.rects[LIST];
                let row = y.saturating_sub(list.y + 1) as usize + self.list.offset();
                if y > list.y && row < self.sessions.len() {
                    self.list.select(Some(row));
                    self.open_selected(ctx);
                }
            }
            (MouseEventKind::ScrollUp, Some(LOG)) => self.scroll(3),
            (MouseEventKind::ScrollDown, Some(LOG)) => self.scroll(-3),
            (MouseEventKind::ScrollUp, Some(LIST)) => self.move_list(-3),
            (MouseEventKind::ScrollDown, Some(LIST)) => self.move_list(3),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        Some(PROMPT) // a chat is for typing: straight into the prompt
    }

    fn help_id(&self) -> &'static str {
        "comms"
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Sessions { result: Ok(sessions), select_first } => {
                self.link = Some(true);
                ctx.requests.push(Request::Link(true));
                self.sessions = sessions.into_iter().take(80).collect();
                if self.list.selected().is_none_or(|i| i >= self.sessions.len()) {
                    self.list.select((!self.sessions.is_empty()).then_some(0));
                }
                if select_first
                    && self.session.is_none()
                    && let Some(first) = self.sessions.first().cloned()
                {
                    self.open_session(first, ctx);
                }
            }
            Got::Sessions { result: Err(e), .. } => {
                self.link = Some(false);
                ctx.requests.push(Request::Link(false));
                ctx.error("COMMS", &format!("Couldn't load chats: {e}"));
            }
            Got::History { generation, result } if generation == self.generation => match result {
                Ok(history) => {
                    let skip = history.len().saturating_sub(60);
                    self.items = history
                        .into_iter()
                        .skip(skip)
                        .map(|m| Item::Msg {
                            role: if m["role"] == "user" { Role::You } else { Role::Mabel },
                            text: m["content"].as_str().unwrap_or("").to_string(),
                            when: stamp(m["metadata"]["timestamp"].as_str()),
                            cache: None,
                        })
                        .collect();
                    self.scroll_up = 0;
                }
                Err(e) => ctx.error("COMMS", &format!("Couldn't open the chat: {e}")),
            },
            Got::History { .. } => {} // an older pick, overtaken
            Got::Created(Ok(created)) => {
                self.generation += 1; // a history still loading belongs to the old chat
                self.session = Some(created);
                self.title = "COMMS ░ NEW CHANNEL".into();
                self.items.clear();
                self.load_sessions(ctx, false);
                ctx.focus(PROMPT);
            }
            Got::Created(Err(e)) => ctx.error("COMMS", &format!("Couldn't start a chat: {e}")),
            Got::Stream { turn, event } if turn == self.turn => self.on_stream(event, ctx),
            Got::TurnDone { turn } if turn == self.turn => self.turn_done(ctx),
            Got::Stream { .. } | Got::TurnDone { .. } => {}
        }
    }

    fn next_wake(&self) -> Option<Instant> {
        let now = Instant::now();
        let mut wake = self.avatar.next_due();
        let mut sooner = |t: Instant| wake = Some(wake.map_or(t, |w| w.min(t)));
        if self.talk_until > now {
            sooner(self.talk_until); // back to idle when she stops talking
        }
        if self.busy {
            sooner(now + Duration::from_millis(250)); // the THINKING spinner
        }
        wake
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stamps_read_utc_and_naive_times() {
        assert_eq!(stamp(Some("2026-11-18T10:05:00")), "10:05");
        let utc = stamp(Some("2026-11-18T10:05:00Z"));
        assert_eq!(utc.len(), 5);
        assert_eq!(stamp(Some("nonsense")).len(), 5);
    }
}
