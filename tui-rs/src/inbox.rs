//! COMMS ▸ INBOX: his Beeper chats (WhatsApp, Signal, Telegram…), relayed by Mabel
//! (/api/beeper). Beeper Desktop does the networks and encryption.
//!
//! Left: chats (unread counts, network). Right: the conversation and a compose line.
//! Chats: j/k move · enter or l opens · / filter · u unread only · , . network · r refresh.
//! Conversation: j/k pick a message · i, tab or enter write · r reply to it · e edit yours
//! react with + · m mark unread · h or esc back to the chats. Compose: enter sends · esc stops.
//! New messages are fetched every few seconds while INBOX is on screen.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use chrono::{DateTime, Local, Utc};
use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use reqwest::Method;
use serde_json::{Value, json};

use crate::channel::{Channel, Ctx, Outcome, View};
use crate::keys;
use crate::palette::Palette;
use crate::ui::{self, PanelState, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::wrap::{self, width};

const ID: &str = "inbox";
const CHATS: usize = 0;
const CONV: usize = 1;
const COMPOSE: usize = 2;

const POLL_CHATS: Duration = Duration::from_secs(8);
const POLL_OPEN: Duration = Duration::from_secs(3);
const REACTIONS: [&str; 6] = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
const IDLE_PLACEHOLDER: &str = "i or tab: write · enter sends · esc";

#[derive(Clone, Copy, PartialEq)]
enum State {
    Connecting,
    Ready,
    NoToken,
    Offline,
}

#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Idle,
    Send,
    Edit,
    React,
    Filter,
}

enum Got {
    Chats {
        generation: u64,
        accounts: Option<HashMap<String, String>>,
        result: Result<Vec<Value>, String>,
        quiet: bool,
    },
    Messages {
        chat: String,
        result: Result<Vec<Value>, String>,
        quiet: bool,
    },
    Posted {
        reload: bool,
        error: Option<String>,
    },
}

/// "14:05" today, "Tue" this week, else "03 Nov".
fn when(ts: Option<&str>) -> String {
    let Some(t) = ts.and_then(|t| DateTime::parse_from_rfc3339(t).ok()) else { return String::new() };
    let t = t.with_timezone(&Local);
    let now = Local::now();
    if t.date_naive() == now.date_naive() {
        t.format("%H:%M").to_string()
    } else if (now - t).num_days() < 7 {
        t.format("%a").to_string()
    } else {
        t.format("%d %b").to_string()
    }
}

fn items(data: Value) -> Vec<Value> {
    match data {
        Value::Array(a) => a,
        Value::Object(mut o) => match o.remove("items") {
            Some(Value::Array(a)) => a,
            _ => vec![],
        },
        _ => vec![],
    }
}

fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

fn preview(chat: &Value) -> String {
    let pv = &chat["preview"];
    if !pv.is_object() {
        return String::new();
    }
    let who = if pv["isSender"].as_bool().unwrap_or(false) {
        "you: ".to_string()
    } else if s(chat, "type") == "group" && !s(pv, "senderName").is_empty() {
        format!("{}: ", s(pv, "senderName"))
    } else {
        String::new()
    };
    let body = match s(pv, "text") {
        "" if pv["attachments"].as_array().is_some_and(|a| !a.is_empty()) => "📎 attachment",
        t => t,
    };
    format!("{who}{body}").replace('\n', " ")
}

/// Cut `text` to at most `max` cells.
fn cut(text: &str, max: usize) -> String {
    let mut out = String::new();
    for c in text.chars() {
        if width(&out) + width(c.encode_utf8(&mut [0; 4])) > max {
            break;
        }
        out.push(c);
    }
    out
}

fn pad(text: &str, to: usize) -> String {
    format!("{text}{}", " ".repeat(to.saturating_sub(width(text))))
}

pub struct Inbox {
    chats: Vec<Value>,
    /// accountID → network name.
    accounts: HashMap<String, String>,
    index: usize,
    top: usize,
    filter: String,
    unread_only: bool,
    network: Option<String>,
    open: Option<Value>,
    messages: Vec<Value>,
    /// The picked message; None = the newest (and follow new ones).
    msg_index: Option<usize>,
    reply_to: Option<Value>,
    editing: Option<Value>,
    message: String,
    state: State,
    compose: Input,
    mode: Mode,
    generation: u64,
    /// Conversation rows on screen → message index (None: a day line or blank).
    line_map: Vec<Option<usize>>,
    rects: Vec<Rect>,
    chat_rows: usize,
    last_chats: Instant,
    last_open: Instant,
    was_visible: bool,
}

