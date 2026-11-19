//! Drawing AUDIO: the browser (search and results) and the deck (now playing, cover, queue).

use super::*;

impl Audio {
    // ── drawing ──────────────────────────────────────────────────────────

    /// One row's five columns (what they hold depends on its kind: names, counts, duration),
    /// each fitted to its width.
    pub(super) fn row_cells(&self, kind: Kind, item: &Value, widths: &[usize; 5]) -> [String; 5] {
        let cells: [String; 5] = match kind {
            Kind::Song => [
                if self.downloaded.contains(s(item, "id")) { "↓" } else { "♪" }.into(),
                s(item, "title").into(),
                s(item, "artist").into(),
                s(item, "album").into(),
                time(item["duration"].as_f64().unwrap_or(0.0)),
            ],
            Kind::Album => [
                "◫".into(),
                s(item, "name").into(),
                s(item, "artist").into(),
                item["year"].as_u64().map(|y| y.to_string()).unwrap_or_default(),
                item["songCount"].as_u64().map(|n| format!("{n} tr")).unwrap_or_default(),
            ],
            Kind::Playlist => [
                "♫".into(),
                s(item, "name").into(),
                s(item, "owner").into(),
                format!("{} songs", item["songCount"].as_u64().unwrap_or(0)),
                time(item["duration"].as_f64().unwrap_or(0.0)),
            ],
            Kind::Artist => [
                "◉".into(),
                s(item, "name").into(),
                String::new(),
                item["albumCount"].as_u64().map(|n| format!("{n} albums")).unwrap_or_default(),
                String::new(),
            ],
            Kind::Offline => {
                let (have, total) = (item["have"].as_u64().unwrap_or(0), item["total"].as_u64().unwrap_or(0));
                let kind = s(item, "kind");
                [
                    "⇩".into(),
                    s(item, "name").into(),
                    s(item, "artist").into(),
                    if have < total { format!("{kind} · {have}/{total}") } else { kind.to_string() },
                    format!("{total} tr"),
                ]
            }
        };
        let mut i = 0;
        cells.map(|c| {
            let out = fit(&c, widths[i]);
            i += 1;
            out
        })
    }

    /// The left side: the search field and the results table (with ↓ on downloaded songs and
    /// the download progress at the bottom). Returns where the two sections went.
    pub(super) fn render_browser(&mut self, frame: &mut Frame, area: Rect, view: &View) -> (Rect, Rect) {
        let pal = view.pal;
        // The search box has its own border; the library's border stands for the results.
        let lit = view.panel_state(RESULTS) == PanelState::Lit;
        let inside = view.inside(SEARCH) || view.inside(RESULTS);
        let state = if lit {
            PanelState::Lit
        } else if inside {
            PanelState::Inside
        } else {
            PanelState::Idle
        };
        let mut block = ui::panel(pal, &self.screen.title, state);
        if !self.progress.is_empty() {
            block = block.title_bottom(Line::styled(format!(" {} ", self.progress), bold(pal.teal)).right_aligned());
        }
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [search, _, results] =
            Layout::vertical([Constraint::Length(3), Constraint::Length(1), Constraint::Fill(1)]).areas(inner);

        let sblock = ui::panel(pal, "", view.panel_state(SEARCH));
        let field = sblock.inner(search);
        frame.render_widget(sblock, search);
        let placeholder = if view.panel_state(SEARCH) == PanelState::Lit { pal.amber_dim } else { pal.amber_faint };
        self.search.render(
            frame,
            field,
            Style::new().fg(pal.amber_hi),
            Style::new().fg(placeholder),
            view.inside(SEARCH),
        );

        // Columns: mark, title, artist, album · year, time; the title takes what's left.
        let w = results.width as usize;
        let fixed = 2 + 22 + 22 + 6 + 4;
        let title_w = w.saturating_sub(fixed).clamp(8, 48);
        let widths = [2, title_w, 22, 22, 6];
        let header =
            Row::new(["", "TITLE", "ARTIST", "ALBUM · YEAR", "TIME"].map(|h| Line::styled(h, bold(pal.beige))))
                .style(Style::new().bg(pal.amber_faint));
        let rows: Vec<Row> =
            self.screen.rows.iter().map(|(k, item)| Row::new(self.row_cells(*k, item, &widths))).collect();
        let cursor = if view.inside(RESULTS) {
            Style::new().bg(pal.amber).fg(pal.bg)
        } else {
            Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
        };
        let table = Table::new(rows, widths.map(|w| Constraint::Length(w as u16)))
            .header(header)
            .column_spacing(1)
            .style(Style::new().fg(pal.amber))
            .row_highlight_style(cursor);
        self.page = (results.height as usize).saturating_sub(2).max(1);
        frame.render_stateful_widget(table, results, &mut self.table);
        if self.screen.rows.is_empty() {
            let note = Rect { y: results.y + 1, height: 1, ..results };
            frame.render_widget(Paragraph::new(Span::styled(" loading…", Style::new().fg(pal.amber_dim))), note);
        }
        (search, results)
    }

