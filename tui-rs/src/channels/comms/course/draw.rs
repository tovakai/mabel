//! Drawing COURSE: OUTLINE, the lesson (with its check result), and ASK.

use super::*;

impl CourseApp {
    // ── drawing ──────────────────────────────────────────────────────────

    /// OUTLINE: every course's modules and lessons (✓ done, ▸ open).
    pub(super) fn render_outline(&self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let block = ui::panel(pal, "OUTLINE", view.panel_state(OUTLINE));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let rows = inner.height as usize;
        let top = self.sel.saturating_sub(rows.saturating_sub(2));
        let lines: Vec<Line> = self
            .rows
            .iter()
            .enumerate()
            .skip(top)
            .take(rows)
            .map(|(i, r)| match r {
                Row::Course(t) => Line::styled(format!(" {t}"), bold(pal.beige)),
                Row::Module(t) => Line::styled(format!(" {t}"), Style::new().fg(pal.amber_dim)),
                Row::Lesson { course, id, label } => {
                    let done = self.progress.get(*course).is_some_and(|p| p.done.contains(id));
                    let open = self.open.as_ref().is_some_and(|(c, o)| c == course && o == id);
                    let mark = if done {
                        "✓"
                    } else if open {
                        "▸"
                    } else {
                        " "
                    };
                    let style = if i == self.sel && view.inside(OUTLINE) {
                        bold(pal.bg).bg(pal.amber)
                    } else if open {
                        bold(pal.amber_hi)
                    } else if done {
                        Style::new().fg(pal.teal)
                    } else {
                        Style::new().fg(pal.amber)
                    };
                    Line::styled(format!("  {mark} {label}"), style)
                }
            })
            .collect();
        frame.render_widget(Paragraph::new(lines), inner);
    }

    /// LESSON: title, goal, how long and where, the lesson as Markdown (with the module's and
    /// course's introduction on a first lesson), and the check's result at the bottom.
    pub(super) fn render_lesson(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = *view.pal;
        let block = ui::panel(&pal, "LESSON", view.panel_state(LESSON));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let Some((course, lesson)) = self.lesson() else {
            let text = if self.courses.is_empty() {
                format!(
                    " No courses yet.\n\n Courses live in {}/Courses/<course>/.\n Courses/COURSE_FORMAT.md says how one is laid out.",
                    self.vault.display()
                )
            } else {
                " Pick a lesson in OUTLINE.".into()
            };
            frame.render_widget(Paragraph::new(text).style(Style::new().fg(pal.amber)), inner);
            return;
        };
        let (ci, id) = self.open.clone().unwrap_or_default();
        let done = self.progress.get(ci).is_some_and(|p| p.done.contains(&id));
        let mut head = vec![Line::styled(
            format!(" {}{}", lesson.title, if done { "  ✓" } else { "" }),
            bold(if done { pal.teal } else { pal.beige }),
        )];
        if !lesson.goal.is_empty() {
            head.push(Line::styled(format!(" {}", lesson.goal), Style::new().fg(pal.amber)));
        }
        let mut meta = vec![];
        if let Some(m) = lesson.minutes {
            meta.push(format!("~{m} min"));
        }
        meta.push(format!("in {}", course.project_dir(lesson).display()));
        if let Some(c) = &lesson.check {
            meta.push(format!("c: check ({c})"));
        } else {
            meta.push("d: mark done".into());
        }
        head.push(Line::styled(format!(" {}", meta.join(" · ")), Style::new().fg(pal.amber_dim)));
        head.push(Line::raw(""));

        let width = inner.width.saturating_sub(2);
        let key = lesson.id.clone();
        if self.cache.as_ref().is_none_or(|(k, w, _)| *k != key || *w != width) {
            // A module's first lesson starts with the module's introduction; the course's
            // first lesson with the course card too.
            let mut text = String::new();
            let first_of_course = course.lessons().next().is_some_and(|l| l.id == lesson.id);
            if first_of_course && !course.intro.is_empty() {
                let by = if course.author.is_empty() { String::new() } else { format!("\n\n*by {}*", course.author) };
                text.push_str(&format!(
                    "> **{}**: {}{by}\n\n{}\n\n---\n\n",
                    course.title, course.description, course.intro
                ));
            }
            if let Some(m) = course.modules.iter().find(|m| m.lessons.first().is_some_and(|l| l.id == lesson.id))
                && !m.intro.is_empty()
            {
                text.push_str(&format!("**Module: {}.** {}\n\n---\n\n", m.title, m.intro));
            }
            text.push_str(&lesson.body);
            let body = crate::widgets::markdown::render(&text, width, &pal, Style::new().fg(pal.amber));
            self.cache = Some((key, width, body));
        }
        let check_lines: Vec<Line> = match (&self.check, self.checking) {
            (_, true) => vec![Line::styled(" ◌ running the check…", Style::new().fg(pal.mint))],
            (Some((cid, ok, out)), false) if *cid == id => {
                let mut l = vec![Line::styled(
                    if *ok {
                        " ✓ CHECK PASSED".to_string()
                    } else {
                        " ✗ CHECK FAILED  (a: ask Mabel about it)".to_string()
                    },
                    bold(if *ok { pal.teal } else { pal.red }),
                )];
                l.extend(
                    out.lines()
                        .rev()
                        .take(12)
                        .collect::<Vec<_>>()
                        .into_iter()
                        .rev()
                        .map(|s| Line::styled(format!(" {s}"), Style::new().fg(pal.amber_dim))),
                );
                l
            }
            _ => vec![],
        };
        let check_h = (check_lines.len() as u16).min(inner.height / 2);
        let [text_area, check_area] = Layout::vertical([Constraint::Fill(1), Constraint::Length(check_h)]).areas(inner);
        let mut lines = head;
        lines.extend(self.cache.as_ref().map(|(_, _, l)| l.clone()).unwrap_or_default().into_iter().map(|mut l| {
            l.spans.insert(0, Span::raw(" "));
            l
        }));
        let max = (lines.len() as u16).saturating_sub(text_area.height.max(1));
        self.scroll = self.scroll.min(max);
        frame.render_widget(Paragraph::new(lines).scroll((self.scroll, 0)), text_area);
        if check_h > 0 {
            frame.render_widget(Paragraph::new(check_lines).wrap(Wrap { trim: false }), check_area);
        }
    }

