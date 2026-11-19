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

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, PanelState, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::wrap::{self, width};

// INBOX is split by job; the pieces add to `impl Inbox`.
/// keys
mod controls;
/// loading, sending, composing
mod data;
/// drawing
mod draw;

/// The id messages and help use.
const ID: &str = "inbox";
// The three sections, in the order `render` returns them.
const CHATS: usize = 0;
const CONV: usize = 1;
const COMPOSE: usize = 2;

/// While INBOX is on screen: fetch the chat list this often, and the open chat this often.
const POLL_CHATS: Duration = Duration::from_secs(8);
const POLL_OPEN: Duration = Duration::from_secs(3);
/// The reactions + offers.
const REACTIONS: [&str; 6] = ["👍", "❤️", "😂", "😮", "😢", "🙏"];
/// The compose line's grey text when it's not in use.
const IDLE_PLACEHOLDER: &str = "i or tab: write · enter sends · esc";

/// Can INBOX reach Beeper?
#[derive(Clone, Copy, PartialEq)]
enum State {
    Connecting,
    Ready,
    /// Mabel has no Beeper token yet (`python -m src.beeper --set-token` on the PC).
    NoToken,
    /// Mabel or Beeper Desktop isn't answering.
    Offline,
}

/// What the compose line is for right now.
#[derive(Clone, Copy, PartialEq)]
enum Mode {
    Idle,
    /// Writing a message (or a reply).
    Send,
    /// Editing one of yours.
    Edit,
    /// Picking a reaction.
    React,
    /// Typing a filter for the chat list.
    Filter,
}

/// Background work reporting back.
enum Got {
    /// The chat list (and the accounts' networks), ignored if it's older than the newest ask.
    Chats {
        generation: u64,
        accounts: Option<HashMap<String, String>>,
        result: Result<Vec<Value>, String>,
        quiet: bool,
    },
    /// A chat's messages (`quiet`: a poll, so no "loading").
    Messages { chat: String, result: Result<Vec<Value>, String>, quiet: bool },
    /// A send, edit, reaction or mark finished.
    Posted { reload: bool, error: Option<String> },
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

/// The list in a Beeper answer (a bare list, or `{"items": […]}`).
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

/// A text field ("" if missing).
fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

/// A chat's last message as one line: "you: …", or the sender in groups, or "📎 attachment".
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

/// `text` padded with spaces to `to` cells.
fn pad(text: &str, to: usize) -> String {
    format!("{text}{}", " ".repeat(to.saturating_sub(width(text))))
}

/// The COMMS ▸ INBOX screen.
pub struct Inbox {
    /// The chats, newest first.
    chats: Vec<Value>,
    /// accountID → network name.
    accounts: HashMap<String, String>,
    /// The chat list's cursor and first line shown.
    index: usize,
    top: usize,
    /// / filter text, u unread only, and , . one network only.
    filter: String,
    unread_only: bool,
    network: Option<String>,
    /// The open chat and its messages (oldest first).
    open: Option<Value>,
    messages: Vec<Value>,
    /// The picked message; None = the newest (and follow new ones).
    msg_index: Option<usize>,
    /// r: the message being replied to; e: the one being edited.
    reply_to: Option<Value>,
    editing: Option<Value>,
    /// The status message (what just happened, or what went wrong).
    message: String,
    state: State,
    compose: Input,
    mode: Mode,
    /// Counts chat-list loads, so a slow old answer can't replace a newer one.
    generation: u64,
    /// Conversation rows on screen → message index (None: a day line or blank).
    line_map: Vec<Option<usize>>,
    /// Where the three sections were drawn (mouse clicks use them).
    rects: Vec<Rect>,
    /// How many chats fit in the list last time.
    chat_rows: usize,
    /// When the list and the open chat were last fetched (for polling).
    last_chats: Instant,
    last_open: Instant,
    /// On screen last tick (coming back fetches straight away).
    was_visible: bool,
}

impl Inbox {
    /// An empty INBOX; `start` asks for the chats.
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

    /// A chat's network (WhatsApp, Signal…), from the chat or its account.
    fn network_of(&self, chat: &Value) -> String {
        match s(chat, "network") {
            "" => self.accounts.get(s(chat, "accountID")).cloned().unwrap_or_default(),
            n => n.to_string(),
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