    /// The right side: the cover, what's playing (with a progress bar, volume, shuffle), and
    /// the queue around the playing song.
    pub(super) fn render_deck(&mut self, frame: &mut Frame, area: Rect, view: &View, player: &Player) {
        let pal = view.pal;
        let block = ui::panel(pal, "DECK", view.panel_state(DECK));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        // The cover takes room only when there is one.
        let has_cover = self.cover.is_some();
        let (cover_area, info_area) = if view.narrow {
            let [c, rest] =
                Layout::horizontal([Constraint::Length(if has_cover { 24 } else { 0 }), Constraint::Fill(1)])
                    .areas(inner);
            (c, rest)
        } else {
            let [c, rest] = Layout::vertical([Constraint::Length(if has_cover { 15 } else { 0 }), Constraint::Fill(1)])
                .areas(inner);
            (c, rest)
        };
        if let Some(p) = &mut self.cover {
            let max_h = if view.narrow { 11 } else { 14 };
            let w = if view.narrow { 22 } else { 30 };
            let spot = Rect { width: w.min(cover_area.width), ..cover_area };
            p.render(spot, frame.buffer_mut(), max_h, 0);
        }

        let (now, queue_lines) = player.with(|st| {
            let mut now: Vec<Line> = vec![];
            match st.current() {
                Some(song) => {
                    let mark = if st.paused { "❚❚ " } else { "▶ " };
                    now.push(Line::styled(format!("{mark}{}", s(song, "title")), bold(pal.amber_hi)));
                    now.push(Line::styled(s(song, "artist").to_string(), Style::new().fg(pal.amber)));
                    now.push(Line::styled(s(song, "album").to_string(), Style::new().fg(pal.amber_dim)));
                    let shuffle = if st.shuffle { "   ⤨ shuffle" } else { "" };
                    now.push(Line::styled(
                        format!("{} / {}   vol {}{shuffle}", time(st.position), time(st.length()), st.volume),
                        Style::new().fg(pal.teal),
                    ));
                }
                None => {
                    now.push(Line::styled("NO TAPE IN THE DECK", bold(pal.amber_dim)));
                    now.push(Line::styled("pick something on the left", Style::new().fg(pal.amber_faint)));
                }
            }
            let progress = if st.length() > 0.0 { (st.position / st.length()).clamp(0.0, 1.0) } else { 0.0 };
            // The queue: a window around the current song, not all 800 of them.
            let at = st.index.unwrap_or(0);
            let start = at.saturating_sub(QUEUE_BEFORE);
            let end = (at + QUEUE_AFTER + 1).min(st.queue.len());
            let mut q: Vec<(Option<usize>, String, bool)> = (start..end)
                .map(|i| {
                    let song = &st.queue[i];
                    let on = st.index == Some(i);
                    let artist: String = s(song, "artist").chars().take(18).collect();
                    let title: String = s(song, "title").chars().take(32).collect();
                    (Some(i), format!("{} {title}  ·  {artist}", if on { "▶" } else { " " }), on)
                })
                .collect();
            if end < st.queue.len() {
                q.push((None, format!("  … {} more", st.queue.len() - end), false));
            }
            ((now, progress), q)
        });
        let (now_lines, progress) = now;
        let [np, _, bar, _, queue] = Layout::vertical([
            Constraint::Length(now_lines.len() as u16),
            Constraint::Length(if view.narrow { 0 } else { 1 }),
            Constraint::Length(1),
            Constraint::Length(if view.narrow { 0 } else { 1 }),
            Constraint::Fill(1),
        ])
        .areas(info_area);
        frame.render_widget(Paragraph::new(now_lines), np);
        let filled = (progress * bar.width as f64).round() as usize;
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled("━".repeat(filled), Style::new().fg(pal.amber)),
                Span::styled("━".repeat((bar.width as usize).saturating_sub(filled)), Style::new().fg(pal.amber_faint)),
            ])),
            bar,
        );

        self.queue_offset = queue_lines.first().and_then(|(i, _, _)| *i).unwrap_or(0);
        let shown = queue_lines.len();
        self.queue_cursor = self.queue_cursor.min(shown.saturating_sub(1));
        let inside = view.inside(DECK);
        let rows = queue.height as usize;
        let skip = (self.queue_cursor + 1).saturating_sub(rows);
        let lines: Vec<Line> = queue_lines
            .into_iter()
            .enumerate()
            .skip(skip)
            .take(rows)
            .map(|(n, (_, text, on))| {
                let style = if inside && n == self.queue_cursor {
                    Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
                } else if on {
                    bold(pal.amber_hi)
                } else {
                    Style::new().fg(pal.amber)
                };
                Line::styled(fit(&text, queue.width as usize), style)
            })
            .collect();
        frame.render_widget(Paragraph::new(lines), queue);
    }
}
