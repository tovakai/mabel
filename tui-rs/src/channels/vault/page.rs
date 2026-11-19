//! VAULT's note page: opening a note (and following links), the vim editor, saving, going back,
//! and drawing it.

use super::*;

impl VaultView {
    // ── the note ─────────────────────────────────────────────────────────

    /// Open a note by path or by name (as `[[links]]` name them), at `line` if given.
    /// `remember` puts the note you were on into the back history. An open editor saves first.
    pub(super) fn open_note(&mut self, target: &str, line: Option<usize>, remember: bool, ctx: &mut Ctx) {
        let Some(v) = &self.vault else { return };
        let found = if v.notes.contains_key(target) { Some(target.to_string()) } else { v.resolve(target, None) };
        let Some(rel) = found else {
            ctx.toast("VAULT", &format!("No note “{target}” in {}", self.vault_name));
            return;
        };
        if self.editor.is_some() {
            self.leave_editor(true, ctx);
        }
        if remember && let Some(cur) = self.note.clone().filter(|c| *c != rel) {
            self.history.push(cur);
        }
        self.text = self.vault.as_ref().map(|v| v.read(&rel)).unwrap_or_default();
        self.note = Some(rel);
        self.rendered = None;
        self.scroll = 0;
        self.jump_to = line;
        let pal = ctx.pal;
        self.build_side(&pal);
        if self.explorer == Explorer::Files || matches!(self.explorer, Explorer::Recent | Explorer::Tag(_)) {
            let at = self.entry_at;
            self.build_explorer(&pal);
            self.entry_at = at.min(self.entries.len().saturating_sub(1));
        }
    }

    /// e (or i inside the note): open the note in the vim editor.
    pub(super) fn edit(&mut self, ctx: &mut Ctx) {
        let (Some(v), Some(rel)) = (&self.vault, &self.note) else { return };
        self.text = v.read(rel);
        self.editor = Some(Vim::new(&self.text));
        ctx.focus(NOTE);
    }

    /// Write the editor's text to the note's file and show the new version.
    pub(super) fn save(&mut self, ctx: &mut Ctx) {
        let (Some(v), Some(rel), Some(ed)) = (&mut self.vault, &self.note, &mut self.editor) else { return };
        let text = ed.text();
        match v.write(rel, &text) {
            Ok(()) => {
                ed.dirty = false;
                self.text = text;
                self.rendered = None;
                let name = v.notes.get(rel).map_or(rel.clone(), |n| n.name.clone());
                ctx.toast("VAULT", &format!("✓ saved {name}"));
            }
            Err(e) => ctx.error("VAULT", &format!("couldn't save: {e}")),
        }
        let pal = ctx.pal;
        self.build_side(&pal);
    }

    /// Back to reading. Like Obsidian, typing is never lost: unsaved text is saved (unless :q!).
    pub(super) fn leave_editor(&mut self, save: bool, ctx: &mut Ctx) {
        if save && self.editor.as_ref().is_some_and(|e| e.dirty) {
            self.save(ctx);
        }
        self.editor = None;
        self.rendered = None;
    }

    /// Go where a list line leads: into a folder, to a note (and line), a tag's notes, a heading
    /// in this note. A link to a note that doesn't exist yet only says so (n makes one).
    pub(super) fn follow_route(&mut self, route: Route, ctx: &mut Ctx) {
        let pal = ctx.pal;
        match route {
            Route::Dir(d) => {
                self.explorer = Explorer::Files;
                self.folder = d;
                self.build_explorer(&pal);
            }
            Route::Tag(t) => {
                self.explorer = Explorer::Tag(t);
                self.build_explorer(&pal);
                ctx.focus(EXPLORER);
            }
            Route::Note(rel, line) => self.open_note(&rel, line, true, ctx),
            Route::Missing(t) => ctx.toast("VAULT", &format!("“{t}” isn't a note yet (n makes one)")),
            Route::Heading(line) => match &mut self.editor {
                Some(ed) => ed.goto_line(line),
                None => self.jump_to = Some(line),
            },
        }
    }

