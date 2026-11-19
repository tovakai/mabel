//! TAPES's LIBRARY: finding tapes (dev folders, installed ones, Mabel's repo), downloading
//! and installing, and drawing the list and the tape's card.

use super::*;

impl Tapes {
    /// Ask Mabel what's in the tape repo (the answer arrives in on_msg).
    pub(super) fn fetch_remote(&self, ctx: &mut Ctx) {
        ctx.spawn("tapes", |api| async move {
            let result = api.tapes().await.map(|list| list.iter().filter_map(Remote::from_json).collect());
            Note::Remote(result.map_err(|e| format!("{e:#}")))
        });
    }

    /// Download a tape from Mabel and install it (`play`: start it once installed).
    pub(super) fn download(&mut self, ctx: &mut Ctx, name: String, play: bool) {
        if self.installing.is_some() {
            return;
        }
        self.installing = Some(name.clone());
        ctx.status(&format!("installing {name}…"));
        ctx.spawn("tapes", move |api| async move {
            let result = api.tape_download(&name).await.map_err(|e| format!("{e:#}"));
            Note::Installed { name, result, play }
        });
    }

    /// Rebuild the LIBRARY: dev tapes (re-reading `tapes_dev` from config.toml), installed
    /// ones (marking those with a newer copy on Mabel), then Mabel's that aren't installed.
    pub(super) fn scan(&mut self) {
        // config.toml again: `mabel-os new-tape` adds to tapes_dev while MABEL-OS runs.
        let fresh =
            std::fs::read_to_string(crate::os::config::config_path()).ok().and_then(|t| t.parse::<toml::Table>().ok());
        if let Some(table) = fresh {
            self.dev_dirs = dev_dirs(&table);
        }
        let keep = self.entries.get(self.sel).map(|e| e.dir.clone());
        let mut entries: Vec<Entry> =
            self.dev_dirs.iter().filter(|d| d.is_dir()).map(|d| Entry::read(d, Kind::Dev)).collect();
        let mut installed: Vec<PathBuf> = std::fs::read_dir(installed_dir())
            .map(|rd| {
                rd.flatten()
                    .map(|e| e.path())
                    .filter(|p| p.is_dir() && !p.file_name().is_some_and(|n| n.to_string_lossy().starts_with('.')))
                    .collect()
            })
            .unwrap_or_default();
        installed.sort();
        for dir in &installed {
            let mut e = Entry::read(dir, Kind::Installed);
            let have = std::fs::read_to_string(dir.join(SHA_FILE)).unwrap_or_default();
            e.update = self
                .remote
                .iter()
                .flatten()
                .any(|r| r.name == e.key() && !have.trim().is_empty() && r.sha != have.trim());
            entries.push(e);
        }
        for r in self.remote.iter().flatten() {
            if !installed.iter().any(|d| d.file_name().is_some_and(|n| n.to_string_lossy() == r.name)) {
                entries.push(Entry::remote(r));
            }
        }
        self.entries = entries;
        self.sel = keep.and_then(|k| self.entries.iter().position(|e| e.dir == k)).unwrap_or(0);
        self.scanned = true;
    }

    /// r: scan again and ask Mabel for her list again.
    pub(super) fn rescan(&mut self, ctx: &mut Ctx) {
        self.scan();
        self.fetch_remote(ctx);
        ctx.status(&format!("{} tapes · asking Mabel for hers", self.entries.len()));
    }

    /// LIBRARY on the left, the selected tape's card on the right (stacked on the deck).
    pub(super) fn render_library(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        let (list, detail) = if view.narrow {
            let [a, b] = Layout::vertical([Constraint::Percentage(40), Constraint::Fill(1)]).areas(area);
            (a, b)
        } else {
            let [a, b] = Layout::horizontal([Constraint::Percentage(38), Constraint::Fill(1)]).areas(area);
            (a, b)
        };

        let block = ui::panel(pal, "LIBRARY", view.panel_state(LIST));
        let inner = block.inner(list);
        frame.render_widget(block, list);
        let mut lines = vec![];
        if self.entries.is_empty() {
            lines.push(Line::styled(" No DreamTapes yet.", bold(pal.beige)));
            lines.push(Line::raw(""));
            lines.push(Line::styled(" Make one:  mabel-os new-tape snake", Style::new().fg(pal.amber)));
            lines.push(Line::styled(" (on the PC or laptop; then r here)", Style::new().fg(pal.amber_dim)));
            lines.push(Line::styled(" Share one: mabel-os tape publish", Style::new().fg(pal.amber)));
            lines.push(Line::styled(" (then it shows up here on every device)", Style::new().fg(pal.amber_dim)));
        }
        let rows = inner.height as usize;
        let top = self.sel.saturating_sub(rows.saturating_sub(1));
        for (i, e) in self.entries.iter().enumerate().skip(top).take(rows) {
            let on = i == self.sel;
            let style = if on && view.inside(LIST) {
                bold(pal.bg).bg(pal.amber)
            } else if on {
                bold(pal.amber_hi)
            } else if e.kind == Kind::Remote {
                Style::new().fg(pal.amber_dim)
            } else {
                Style::new().fg(pal.amber)
            };
            let mark = if e.kind == Kind::Remote { "○" } else { "◘" };
            let mut spans = vec![Span::styled(format!(" {mark} {} ", e.title()), style)];
            match e.kind {
                Kind::Dev => spans.push(Span::styled(" DEV", Style::new().fg(pal.teal))),
                Kind::Remote => spans.push(Span::styled(" ON MABEL", Style::new().fg(pal.sand))),
                Kind::Installed if e.update => spans.push(Span::styled(" ▲ NEWER ON MABEL", Style::new().fg(pal.sand))),
                Kind::Installed => {}
            }
            if self.installing.as_deref() == Some(e.key().as_str()) {
                spans.push(Span::styled(" ↓ installing…", Style::new().fg(pal.mint)));
            }
            if e.info.is_err() {
                spans.push(Span::styled(" !", Style::new().fg(pal.red)));
            }
            lines.push(Line::from(spans));
        }
        if let Some(err) = &self.remote_error {
            lines.push(Line::raw(""));
            lines.push(Line::styled(format!(" Mabel's tapes: {err}"), Style::new().fg(pal.amber_dim)));
        }
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);

