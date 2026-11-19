//! Drawing INBOX: the chat list (with networks and unread counts) and the conversation.

use super::*;

impl Inbox {
    // ── drawing ──────────────────────────────────────────────────────────

    /// The chat list's title, with the filters that are on.
    pub(super) fn chats_title(&self) -> String {
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

    /// The chat list panel (or why it's empty: connecting, no token, offline).
    pub(super) fn draw_chats(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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

    /// The chats that fit, scrolled so the cursor stays in view: name, time, preview,
    /// network and unread count.
    pub(super) fn chat_lines(&mut self, pal: &Palette, height: usize, w: usize, focused: bool) -> Vec<Line<'static>> {
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

    /// The conversation (messages wrapped, with day lines, the picked one marked) and the
    /// compose line under it.
    pub(super) fn draw_messages(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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
