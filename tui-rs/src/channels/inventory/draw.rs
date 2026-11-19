//! Drawing INVENTORY: laying the blocks out into lines (remembering where each clickable thing
//! went), the main page, and the item panel.

use super::*;

impl Inventory {
    // ── drawing ──────────────────────────────────────────────────────────

    /// Lay the blocks out at `width`: lines, and which target sits on which lines and columns.
    pub(super) fn layout(&self, width: usize, pal: &Palette) -> (Vec<Line<'static>>, Vec<Place>) {
        let mut lines: Vec<Line<'static>> = vec![];
        let mut places = vec![];
        let mut t = 0;
        let per_row = if width >= 120 { 4 } else { 3 };
        for block in &self.blocks {
            match block {
                Block::Text(l) => lines.extend(wrap::wrap_lines(std::slice::from_ref(l), width.max(1))),
                Block::Map(zi) => lines.extend(self.draw_grid(&self.page.zones[*zi], width, pal)),
                Block::Select(entry, _) => {
                    let selected = t == self.sel;
                    let wrapped = wrap::wrap_lines(entry, width.max(1));
                    places.push(Place {
                        target: t,
                        line: lines.len(),
                        x: 0,
                        width: width as u16,
                        height: wrapped.len().max(1),
                    });
                    for l in wrapped {
                        lines.push(if selected { l.style(Style::new().bg(pal.amber_faint)) } else { l });
                    }
                    t += 1;
                }
                Block::Cards(cards) => {
                    let gap = 2;
                    let cw = ((width + gap) / per_row).saturating_sub(gap).max(12);
                    for row in cards.chunks(per_row) {
                        let top = lines.len();
                        let mut rows: Vec<Vec<Span<'static>>> = vec![vec![]; CARD_H];
                        for (n, card) in row.iter().enumerate() {
                            let selected = t == self.sel;
                            let (edge, bg) =
                                if selected { (pal.amber, pal.amber_faint) } else { (pal.amber_dim, pal.panel) };
                            let fit = |s: &str, w: usize| {
                                let mut out = String::new();
                                for c in s.chars() {
                                    if width_of(&out) + width_of(&c.to_string()) > w {
                                        break;
                                    }
                                    out.push(c);
                                }
                                let pad = w.saturating_sub(width_of(&out));
                                out + &" ".repeat(pad)
                            };
                            let inner = cw.saturating_sub(4);
                            let count = if card.count > 0 { format!("  · {}", card.count) } else { String::new() };
                            let code: String = card.code.chars().take(34).collect();
                            let code_w = inner.saturating_sub(width_of(&count));
                            rows[0].push(Span::styled(format!("╭{}╮", "─".repeat(cw - 2)), Style::new().fg(edge)));
                            rows[1].push(Span::styled("│ ", Style::new().fg(edge).bg(bg)));
                            rows[1].push(Span::styled(fit(&card.title, inner), bold(pal.amber_hi).bg(bg)));
                            rows[1].push(Span::styled(" │", Style::new().fg(edge).bg(bg)));
                            rows[2].push(Span::styled("│ ", Style::new().fg(edge).bg(bg)));
                            rows[2].push(Span::styled(fit(&code, code_w), Style::new().fg(pal.beige_dark).bg(bg)));
                            rows[2].push(Span::styled(
                                fit(&count, inner - code_w.min(inner)),
                                Style::new().fg(pal.teal).bg(bg),
                            ));
                            rows[2].push(Span::styled(" │", Style::new().fg(edge).bg(bg)));
                            rows[3].push(Span::styled(format!("╰{}╯", "─".repeat(cw - 2)), Style::new().fg(edge)));
                            if n + 1 < row.len() {
                                for r in rows.iter_mut() {
                                    r.push(Span::raw(" ".repeat(gap)));
                                }
                            }
                            places.push(Place {
                                target: t,
                                line: top,
                                x: (n * (cw + gap)) as u16,
                                width: cw as u16,
                                height: CARD_H,
                            });
                            t += 1;
                        }
                        lines.extend(rows.into_iter().map(Line::from));
                    }
                    lines.push(Line::raw(""));
                }
            }
        }
        (lines, places)
    }

