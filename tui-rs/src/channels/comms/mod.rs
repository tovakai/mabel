//! COMMS ▸ MABEL: chat with her. Chats on the left (ctrl+s), the conversation, the prompt at the
//! bottom, her portrait and state on the right.
//!
//! Her reply streams in as Markdown. Tool calls show as one dim line each; an "Allow this
//! action?" card takes y (allow once) or n (deny), or a click, and resumes the turn.

/// COMMS ▸ COURSE: the screen
pub mod course;
/// COURSE's model: courses in the vault
pub mod courses;
/// COMMS ▸ INBOX: Beeper chats
pub mod inbox;

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

use crate::os::api::ChatEvent;
use crate::os::avatar::Avatar;
use crate::os::channel::{Channel, Ctx, Msg, Outcome, Request, View};
use crate::os::config::Config;
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, PanelState, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::{markdown, wrap};

// COMMS ▸ MABEL is split by job; the pieces add to `impl Comms`.
/// drawing
mod draw;
/// chats, sending, the reply, approvals
mod talk;

/// The id messages and help use.
const ID: &str = "mabel";
// The three sections, in the order `render` returns them.
const LIST: usize = 0;
const LOG: usize = 1;
const PROMPT: usize = 2;

/// Keep talking this long after the last bit of text arrived.
const TALK_HOLD: Duration = Duration::from_millis(600);
/// Characters a second: talk time for a reply that lands all at once.
const READ_SPEED: f64 = 45.0;
/// The prompt's grey text when it's empty.
const PLACEHOLDER: &str = "talk to Mabel…";

/// Results of background work, back in `on_msg`.
enum Got {
    /// Her chat list (`select_first`: open the newest one).
    Sessions {
        result: Result<Vec<Value>, String>,
        select_first: bool,
    },
    /// A chat's messages (ignored if another chat was picked meanwhile).
    History {
        generation: u64,
        result: Result<Vec<Value>, String>,
    },
    /// A new chat was made.
    Created(Result<Value, String>),
    /// A piece of her reply, and its end (old turns' pieces are ignored).
    Stream {
        turn: u64,
        event: ChatEvent,
    },
    TurnDone {
        turn: u64,
    },
}

/// Who said a message.
#[derive(Clone, Copy, PartialEq)]
enum Role {
    You,
    Mabel,
}

/// One thing in the conversation.
enum Item {
    /// A message, with its time and its rendered lines (cached per width and length).
    Msg {
        role: Role,
        text: String,
        when: String,
        cache: Option<(u16, usize, Vec<Line<'static>>)>,
    },
    /// One dim line per tool she used ("⚙ searching the web…").
    Tool(String),
    Error(String),
    /// An "Allow this action?" card: y allows once, n denies.
    Approval(Value),
}

/// "12:34" from an ISO timestamp (UTC or local), or now.
fn stamp(value: Option<&str>) -> String {
    value.and_then(crate::os::timefmt::parse_local).unwrap_or_else(Local::now).format("%H:%M").to_string()
}

/// The COMMS ▸ MABEL screen.
pub struct Comms {
    /// Her chats (newest first) and the list's cursor.
    sessions: Vec<Value>,
    list: ListState,
    /// The open chat, and its title.
    session: Option<Value>,
    title: String,
    /// The conversation.
    items: Vec<Item>,
    prompt: Input,
    /// A reply is streaming (one message at a time).
    busy: bool,
    /// Does Mabel answer? (Shown in the casing; None until the first try.)
    link: Option<bool>,
    /// Her face animates as talking until then.
    talk_until: Instant,
    avatar: Avatar,
    /// ctrl+s: on the PC the list is shown unless toggled; on the deck hidden unless toggled.
    list_toggled: bool,
    /// Lines scrolled up from the bottom of the conversation (0 = following it).
    scroll_up: usize,
    /// How many lines the conversation showed last time (PgUp/PgDn move by it).
    log_page: usize,
    /// Which chat a history load is for (a quicker second pick wins).
    generation: u64,
    /// Counts messages sent, so a late piece of an old reply can't land in a new one.
    turn: u64,
    /// The reply being streamed, and whether any text arrived.
    reply: Option<usize>,
    got_text: bool,
    /// Where the approval buttons were drawn: (area, approval id, decision).
    buttons: Vec<(Rect, String, &'static str)>,
    /// Where the three sections were drawn (mouse clicks use them).
    rects: Vec<Rect>,
    /// VAULT's M: the next message goes with this note (name, path, content).
    pub pending_note: Option<(String, String, String)>,
}

impl Comms {
    /// An empty COMMS; `start` loads her chats.
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

    /// Keep her face talking for at least `d` more.
    fn talk(&mut self, d: Duration) {
        self.talk_until = self.talk_until.max(Instant::now() + d);
    }

    /// Is the chat list shown? (PC: unless ctrl+s hid it. Deck: only if ctrl+s showed it.)
    fn list_visible(&self, narrow: bool) -> bool {
        if narrow { self.list_toggled } else { !self.list_toggled }
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

    /// Scroll the conversation up (negative: down) by lines.
    fn scroll(&mut self, up: isize) {
        self.scroll_up = (self.scroll_up as isize + up).max(0) as usize; // clamped when drawn
    }

    /// Move the chat list's cursor.
    fn move_list(&mut self, by: isize) {
        if self.sessions.is_empty() {
            return;
        }
        let at = self.list.selected().unwrap_or(0) as isize + by;
        self.list.select(Some(at.clamp(0, self.sessions.len() as isize - 1) as usize));
    }

    /// Open the chat under the list's cursor and go to the prompt.
    fn open_selected(&mut self, ctx: &mut Ctx) {
        if let Some(s) = self.list.selected().and_then(|i| self.sessions.get(i)).cloned() {
            self.open_session(s, ctx);
            ctx.focus(PROMPT);
        }
    }
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
        let msg = match msg.downcast::<crate::os::channel::AskAbout>() {
            Ok(ask) => {
                // VAULT's M: the next message goes with this note.
                self.prompt.placeholder = format!("ask Mabel about “{}”…", ask.name);
                self.pending_note = Some((ask.name, ask.rel, ask.content));
                ctx.focus(PROMPT);
                return;
            }
            Err(other) => other,
        };
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
