//! VAULT's pickers and questions (open a note, tags, new note, rename, delete…).

use super::*;

impl VaultView {
    // ── pickers and questions ────────────────────────────────────────────

    /// What a picker offers for `query`: notes matching it, or (for f) this note's links.
    /// Each choice is (what's chosen, what's shown).
    pub(super) fn choices(&self, purpose: &PickFor, query: &str) -> Vec<(String, String)> {
        let Some(v) = &self.vault else { return vec![] };
        match purpose {
            PickFor::Follow => {
                let note = self.note.as_ref().and_then(|r| v.notes.get(r));
                let mut out: Vec<(String, String)> = vec![];
                for t in note.map(|n| n.links.clone()).unwrap_or_default() {
                    if t.to_lowercase().contains(&query.to_lowercase()) && !out.iter().any(|(k, _)| *k == t) {
                        out.push((t.clone(), t));
                    }
                }
                out
            }
            _ => v
                .find_notes(query, 60)
                .into_iter()
                .map(|n| (n.rel.clone(), format!("{}   {}", n.name, n.folder())))
                .collect(),
        }
    }

    /// Open a picker titled `title`.
    pub(super) fn picker(&mut self, title: &str, purpose: PickFor) {
        let options = self.choices(&purpose, "");
        self.popup =
            Some(Popup::Picker { title: title.into(), input: Input::new("type to filter"), options, at: 0, purpose });
    }

    /// A picker's choice (None: cancelled): open the note, insert a link, or follow it.
    pub(super) fn picked(&mut self, purpose: PickFor, choice: Option<String>, ctx: &mut Ctx) {
        match (purpose, choice) {
            (PickFor::Open, Some(rel)) => self.open_note(&rel, None, true, ctx),
            (PickFor::Follow, Some(target)) => {
                let note = self.note.as_ref().and_then(|r| self.vault.as_ref()?.notes.get(r)).cloned();
                let found = self.vault.as_ref().and_then(|v| v.resolve(&target, note.as_ref()));
                self.open_note(found.as_deref().unwrap_or(&target), None, true, ctx);
            }
            (PickFor::Link, choice) => {
                let name = choice.and_then(|rel| self.vault.as_ref()?.notes.get(&rel).map(|n| n.name.clone()));
                if let (Some(ed), Some(name)) = (&mut self.editor, name) {
                    ed.insert(&format!("{name}]]"));
                }
                ctx.focus(NOTE);
            }
            _ => {}
        }
    }

    /// Keys while a popup is open: typing filters a picker, j/k or arrows move, enter chooses,
    /// esc closes. The other popups ask one question (search, new note's name, new name).
    pub(super) fn popup_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) {
        let Some(popup) = self.popup.take() else { return };
        match popup {
            Popup::Picker { title, mut input, mut options, mut at, purpose } => {
                match key.code {
                    KeyCode::Esc => return self.picked(purpose, None, ctx),
                    KeyCode::Enter => {
                        let choice = options.get(at).map(|(k, _)| k.clone());
                        return self.picked(purpose, choice, ctx);
                    }
                    KeyCode::Down => at = (at + 1).min(options.len().saturating_sub(1)),
                    KeyCode::Up => at = at.saturating_sub(1),
                    _ => {
                        if input.key(key) == InputResult::Used {
                            options = self.choices(&purpose, &input.value);
                            at = 0;
                        }
                    }
                }
                self.popup = Some(Popup::Picker { title, input, options, at, purpose });
            }
            Popup::Search(mut ask) => match ask.key(key) {
                Answer::Open => self.popup = Some(Popup::Search(ask)),
                Answer::Cancel => {}
                Answer::Chosen(q) if !q.is_empty() => {
                    self.explorer = Explorer::Search(q);
                    let pal = ctx.pal;
                    self.build_explorer(&pal);
                    ctx.focus(EXPLORER);
                }
                Answer::Chosen(_) => {}
            },
            Popup::NewNote(mut ask) => match ask.key(key) {
                Answer::Open => self.popup = Some(Popup::NewNote(ask)),
                Answer::Chosen(name) if !name.is_empty() => {
                    let folder = if self.explorer == Explorer::Files && !self.folder.is_empty() {
                        format!("{}/", self.folder)
                    } else {
                        String::new()
                    };
                    let title = name.rsplit('/').next().unwrap_or(&name).to_string();
                    let created =
                        self.vault.as_mut().map(|v| v.create(&format!("{folder}{name}"), &format!("# {title}\n\n")));
                    match created {
                        Some(Ok(rel)) => {
                            self.open_note(&rel, None, true, ctx);
                            self.edit(ctx);
                        }
                        Some(Err(e)) => ctx.error("VAULT", &e.to_string()),
                        None => {}
                    }
                }
                _ => {}
            },
            Popup::Rename(mut ask) => match ask.key(key) {
                Answer::Open => self.popup = Some(Popup::Rename(ask)),
                Answer::Chosen(name) if !name.is_empty() => {
                    let Some(rel) = self.note.clone() else { return };
                    let renamed = self.vault.as_mut().map(|v| v.rename(&rel, &name));
                    match renamed {
                        Some(Ok((new_rel, changed))) => {
                            ctx.toast("VAULT", &format!("✓ renamed; links updated in {changed} note(s)"));
                            self.open_note(&new_rel, None, false, ctx);
                        }
                        Some(Err(e)) => ctx.error("VAULT", &e.to_string()),
                        None => {}
                    }
                }
                _ => {}
            },
        }
    }

    /// Draw the open popup over the page.
    pub(super) fn render_popup(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        match &mut self.popup {
            Some(Popup::Picker { title, input, options, at, .. }) => {
                let h = (options.len() as u16 + 4).clamp(8, area.height * 7 / 10);
                let inner = frame_box(frame, area, pal, title, "enter picks · esc cancels", 70, h);
                let [q, _, list] =
                    Layout::vertical([Constraint::Length(1), Constraint::Length(1), Constraint::Fill(1)]).areas(inner);
                input.render(frame, q, Style::new().fg(pal.amber_hi), Style::new().fg(pal.amber_dim), true);
                let rows = list.height as usize;
                let top = (*at + 1).saturating_sub(rows);
                let lines: Vec<Line> = options
                    .iter()
                    .enumerate()
                    .skip(top)
                    .take(rows)
                    .map(|(i, (_, label))| {
                        let style = if i == *at {
                            Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
                        } else {
                            Style::new().fg(pal.amber)
                        };
                        Line::styled(label.clone(), style)
                    })
                    .collect();
                frame.render_widget(Paragraph::new(lines), list);
            }
            Some(Popup::Search(ask) | Popup::NewNote(ask) | Popup::Rename(ask)) => ask.render(frame, area, pal),
            None => {}
        }
    }
}