    /// The main panel; returns where the search box and the body were drawn (its sections).
    pub(super) fn render_main(&mut self, frame: &mut Frame, area: Rect, view: &View) -> (Rect, Rect) {
        let pal = view.pal;
        let lit = view.panel_state(BODY) == PanelState::Lit;
        let inside = view.inside(SEARCH) || view.inside(BODY);
        let state = if lit {
            PanelState::Lit
        } else if inside {
            PanelState::Inside
        } else {
            PanelState::Idle
        };
        let block = ui::panel(pal, &self.title, state);
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [search, crumbs, _, body] = Layout::vertical([
            Constraint::Length(3),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Fill(1),
        ])
        .areas(inner);
        let sblock = ui::panel(pal, "", view.panel_state(SEARCH));
        let field = sblock.inner(search);
        frame.render_widget(sblock, search);
        let ph = if view.panel_state(SEARCH) == PanelState::Lit { pal.amber_dim } else { pal.amber_faint };
        self.search.render(frame, field, Style::new().fg(pal.amber_hi), Style::new().fg(ph), view.inside(SEARCH));

        // Breadcrumbs: each step clickable.
        self.crumb_hits.clear();
        if let Some(w) = &self.world {
            if self.mode == Mode::Browse {
                let mut spans = vec![];
                let mut x = crumbs.x;
                for (n, step) in w.chain(&self.route).iter().enumerate() {
                    if n > 0 {
                        spans.push(Span::styled("  ›  ", Style::new().fg(pal.amber_faint)));
                        x += 5;
                    }
                    let title = w.title(step);
                    let tw = width(&title) as u16;
                    if *step == self.route {
                        spans.push(Span::styled(title, bold(pal.amber_hi)));
                    } else {
                        spans.push(Span::styled(
                            title,
                            Style::new().fg(pal.amber_dim).add_modifier(Modifier::UNDERLINED),
                        ));
                        self.crumb_hits.push((Rect::new(x, crumbs.y, tw, 1), step.clone()));
                    }
                    x += tw;
                }
                spans.push(Span::styled(format!("     {}", w.code(&self.route)), Style::new().fg(pal.beige_dark)));
                frame.render_widget(Paragraph::new(Line::from(spans)), crumbs);
            }
        } else {
            let text = if self.error.is_empty() {
                "loading the Gremlin Index…".to_string()
            } else {
                format!("✖ can't reach the Gremlin Index: {}", self.error)
            };
            frame.render_widget(
                Paragraph::new(Line::styled(
                    text,
                    Style::new().fg(if self.error.is_empty() { pal.amber_dim } else { pal.red }),
                )),
                crumbs,
            );
        }

        let (lines, places) = self.layout(body.width.saturating_sub(1) as usize, pal);
        let h = body.height as usize;
        self.page_h = h;
        // Keep the selection in view.
        if let Some(p) = places.iter().find(|p| p.target == self.sel) {
            let last = p.line + p.height - 1;
            if p.line < self.scroll {
                self.scroll = p.line;
            } else if last >= self.scroll + h {
                self.scroll = last + 1 - h;
            }
        }
        self.scroll = self.scroll.min(lines.len().saturating_sub(h));
        self.hits = places
            .iter()
            .filter(|p| p.line >= self.scroll && p.line < self.scroll + h)
            .map(|p| {
                (Rect::new(body.x + p.x, body.y + (p.line - self.scroll) as u16, p.width, p.height as u16), p.target)
            })
            .collect();
        let shown: Vec<Line> = lines.into_iter().skip(self.scroll).take(h).collect();
        frame.render_widget(Paragraph::new(shown), body);
        (search, body)
    }

    /// The item panel: its photo (when there's room) and the card, scrollable.
    pub(super) fn render_item(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let title = self.item.as_ref().map_or("ITEM".to_string(), |d| format!("ITEM ░ #{}", value_text(&d["id"])));
        let block = ui::panel(pal, &title, view.panel_state(ITEM));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let mut y = inner.y;
        if let Some(p) = &mut self.photo {
            let max_h = if view.narrow { 11 } else { 14 };
            let max_w = if view.narrow { 22 } else { 28 };
            let spot = Rect { width: max_w.min(inner.width), height: inner.height, ..inner };
            y += p.render(spot, frame.buffer_mut(), max_h, 0) + 1;
        }
        let rest = Rect { y, height: inner.bottom().saturating_sub(y), ..inner };
        let lines = wrap::wrap_lines(&self.card_lines(pal), rest.width.max(1) as usize);
        self.card_scroll = self.card_scroll.min(lines.len().saturating_sub(rest.height as usize));
        frame.render_widget(Paragraph::new(lines).scroll((self.card_scroll as u16, 0)), rest);
    }
}

/// Where a target was laid out: its first line, column, width and height in lines.
pub(super) struct Place {
    target: usize,
    line: usize,
    x: u16,
    width: u16,
    height: usize,
}

/// How many cells `s` takes on screen.
pub(super) fn width_of(s: &str) -> usize {
    width(s)
}
