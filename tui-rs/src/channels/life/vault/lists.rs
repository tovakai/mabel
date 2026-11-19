//! VAULT's lists: EXPLORER (folders, recent, tags, search results) and the side panel
//! (outline, links, backlinks), and the keys and drawing they share.

use super::*;

impl VaultView {
    // ── the explorer and the side panel ──────────────────────────────────

    /// A note as a list line (▶ if it's the open one), with its folder when `with_path`.
    pub(super) fn note_entry(&self, note: &Note, with_path: bool, pal: &Palette) -> Entry {
        let open = self.note.as_deref() == Some(note.rel.as_str());
        let mut spans = vec![
            Span::styled(if open { "▶ " } else { "• " }, Style::new().fg(pal.amber_dim)),
            Span::styled(note.name.clone(), Style::new().fg(pal.amber)),
        ];
        if with_path && !note.folder().is_empty() {
            spans.push(Span::styled(format!("  {}", note.folder()), Style::new().fg(pal.beige_dark)));
        }
        Entry { line: Line::from(spans), route: Some(Route::Note(note.rel.clone(), None)) }
    }

    /// A heading line in a list (not selectable).
    pub(super) fn heading(text: String, pal: &Palette) -> Entry {
        Entry { line: Line::styled(text, bold(pal.beige)), route: None }
    }

    /// Fill EXPLORER for what it's showing: a folder (subfolders, then notes), search results,
    /// all tags, one tag's notes, or recent notes.
    pub(super) fn build_explorer(&mut self, pal: &Palette) {
        let Some(v) = &self.vault else { return };
        let mut entries = vec![];
        match &self.explorer {
            Explorer::Files => {
                let prefix = if self.folder.is_empty() { String::new() } else { format!("{}/", self.folder) };
                let mut folders: Vec<(String, usize)> = vec![];
                let mut notes: Vec<&Note> = vec![];
                for (rel, note) in &v.notes {
                    let Some(rest) = rel.strip_prefix(&prefix) else { continue };
                    match rest.split_once('/') {
                        Some((f, _)) => match folders.iter_mut().find(|(n, _)| n == f) {
                            Some((_, c)) => *c += 1,
                            None => folders.push((f.to_string(), 1)),
                        },
                        None => notes.push(note),
                    }
                }
                entries.push(Self::heading(
                    if self.folder.is_empty() { "▸ /".into() } else { format!("▸ {}", self.folder) },
                    pal,
                ));
                if !self.folder.is_empty() {
                    let up = self.folder.rsplit_once('/').map_or(String::new(), |(f, _)| f.to_string());
                    entries.push(Entry {
                        line: Line::styled("◂ ..", Style::new().fg(pal.amber_dim)),
                        route: Some(Route::Dir(up)),
                    });
                }
                folders.sort_by_key(|(f, _)| f.to_lowercase());
                for (f, count) in folders {
                    entries.push(Entry {
                        line: Line::from(vec![
                            Span::styled("▤ ", Style::new().fg(pal.amber_dim)),
                            Span::styled(f.clone(), Style::new().fg(pal.amber_hi)),
                            Span::styled(format!("  {count}"), Style::new().fg(pal.beige_dark)),
                        ]),
                        route: Some(Route::Dir(format!("{prefix}{f}"))),
                    });
                }
                notes.sort_by_key(|n| n.name.to_lowercase());
                for n in notes {
                    entries.push(self.note_entry(n, false, pal));
                }
            }
            Explorer::Search(q) => {
                let hits = v.search(q, 100);
                entries.push(Self::heading(format!("“{q}” · {} lines", hits.len()), pal));
                for (rel, line, text) in hits {
                    let name = v.notes.get(&rel).map_or(rel.clone(), |n| n.name.clone());
                    entries.push(Entry {
                        line: Line::from(vec![
                            Span::styled(name, Style::new().fg(pal.amber_hi)),
                            Span::styled(format!(" :{}  ", line + 1), Style::new().fg(pal.beige_dark)),
                            Span::styled(text.chars().take(80).collect::<String>(), Style::new().fg(pal.amber)),
                        ]),
                        route: Some(Route::Note(rel, Some(line))),
                    });
                }
            }
            Explorer::Tags => {
                entries.push(Self::heading("TAGS".into(), pal));
                for (tag, notes) in v.tags() {
                    entries.push(Entry {
                        line: Line::from(vec![
                            Span::styled(format!("#{tag}"), Style::new().fg(pal.teal)),
                            Span::styled(format!("  {}", notes.len()), Style::new().fg(pal.beige_dark)),
                        ]),
                        route: Some(Route::Tag(tag)),
                    });
                }
            }
            Explorer::Tag(tag) => {
                let mut notes: Vec<&Note> = v.notes.values().filter(|n| n.tags.contains(tag)).collect();
                notes.sort_by_key(|n| n.name.to_lowercase());
                entries.push(Entry {
                    line: Line::styled(format!("#{tag} · {}", notes.len()), bold(pal.teal)),
                    route: None,
                });
                for n in notes {
                    entries.push(self.note_entry(n, true, pal));
                }
            }
            Explorer::Recent => {
                entries.push(Self::heading("RECENT".into(), pal));
                for n in v.find_notes("", 40) {
                    entries.push(self.note_entry(n, true, pal));
                }
            }
        }
        self.entries = entries;
        self.entry_at = self.entries.iter().position(|e| e.route.is_some()).unwrap_or(0);
    }

