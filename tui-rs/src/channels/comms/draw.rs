//! Drawing COMMS ▸ MABEL: the conversation (Markdown), the chat list, her face and state, and
//! approval cards.

use super::*;

impl Comms {
    // ── drawing ──────────────────────────────────────────────────────────

    /// One conversation item as lines (a message's are cached until its text or the width changes).
    pub(super) fn item_lines(item: &mut Item, width: u16, pal: &Palette) -> Vec<Line<'static>> {
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

    /// The conversation, scrolled (`scroll_up` lines from the bottom), with a scrollbar.
    pub(super) fn render_log(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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

    /// Her chats.
    pub(super) fn render_list(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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

    /// Her face (talking or idle) and her state line, when there's room.
    pub(super) fn render_side(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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
pub(super) fn button_spots() -> ((u16, u16), (u16, u16)) {
    ((2, 18), (22, 12))
}

/// An approval card's lines: what she wants to do, and the allow / deny buttons.
pub(super) fn approval_lines(card: &Value, width: u16, pal: &Palette) -> Vec<Line<'static>> {
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