    /// ASK: the chat, and the question line.
    pub(super) fn render_ask(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = *view.pal;
        let block = ui::panel(&pal, "ASK MABEL", view.panel_state(ASK));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [log, input] = Layout::vertical([Constraint::Fill(1), Constraint::Length(1)]).areas(inner);
        let width = log.width.saturating_sub(1);
        let mut lines: Vec<Line> = vec![];
        if self.chat.is_empty() {
            lines.push(Line::styled(
                " Stuck? Ask here. She has this lesson's tutor manual",
                Style::new().fg(pal.amber_dim),
            ));
            lines.push(Line::styled(
                " and sees your code. Hints first; say \"just show me\"",
                Style::new().fg(pal.amber_dim),
            ));
            lines.push(Line::styled(" if you want the answer.", Style::new().fg(pal.amber_dim)));
        }
        for c in &self.chat {
            match c.who {
                Who::You => {
                    lines.push(Line::styled(" YOU", bold(pal.teal)));
                    lines.extend(crate::widgets::markdown::render(&c.text, width, &pal, Style::new().fg(pal.beige)));
                }
                Who::Mabel => {
                    lines.push(Line::styled(" MABEL", bold(pal.amber_hi)));
                    let text = if c.text.is_empty() && self.busy {
                        let secs = self.asked_at.map_or(0, |t| t.elapsed().as_secs());
                        format!("◌ thinking… {secs}s")
                    } else {
                        c.text.clone()
                    };
                    lines.extend(crate::widgets::markdown::render(&text, width, &pal, Style::new().fg(pal.amber)));
                }
                Who::Note => lines.push(Line::styled(format!(" {}", c.text), Style::new().fg(pal.amber_dim))),
            }
            lines.push(Line::raw(""));
        }
        let skip = (lines.len() as u16).saturating_sub(log.height);
        frame.render_widget(Paragraph::new(lines).scroll((skip, 0)), log);
        self.prompt.render(frame, input, Style::new().fg(pal.beige), Style::new().fg(pal.amber_dim), view.inside(ASK));
    }
}
