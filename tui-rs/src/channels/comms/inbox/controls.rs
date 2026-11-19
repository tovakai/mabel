//! INBOX's keys: in the chat list and in the conversation.

use super::*;

impl Inbox {
    // ── keys ─────────────────────────────────────────────────────────────

    /// A key in the chat list (the list is at the top of `mod.rs`).
    pub(super) fn chat_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
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

    /// A key in the conversation: pick a message, reply, edit, react, mark unread, back.
    pub(super) fn message_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
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
    pub(super) fn scroll_messages(&mut self, step: isize) {
        let n = self.messages.len();
        if n == 0 {
            return;
        }
        let cur = self.msg_index.unwrap_or(n - 1) as isize + step;
        self.msg_index =
            if step > 0 && cur >= n as isize - 1 { None } else { Some(cur.clamp(0, n as isize - 1) as usize) };
    }
}