        let block = ui::panel(pal, "TAPE", view.panel_state(CARD));
        let inner = block.inner(detail);
        frame.render_widget(block, detail);
        if let Some(e) = self.entries.get(self.sel) {
            self.render_card(frame, inner, pal, e);
        }
        vec![list, detail]
    }

    /// A tape's card: cover, title, author, description, last played, screen and fps, where it
    /// comes from, and the last run's console.
    pub(super) fn render_card(&self, frame: &mut Frame, area: Rect, pal: &Palette, e: &Entry) {
        let mut y = area.y;
        if let Some(cover) = &e.cover {
            let max_rows = (area.height / 3).max(4) as usize;
            let scale = (area.width as usize / cover.w.max(1)).min(max_rows * 2 / cover.h.max(1)).clamp(1, 4);
            let mut px = Pixels::new(cover.w, cover.h);
            px.cls(rgb(pal.panel));
            cover.draw(&mut px, 0, 0, 0, false, false);
            let rows = (cover.h * scale).div_ceil(2) as u16;
            let place = Rect { x: area.x + 1, y, width: area.width.saturating_sub(1), height: rows.min(area.height) };
            mabel_console::render(&px, &[], frame.buffer_mut(), place, scale);
            y += rows + 1;
        }
        let dim = Style::new().fg(pal.amber_dim);
        let mut lines = vec![];
        match &e.info {
            Ok(info) => {
                lines.push(Line::styled(format!(" {}", info.title), bold(pal.beige)));
                if !info.author.is_empty() {
                    lines.push(Line::styled(format!(" by {}", info.author), dim));
                }
                if !info.description.is_empty() {
                    lines.push(Line::raw(""));
                    lines.push(Line::styled(format!(" {}", info.description), Style::new().fg(pal.amber)));
                }
                lines.push(Line::raw(""));
                let played = self.played.get(&e.key()).and_then(|t| chrono::DateTime::from_timestamp(*t, 0));
                let when = played
                    .map(|t| t.with_timezone(&chrono::Local).format("%d %b %Y %H:%M").to_string())
                    .unwrap_or_else(|| "never".into());
                lines.push(Line::styled(format!(" last played: {when}"), dim));
                let screen = match info.screen {
                    Screen::Fill => "fills the screen",
                    Screen::Fixed => "128×72",
                };
                lines.push(Line::styled(format!(" {screen} · {} fps", info.fps), dim));
                match e.kind {
                    Kind::Dev => {
                        let built = if e.wasm.exists() { "built" } else { "not built yet: cargo tape" };
                        lines.push(Line::styled(
                            format!(" DEV · {} · {built}", e.dir.display()),
                            Style::new().fg(pal.teal),
                        ));
                    }
                    Kind::Remote => {
                        lines.push(Line::styled(" ON MABEL: not on this device yet", Style::new().fg(pal.sand)));
                    }
                    Kind::Installed if e.update => {
                        lines.push(Line::styled(" ▲ Mabel has a newer version: u updates", Style::new().fg(pal.sand)));
                    }
                    Kind::Installed => {}
                }
                lines.push(Line::raw(""));
                let action = if e.kind == Kind::Remote { " enter: install and play" } else { " enter: play" };
                lines.push(Line::styled(action, Style::new().fg(pal.amber)));
            }
            Err(err) => lines.push(Line::styled(format!(" {err}"), Style::new().fg(pal.red))),
        }
        let is_last = self.run.is_none() && !self.console.is_empty() && Some(&e.dir) == self.last_run.as_ref();
        if is_last {
            lines.push(Line::raw(""));
            lines.push(Line::styled(" CONSOLE (last run)", bold(pal.beige)));
            let room = (area.height as usize).saturating_sub(lines.len() + (y - area.y) as usize).max(1);
            for l in self.console.iter().rev().take(room).rev() {
                let colour = if l.starts_with("crashed") || l.starts_with("panic") { pal.red } else { pal.amber_dim };
                lines.push(Line::styled(format!(" {l}"), Style::new().fg(colour)));
            }
        }
        let rest = Rect { y, height: area.height.saturating_sub(y - area.y), ..area };
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), rest);
    }
}