impl Inbox {
    pub fn new() -> Self {
        Inbox {
            chats: vec![],
            accounts: HashMap::new(),
            index: 0,
            top: 0,
            filter: String::new(),
            unread_only: false,
            network: None,
            open: None,
            messages: vec![],
            msg_index: None,
            reply_to: None,
            editing: None,
            message: String::new(),
            state: State::Connecting,
            compose: Input::new(IDLE_PLACEHOLDER),
            mode: Mode::Idle,
            generation: 0,
            line_map: vec![],
            rects: vec![Rect::default(); 3],
            chat_rows: 1,
            last_chats: Instant::now(),
            last_open: Instant::now(),
            was_visible: false,
        }
    }

    fn network_of(&self, chat: &Value) -> String {
        match s(chat, "network") {
            "" => self.accounts.get(s(chat, "accountID")).cloned().unwrap_or_default(),
            n => n.to_string(),
        }
    }

    // ── data ─────────────────────────────────────────────────────────────

    fn load_chats(&mut self, ctx: &Ctx, quiet: bool) {
        self.generation += 1;
        self.last_chats = Instant::now();
        let generation = self.generation;
        let known = (!self.accounts.is_empty()).then(|| self.accounts.clone());
        let (filter, unread, network) = (self.filter.clone(), self.unread_only, self.network.clone());
        ctx.spawn(ID, move |api| async move {
            let mut fetched = None;
            let accounts = match known {
                Some(a) => a,
                None => match api.beeper_status().await {
                    Err(e) => return Got::Chats { generation, accounts: None, result: Err(e.to_string()), quiet },
                    Ok(st) if !st["token"].as_bool().unwrap_or(false) => {
                        let e = "set-token: Beeper isn't connected".to_string();
                        return Got::Chats { generation, accounts: None, result: Err(e), quiet };
                    }
                    Ok(st) => {
                        let a: HashMap<String, String> = st["accounts"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .map(|a| {
                                let id = s(a, "accountID").to_string();
                                let net = match s(a, "network") {
                                    "" => id.clone(),
                                    n => n.to_string(),
                                };
                                (id, net)
                            })
                            .collect();
                        fetched = Some(a.clone());
                        a
                    }
                },
            };
            let mut params: Vec<(&str, String)> = vec![("limit", "100".into())];
            if !filter.is_empty() {
                params.push(("query", filter.clone()));
            }
            if unread {
                params.push(("unreadOnly", "true".into()));
            }
            if let Some(net) = &network {
                for (id, n) in &accounts {
                    if n == net {
                        params.push(("accountIDs", id.clone()));
                    }
                }
            }
            let path = if !filter.is_empty() || unread { "v1/chats/search" } else { "v1/chats" };
            let borrowed: Vec<(&str, &str)> = params.iter().map(|(k, v)| (*k, v.as_str())).collect();
            let result = api.beeper(Method::GET, path, &borrowed, None).await.map(items).map(|chats| {
                chats
                    .into_iter()
                    .filter(|c| match (&network, path) {
                        (Some(net), "v1/chats") => accounts.get(s(c, "accountID")) == Some(net),
                        _ => true,
                    })
                    .collect()
            });
            Got::Chats { generation, accounts: fetched, result: result.map_err(|e| e.to_string()), quiet }
        });
    }

    fn load_messages(&mut self, ctx: &Ctx, quiet: bool) {
        self.last_open = Instant::now();
        let Some(chat) = self.open.as_ref().map(|c| s(c, "id").to_string()) else { return };
        ctx.spawn(ID, move |api| async move {
            let result = api.beeper(Method::GET, &format!("v1/chats/{chat}/messages"), &[], None).await;
            let result = result.map(items).map_err(|e| e.to_string()).map(|msgs| {
                let mut msgs: Vec<Value> =
                    msgs.into_iter().filter(|m| !m["isHidden"].as_bool().unwrap_or(false)).collect();
                let key = |m: &Value| match s(m, "sortKey") {
                    "" => s(m, "timestamp").to_string(),
                    k => k.to_string(),
                };
                msgs.sort_by_key(key);
                msgs
            });
            Got::Messages { chat, result, quiet }
        });
    }

    fn post(&self, ctx: &Ctx, method: Method, path: String, body: Value, reload: bool) {
        ctx.spawn(ID, move |api| async move {
            let error = api.beeper(method, &path, &[], Some(&body)).await.err().map(|e| format!("✖ {e}"));
            Got::Posted { reload, error }
        });
    }

    fn open_chat(&mut self, index: usize, ctx: &mut Ctx) {
        let Some(chat) = self.chats.get_mut(index) else { return };
        let id = s(chat, "id").to_string();
        let unread = chat["unreadCount"].as_u64().unwrap_or(0) > 0;
        if unread {
            chat["unreadCount"] = json!(0);
        }
        self.open = Some(chat.clone());
        self.messages.clear();
        self.msg_index = None;
        self.reply_to = None;
        self.editing = None;
        self.load_messages(ctx, false);
        if unread {
            self.post(ctx, Method::POST, format!("v1/chats/{id}/read"), json!({}), false);
        }
        ctx.focus(CONV);
    }

    fn selected(&self) -> Option<&Value> {
        match self.msg_index {
            Some(i) => self.messages.get(i),
            None => self.messages.last(),
        }
    }

    // ── compose ──────────────────────────────────────────────────────────

    fn start_compose(&mut self, value: &str, ctx: &mut Ctx) {
        let Some(open) = &self.open else { return };
        if open["isReadOnly"].as_bool().unwrap_or(false) {
            self.message = "you can't write in this chat".into();
            return;
        }
        self.compose.placeholder = format!("message {} · enter sends · esc", s(open, "title"));
        self.compose.set(value);
        self.mode = if self.editing.is_some() { Mode::Edit } else { Mode::Send };
        ctx.focus(COMPOSE);
    }

    fn stop_compose(&mut self, ctx: &mut Ctx) {
        self.reply_to = None;
        self.editing = None;
        self.compose.clear();
        self.compose.placeholder = IDLE_PLACEHOLDER.into();
        self.mode = Mode::Idle;
        ctx.focus(if self.open.is_some() { CONV } else { CHATS });
    }

    fn submit(&mut self, ctx: &mut Ctx) {
        let text = self.compose.value.trim().to_string();
        match self.mode {
            Mode::Filter => {
                self.filter = text;
                self.index = 0;
                self.load_chats(ctx, false);
                self.stop_compose(ctx);
                ctx.focus(CHATS);
            }
            Mode::React => {
                let key = match text.parse::<usize>() {
                    Ok(n) if (1..=REACTIONS.len()).contains(&n) => REACTIONS[n - 1].to_string(),
                    _ => text,
                };
                if let (false, Some(m), Some(open)) = (key.is_empty(), self.selected(), &self.open) {
                    let path = format!("v1/chats/{}/messages/{}/reactions", s(open, "id"), s(m, "id"));
                    self.post(ctx, Method::POST, path, json!({ "reactionKey": key }), true);
                }
                self.stop_compose(ctx);
            }
            Mode::Edit | Mode::Send | Mode::Idle => {
                let Some(open) = self.open.clone() else {
                    self.stop_compose(ctx);
                    return;
                };
                if text.is_empty() {
                    self.stop_compose(ctx);
                    return;
                }
                let chat = s(&open, "id").to_string();
                match (&self.editing, self.mode) {
                    (Some(m), Mode::Edit) => {
                        let path = format!("v1/chats/{chat}/messages/{}", s(m, "id"));
                        self.post(ctx, Method::PUT, path, json!({ "text": text }), true);
                    }
                    _ => {
                        let mut body = json!({ "text": text });
                        if let Some(r) = &self.reply_to {
                            body["replyToMessageID"] = json!(s(r, "id"));
                        }
                        // Shown straight away as "sending"; the reload brings the real one.
                        self.messages.push(json!({"id": "pending", "isSender": true, "text": text,
                            "timestamp": Utc::now().to_rfc3339(), "sendStatus": {"status": "sending"}}));
                        self.msg_index = None;
                        self.post(ctx, Method::POST, format!("v1/chats/{chat}/messages"), body, true);
                    }
                }
                self.reply_to = None;
                self.editing = None;
                self.compose.clear();
                if self.mode == Mode::Edit {
                    self.mode = Mode::Send;
                }
            }
        }
    }

    // ── keys ─────────────────────────────────────────────────────────────

    fn chat_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.message.clear();
        match (keys::ch(key), key.code) {
            (_, KeyCode::Down) => self.index = (self.index + 1).min(self.chats.len().saturating_sub(1)),
            (_, KeyCode::Up) => self.index = self.index.saturating_sub(1),
            (_, KeyCode::PageDown) => {
                self.index = (self.index + self.chat_rows).min(self.chats.len().saturating_sub(1))
            }
            (_, KeyCode::PageUp) => self.index = self.index.saturating_sub(self.chat_rows),
            (_, KeyCode::Enter | KeyCode::Right) if !self.chats.is_empty() => self.open_chat(self.index, ctx),
            (Some('r'), _) => {
                if self.state == State::NoToken {
                    self.accounts.clear();
                }
                self.load_chats(ctx, false);
            }
            (Some('u'), _) => {
                self.unread_only = !self.unread_only;
                self.index = 0;
                self.load_chats(ctx, false);
            }
            (Some(c @ (',' | '.')), _) if !self.accounts.is_empty() => {
                let mut nets: Vec<Option<String>> = vec![None];
                let mut names: Vec<String> = self.accounts.values().cloned().collect();
                names.sort();
                names.dedup();
                nets.extend(names.into_iter().map(Some));
                let at = nets.iter().position(|n| *n == self.network).unwrap_or(0) as isize;
                let step = if c == '.' { 1 } else { -1 };
                self.network = nets[(at + step).rem_euclid(nets.len() as isize) as usize].clone();
                self.index = 0;
                self.load_chats(ctx, false);
            }
            (Some('/'), _) => {
                self.compose.placeholder = "filter chats (empty clears)".into();
                self.compose.set(&self.filter.clone());
                self.mode = Mode::Filter;
                ctx.focus(COMPOSE);
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn message_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        let n = self.messages.len();
        self.message.clear();
        match (keys::ch(key), key.code) {
            (_, KeyCode::Up) if n > 0 => self.msg_index = Some(self.msg_index.unwrap_or(n - 1).saturating_sub(1)),
            (_, KeyCode::Down) if n > 0 => {
                self.msg_index = self.msg_index.and_then(|i| (i + 1 < n).then_some(i + 1));
            }
            (Some('i'), _) | (_, KeyCode::Tab | KeyCode::Enter) => self.start_compose("", ctx),
            (_, KeyCode::Left | KeyCode::Esc) => ctx.focus(CHATS),
            (Some('r'), _) if n > 0 => {
                self.reply_to = self.selected().cloned();
                self.start_compose("", ctx);
            }
            (Some('e'), _) if n > 0 => match self.selected().cloned() {
                Some(m) if m["isSender"].as_bool().unwrap_or(false) => {
                    let text = s(&m, "text").to_string();
                    self.editing = Some(m);
                    self.start_compose(&text, ctx);
                }
                _ => self.message = "you can only edit your own messages".into(),
            },
            (Some('+'), _) if n > 0 => {
                let menu: Vec<String> = REACTIONS.iter().enumerate().map(|(i, e)| format!("{} {e}", i + 1)).collect();
                self.compose.placeholder = format!("react: {}  (or type one)", menu.join("  "));
                self.compose.clear();
                self.mode = Mode::React;
                ctx.focus(COMPOSE);
            }
            (Some('m'), _) if self.open.is_some() => {
                let id = self.open.as_ref().map(|o| s(o, "id").to_string()).unwrap_or_default();
                self.post(ctx, Method::POST, format!("v1/chats/{id}/unread"), json!({}), false);
                self.message = "marked unread".into();
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    /// The wheel: up walks back through the messages, down comes forward (past the newest: follow).
    fn scroll_messages(&mut self, step: isize) {
        let n = self.messages.len();
        if n == 0 {
            return;
        }
        let cur = self.msg_index.unwrap_or(n - 1) as isize + step;
        self.msg_index =
            if step > 0 && cur >= n as isize - 1 { None } else { Some(cur.clamp(0, n as isize - 1) as usize) };
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn chats_title(&self) -> String {
        let mut t = "CHATS".to_string();
        if let Some(n) = &self.network {
            t += &format!(" · {n}");
        }
        if self.unread_only {
            t += " · unread";
        }
        if !self.filter.is_empty() {
            t += &format!(" · “{}”", self.filter);
        }
        t
    }

    fn draw_chats(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let block = ui::panel(pal, &self.chats_title(), view.panel_state(CHATS));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let w = inner.width as usize;
        let lines: Vec<Line> = match self.state {
            State::NoToken => vec![
                Line::raw(""),
                Line::styled(" Beeper isn't connected yet.", bold(pal.amber_hi)),
                Line::raw(""),
                Line::styled(" On the PC, in a terminal, run this", Style::new().fg(pal.amber)),
                Line::styled(" (one line; it may wrap here):", Style::new().fg(pal.amber)),
                Line::raw(""),
                Line::styled(
                    " cd ~/Documents/mabel; .venv-arch/bin/python -m src.beeper --set-token",
                    Style::new().fg(pal.teal),
                ),
                Line::raw(""),
                Line::styled(" then r here.", Style::new().fg(pal.amber_dim)),
            ],
            State::Connecting => vec![Line::styled(" connecting…", Style::new().fg(pal.amber_dim))],
            State::Offline if self.chats.is_empty() => vec![
                Line::styled(
                    format!(" ✖ {}", if self.message.is_empty() { "offline" } else { &self.message }),
                    Style::new().fg(pal.red),
                ),
                Line::raw(""),
                Line::styled(" r tries again", Style::new().fg(pal.red)),
            ],
            _ => self.chat_lines(pal, inner.height as usize, w, view.inside(CHATS)),
        };
        let wrapped = matches!(self.state, State::NoToken);
        let p = Paragraph::new(lines);
        frame.render_widget(if wrapped { p.wrap(ratatui::widgets::Wrap { trim: false }) } else { p }, inner);
    }

    fn chat_lines(&mut self, pal: &Palette, height: usize, w: usize, focused: bool) -> Vec<Line<'static>> {
        let rows = (height.saturating_sub(1) / 2).max(1);
        self.chat_rows = rows;
        if self.index < self.top {
            self.top = self.index;
        } else if self.index >= self.top + rows {
            self.top = self.index + 1 - rows;
        }
        let mut lines = vec![];
        let open = self.open.as_ref().map(|o| s(o, "id").to_string());
        for i in self.top..self.chats.len().min(self.top + rows) {
            let c = &self.chats[i];
            let on = i == self.index && focused;
            let unread = c["unreadCount"].as_u64().unwrap_or(0);
            let stamp = when(c["lastActivity"].as_str());
            let mark = if unread > 0 {
                format!("{unread:>2} ")
            } else if open.as_deref() == Some(s(c, "id")) {
                "▸  ".to_string()
            } else {
                "   ".to_string()
            };
            let name = cut(
                match s(c, "title") {
                    "" => "?",
                    t => t,
                },
                w.saturating_sub(width(&stamp) + 4).max(6),
            );
            let style = if on {
                bold(pal.bg).bg(pal.amber)
            } else if unread > 0 {
                bold(pal.amber_hi)
            } else {
                Style::new().fg(pal.amber)
            };
            let left = pad(&format!("{mark}{name}"), w.saturating_sub(width(&stamp)));
            lines.push(Line::from(vec![
                Span::styled(left, style),
                Span::styled(stamp, if on { style } else { Style::new().fg(pal.amber_dim) }),
            ]));
            let net = cut(&self.network_of(c), 10);
            let sub = cut(&format!("   {net} · {}", preview(c)), w);
            let sub_style = if on { Style::new().fg(pal.bg).bg(pal.amber_dim) } else { Style::new().fg(pal.amber_dim) };
            lines.push(Line::styled(pad(&sub, if on { w } else { 0 }), sub_style));
        }
        if self.chats.is_empty() {
            let filtered = !self.filter.is_empty() || self.unread_only || self.network.is_some();
            lines.push(Line::styled(
                if filtered { " no chats match" } else { " no chats" },
                Style::new().fg(pal.amber_dim),
            ));
        }
        lines
    }

    fn draw_messages(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let title = match &self.open {
            Some(o) => format!(
                "{} · {}",
                match s(o, "title") {
                    "" => "?",
                    t => t,
                },
                self.network_of(o)
            ),
            None => "—".into(),
        };
        let block = ui::panel(pal, &title, view.panel_state(CONV));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        self.line_map.clear();
        if self.open.is_none() {
            let mut lines =
                vec![Line::raw(""), Line::styled(" enter on a chat opens it", Style::new().fg(pal.amber_dim))];
            if !self.message.is_empty() {
                lines.push(Line::raw(""));
                lines.push(Line::styled(format!(" {}", self.message), Style::new().fg(pal.sand)));
            }
            frame.render_widget(Paragraph::new(lines), inner);
            return;
        }
        let focused = view.inside(CONV);
        let wrap_w = (inner.width as usize).saturating_sub(3).max(10);
        let by_id: HashMap<&str, &Value> = self.messages.iter().map(|m| (s(m, "id"), m)).collect();
        let mut lines: Vec<(Option<usize>, Line<'static>)> = vec![];
        let mut last_day = String::new();
        for (i, m) in self.messages.iter().enumerate() {
            let day: String = s(m, "timestamp").chars().take(10).collect();
            if day != last_day {
                lines.push((None, Line::styled(format!("── {day} ──"), Style::new().fg(pal.amber_faint))));
                last_day = day;
            }
            let mine = m["isSender"].as_bool().unwrap_or(false);
            let who = if mine {
                "you".to_string()
            } else {
                match s(m, "senderName") {
                    "" => "?".into(),
                    n => n.to_string(),
                }
            };
            let mut head = vec![
                Span::styled(who, bold(if mine { pal.teal } else { pal.amber_hi })),
                Span::styled(format!("  {}", when(m["timestamp"].as_str())), Style::new().fg(pal.amber_dim)),
            ];
            if !s(m, "editedTimestamp").is_empty() {
                head.push(Span::styled(" (edited)", Style::new().fg(pal.amber_dim)));
            }
            let status = s(&m["sendStatus"], "status");
            if mine && !status.is_empty() && !matches!(status, "sent" | "delivered" | "read" | "success") {
                head.push(Span::styled(format!(" · {status}"), Style::new().fg(pal.red)));
            }
            lines.push((Some(i), Line::from(head)));
            if let Some(linked) = m["linkedMessageID"].as_str() {
                let quote = by_id.get(linked).map_or("an earlier message".to_string(), |r| match s(r, "text") {
                    "" => "…".into(),
                    t => t.replace('\n', " "),
                });
                lines.push((
                    Some(i),
                    Line::styled(
                        format!("  ↳ {}", cut(&quote, wrap_w.saturating_sub(4))),
                        Style::new().fg(pal.amber_dim),
                    ),
                ));
            }
            let deleted = m["isDeleted"].as_bool().unwrap_or(false);
            let mut body = if deleted { "(deleted)".to_string() } else { s(m, "text").to_string() };
            for a in m["attachments"].as_array().into_iter().flatten() {
                let kind = if a["isVoiceNote"].as_bool().unwrap_or(false) {
                    "🎤 voice"
                } else if a["isSticker"].as_bool().unwrap_or(false) {
                    "sticker"
                } else {
                    match s(a, "type") {
                        "" => "file",
                        t => t,
                    }
                };
                body += format!("\n📎 {kind} {}", s(a, "fileName")).trim_end();
            }
            for link in m["links"].as_array().into_iter().flatten() {
                body += &format!(
                    "\n🔗 {}",
                    match s(link, "title") {
                        "" => s(link, "url"),
                        t => t,
                    }
                );
            }
            let style = Style::new().fg(if deleted { pal.amber_dim } else { pal.amber });
            for para in body.split('\n') {
                if para.is_empty() {
                    continue;
                }
                for l in
                    wrap::wrap(&[Span::styled(para.to_string(), style)], wrap_w, &[Span::raw("  ")], &[Span::raw("  ")])
                {
                    lines.push((Some(i), l));
                }
            }
            if let Some(reactions) = m["reactions"].as_array().filter(|r| !r.is_empty()) {
                let mut counts: Vec<(String, usize)> = vec![];
                for r in reactions {
                    let k = match s(r, "reactionKey") {
                        "" => "?",
                        k => k,
                    }
                    .to_string();
                    match counts.iter_mut().find(|(key, _)| *key == k) {
                        Some((_, n)) => *n += 1,
                        None => counts.push((k, 1)),
                    }
                }
                let text: Vec<String> =
                    counts.iter().map(|(k, n)| if *n > 1 { format!("{k}{n}") } else { k.clone() }).collect();
                lines.push((Some(i), Line::styled(format!("  {}", text.join("  ")), Style::new().fg(pal.sand))));
            }
        }
        let sel = self.msg_index.unwrap_or(self.messages.len().saturating_sub(1));
        let rows = (inner.height as usize).saturating_sub(1).max(1);
        let start = match self.msg_index {
            Some(_) => {
                let last_sel =
                    lines.iter().rposition(|(i, _)| *i == Some(sel)).unwrap_or(lines.len().saturating_sub(1));
                (last_sel + 2).saturating_sub(rows).min(lines.len().saturating_sub(rows))
            }
            None => lines.len().saturating_sub(rows),
        };
        let mut shown: Vec<Line> = vec![];
        for (i, line) in lines.into_iter().skip(start).take(rows) {
            let marked = focused && self.msg_index.is_some() && i == Some(sel);
            let mut spans =
                vec![if marked { Span::styled("▌", Style::new().fg(pal.amber_hi)) } else { Span::raw(" ") }];
            spans.extend(line.spans);
            shown.push(Line::from(spans));
            self.line_map.push(i);
        }
        let hint = if let Some(r) = &self.reply_to {
            let name = match s(r, "senderName") {
                "" => "you",
                n => n,
            };
            format!("replying to {name}: {}", cut(s(r, "text"), 40))
        } else if self.editing.is_some() {
            "editing your message".to_string()
        } else {
            self.message.clone()
        };
        let [body, foot] = Layout::vertical([Constraint::Length(rows as u16), Constraint::Length(1)]).areas(inner);
        frame.render_widget(Paragraph::new(shown), body);
        if !hint.is_empty() {
            frame.render_widget(Paragraph::new(Span::styled(format!(" {hint}"), bold(pal.sand))), foot);
        }
    }
}

impl Channel for Inbox {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "COMMS"
    }

    fn start(&mut self, ctx: &mut Ctx) {
        self.load_chats(ctx, false);
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let [chats, right] = Layout::horizontal([Constraint::Length(42), Constraint::Fill(1)]).areas(area);
        let [conv, compose] = Layout::vertical([Constraint::Fill(1), Constraint::Length(3)]).areas(right);
        self.draw_chats(frame, chats, view);
        self.draw_messages(frame, conv, view);
        let state = view.panel_state(COMPOSE);
        let block = ui::panel(view.pal, "", state);
        let inner = block.inner(compose);
        frame.render_widget(block, compose);
        let placeholder = if state == PanelState::Lit { view.pal.amber_dim } else { view.pal.amber_faint };
        self.compose.render(
            frame,
            inner,
            Style::new().fg(view.pal.amber_hi),
            Style::new().fg(placeholder),
            view.inside(COMPOSE),
        );
        self.rects = vec![chats, conv, compose];
        self.rects.clone()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        match section {
            CHATS => self.chat_key(&key, ctx),
            CONV => self.message_key(&key, ctx),
            COMPOSE => {
                if key.code == KeyCode::Esc {
                    self.stop_compose(ctx);
                    return Outcome::Used;
                }
                if self.mode == Mode::Idle && key.code != KeyCode::Enter {
                    // Typing into the idle line starts a message in the open chat.
                    if self.open.is_none() {
                        return Outcome::Ignored;
                    }
                    self.start_compose("", ctx);
                }
                match self.compose.key(&key) {
                    InputResult::Submit => {
                        self.submit(ctx);
                        Outcome::Used
                    }
                    InputResult::Used => Outcome::Used,
                    InputResult::Ignored => Outcome::Ignored,
                }
            }
            _ => Outcome::Ignored,
        }
    }

    fn paste(&mut self, section: usize, text: &str, ctx: &mut Ctx) {
        if section == COMPOSE {
            if self.mode == Mode::Idle {
                self.start_compose("", ctx);
            }
            self.compose.insert(text);
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        let (x, y) = (event.column, event.row);
        let _ = x;
        match (event.kind, section) {
            (MouseEventKind::Down(MouseButton::Left), Some(CHATS)) => {
                let top = self.rects[CHATS].y + 1;
                let i = self.top + (y.saturating_sub(top) / 2) as usize; // two lines per chat
                if y >= top && i < self.chats.len() {
                    self.index = i;
                    self.open_chat(i, ctx);
                }
            }
            (MouseEventKind::Down(MouseButton::Left), Some(CONV)) => {
                let row = y.saturating_sub(self.rects[CONV].y + 1) as usize;
                if let Some(Some(i)) = self.line_map.get(row) {
                    self.msg_index = Some(*i);
                }
            }
            (MouseEventKind::Down(MouseButton::Left), Some(COMPOSE)) => {
                if self.open.is_some() && self.mode == Mode::Idle {
                    self.start_compose("", ctx); // clicking the line starts writing
                }
            }
            (MouseEventKind::ScrollDown, Some(CHATS)) => {
                self.index = (self.index + 3).min(self.chats.len().saturating_sub(1));
            }
            (MouseEventKind::ScrollUp, Some(CHATS)) => self.index = self.index.saturating_sub(3),
            (MouseEventKind::ScrollDown, Some(CONV)) => self.scroll_messages(1),
            (MouseEventKind::ScrollUp, Some(CONV)) => self.scroll_messages(-1),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        Some(CHATS)
    }

    fn tick(&mut self, now: Instant, visible: bool, ctx: &mut Ctx) {
        let appeared = visible && !self.was_visible;
        self.was_visible = visible;
        if !visible {
            return;
        }
        let polling = matches!(self.state, State::Ready | State::Offline);
        if polling && (appeared || now.duration_since(self.last_chats) >= POLL_CHATS) {
            self.load_chats(ctx, true);
        }
        if self.open.is_some()
            && self.state == State::Ready
            && (appeared || now.duration_since(self.last_open) >= POLL_OPEN)
        {
            self.load_messages(ctx, true);
        }
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Chats { generation, .. } if generation != self.generation => {} // overtaken by a newer load
            Got::Chats { accounts, result, quiet, .. } => {
                if let Some(a) = accounts {
                    self.accounts = a;
                }
                match result {
                    Ok(chats) => {
                        let keep = self.chats.get(self.index).map(|c| s(c, "id").to_string());
                        self.chats =
                            chats.into_iter().filter(|c| !c["isArchived"].as_bool().unwrap_or(false)).collect();
                        if let Some(keep) = keep
                            && let Some(i) = self.chats.iter().position(|c| s(c, "id") == keep)
                        {
                            self.index = i;
                        }
                        self.index = self.index.min(self.chats.len().saturating_sub(1));
                        self.state = State::Ready;
                    }
                    Err(e) => {
                        self.state = if e.contains("set-token") { State::NoToken } else { State::Offline };
                        if !quiet {
                            self.message = e;
                        }
                    }
                }
            }
            Got::Messages { chat, result, quiet } => {
                if self.open.as_ref().map(|o| s(o, "id")) != Some(chat.as_str()) {
                    return; // another chat was opened meanwhile
                }
                match result {
                    Ok(msgs) => {
                        let grew = msgs.len() != self.messages.len();
                        self.messages = msgs;
                        if grew && !quiet {
                            self.msg_index = None;
                        }
                        if let Some(i) = self.msg_index {
                            self.msg_index = Some(i.min(self.messages.len().saturating_sub(1)));
                        }
                    }
                    Err(e) if !quiet => self.message = e,
                    Err(_) => {}
                }
            }
            Got::Posted { reload, error } => match error {
                Some(e) => self.message = e,
                None if reload => self.load_messages(ctx, false),
                None => {}
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn previews_and_cuts() {
        let chat = json!({"type": "group", "preview": {"senderName": "Ann", "text": "hej\ndå"}});
        assert_eq!(preview(&chat), "Ann: hej då");
        let mine = json!({"preview": {"isSender": true, "attachments": [{}]}});
        assert_eq!(preview(&mine), "you: 📎 attachment");
        assert_eq!(cut("hej då", 4), "hej ");
        assert_eq!(cut("😀😀", 3), "😀");
        assert_eq!(when(Some("garbage")), "");
    }
}