    /// Fill the side panel for the open note: its outline, its links, the notes linking to it
    /// (backlinks) and its tags.
    pub(super) fn build_side(&mut self, pal: &Palette) {
        let (Some(v), Some(rel)) = (&self.vault, &self.note) else {
            self.side.clear();
            return;
        };
        let Some(note) = v.notes.get(rel) else { return };
        let mut side = vec![Self::heading("OUTLINE".into(), pal)];
        for (level, text, line) in &note.headings {
            let colour = if *level <= 2 { pal.amber_hi } else { pal.amber };
            side.push(Entry {
                line: Line::styled(format!("{}{text}", "  ".repeat(level - 1)), Style::new().fg(colour)),
                route: Some(Route::Heading(*line)),
            });
        }
        if note.headings.is_empty() {
            side.push(Entry { line: Line::styled("no headings", Style::new().fg(pal.amber_faint)), route: None });
        }
        let back = v.backlinks(note);
        side.push(Entry { line: Line::raw(""), route: None });
        side.push(Self::heading(format!("BACKLINKS · {}", back.len()), pal));
        let back_entries: Vec<Entry> = back.iter().take(60).map(|n| self.note_entry(n, true, pal)).collect();
        side.extend(back_entries);
        let mut links: Vec<&String> = vec![];
        for l in &note.links {
            if !links.contains(&l) {
                links.push(l);
            }
        }
        side.push(Entry { line: Line::raw(""), route: None });
        side.push(Self::heading(format!("LINKS · {}", links.len()), pal));
        for target in links.into_iter().take(60) {
            let found = v.resolve(target, Some(note));
            let colour = if found.is_some() { pal.amber } else { pal.amber_faint };
            let route = match found {
                Some(rel) => Route::Note(rel, None),
                None => Route::Missing(target.clone()),
            };
            side.push(Entry { line: Line::styled(target.clone(), Style::new().fg(colour)), route: Some(route) });
        }
        if !note.tags.is_empty() {
            side.push(Entry { line: Line::raw(""), route: None });
            side.push(Self::heading("TAGS".into(), pal));
            let mut tags = note.tags.clone();
            tags.sort();
            for t in tags {
                side.push(Entry {
                    line: Line::styled(format!("#{t}"), Style::new().fg(pal.teal)),
                    route: Some(Route::Tag(t)),
                });
            }
        }
        self.side = side;
        self.side_at = self.side.iter().position(|e| e.route.is_some()).unwrap_or(0);
    }

    /// Moving in a list, skipping headings. Some(Some(route)): enter chose a line. Some(None):
    /// the cursor moved. None: not a list key.
    pub(super) fn list_key(list: &[Entry], at: &mut usize, key: &KeyEvent, page: usize) -> Option<Option<Route>> {
        let selectable: Vec<usize> =
            list.iter().enumerate().filter(|(_, e)| e.route.is_some()).map(|(i, _)| i).collect();
        if selectable.is_empty() {
            return None;
        }
        let pos = selectable.iter().position(|i| i == at).unwrap_or(0);
        let go = |p: isize| selectable[p.clamp(0, selectable.len() as isize - 1) as usize];
        match key.code {
            KeyCode::Down => *at = go(pos as isize + 1),
            KeyCode::Up => *at = go(pos as isize - 1),
            KeyCode::PageDown => *at = go(pos as isize + page as isize),
            KeyCode::PageUp => *at = go(pos as isize - page as isize),
            KeyCode::Home => *at = selectable[0],
            KeyCode::End => *at = *selectable.last().unwrap_or(&0),
            KeyCode::Enter => return Some(list.get(*at).and_then(|e| e.route.clone())),
            _ => return None,
        }
        Some(None)
    }

    /// Draw a list with its cursor, scrolled so the cursor stays in view.
    pub(super) fn draw_list(frame: &mut Frame, area: Rect, list: &[Entry], at: usize, focused: bool, pal: &Palette) {
        let h = area.height as usize;
        let top = (at + 1).saturating_sub(h);
        let lines: Vec<Line> = list
            .iter()
            .enumerate()
            .skip(top)
            .take(h)
            .map(|(i, e)| {
                let mut line = e.line.clone();
                if i == at && e.route.is_some() {
                    line = line.style(if focused {
                        Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
                    } else {
                        Style::new().bg(pal.panel)
                    });
                }
                line
            })
            .collect();
        frame.render_widget(Paragraph::new(lines), area);
    }
}