    /// backspace: the note you were on before.
    pub(super) fn back(&mut self, ctx: &mut Ctx) {
        if self.editor.is_some() {
            return;
        }
        if let Some(prev) = self.history.pop() {
            self.open_note(&prev, None, false, ctx);
        } else if self.explorer != Explorer::Files || !self.folder.is_empty() {
            self.explorer = Explorer::Files;
            self.folder = self.folder.rsplit_once('/').map_or(String::new(), |(f, _)| f.to_string());
            let pal = ctx.pal;
            self.build_explorer(&pal);
        }
    }

    /// The note's panel: the rendered note (Obsidian's extras as Markdown), or the editor.
    pub(super) fn render_note(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let v = self.vault.as_ref();
        let note = self.note.as_ref().and_then(|r| v?.notes.get(r));
        let title = note.map_or("NOTE".to_string(), |n| {
            format!("NOTE ░ {}", n.name.to_uppercase().chars().take(44).collect::<String>())
        });
        let block = ui::panel(pal, &title, view.panel_state(NOTE));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [meta, body, status] =
            Layout::vertical([Constraint::Length(2), Constraint::Fill(1), Constraint::Length(1)]).areas(inner);
        let Some(note) = note.cloned() else {
            let msg = if self.loading {
                "loading the vault…".to_string()
            } else if !self.message.is_empty() {
                self.message.clone()
            } else {
                let n = v.map_or(0, |v| v.notes.len());
                format!("{n} notes · o open · / search · # tags · D today's note")
            };
            frame.render_widget(Paragraph::new(Line::styled(msg, Style::new().fg(pal.beige_dark))), meta);
            return;
        };
        let mut meta_spans = vec![Span::styled(note.rel.clone(), Style::new().fg(pal.beige_dark))];
        if !note.tags.is_empty() {
            let mut tags = note.tags.clone();
            tags.sort();
            meta_spans.push(Span::styled(
                format!("   {}", tags.iter().map(|t| format!("#{t}")).collect::<Vec<_>>().join(" ")),
                Style::new().fg(pal.teal),
            ));
        }
        frame.render_widget(Paragraph::new(Line::from(meta_spans)), meta);

        if let Some(ed) = &mut self.editor {
            ed.render(frame, body, pal, view.inside(NOTE));
            let dirty = if ed.dirty {
                Span::styled("   ● unsaved", Style::new().fg(pal.orange))
            } else {
                Span::styled("   saved", Style::new().fg(pal.beige_dark))
            };
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled(ed.status(), bold(pal.amber_hi)),
                    dirty,
                    Span::styled("   ctrl+s save · esc esc back to reading", Style::new().fg(pal.amber_faint)),
                ])),
                status,
            );
            return;
        }
        let width = body.width.saturating_sub(1).max(10);
        if self.rendered.as_ref().is_none_or(|(w, _)| *w != width) {
            let md = obsidian_to_markdown(&self.text);
            self.rendered = Some((width, markdown::render(&md, width, pal, Style::new().fg(pal.amber))));
        }
        let lines = &self.rendered.as_ref().expect("just rendered").1;
        self.page = body.height as usize;
        if let Some(line) = self.jump_to.take() {
            // A file line: about that far through the rendered note.
            let total = self.text.lines().count().max(1);
            self.scroll = lines.len() * line / total;
        }
        self.scroll = self.scroll.min(lines.len().saturating_sub(body.height as usize));
        let shown: Vec<Line> = lines.iter().skip(self.scroll).take(body.height as usize).cloned().collect();
        frame.render_widget(Paragraph::new(shown), body);
        frame.render_widget(
            Paragraph::new(Line::styled(
                "read · e edit · f follow a link · M ask Mabel",
                Style::new().fg(pal.amber_faint),
            )),
            status,
        );
    }
}
