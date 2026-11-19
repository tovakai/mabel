//! COMMS ▸ COURSE: courses from the vault (courses.rs), with Mabel as the tutor.
//!
//! OUTLINE lists every course's lessons (✓ done). LESSON shows the one you're on: `c` runs its
//! check (passing marks it done), `d` marks it done by hand, `s` copies its starter files,
//! `a` asks Mabel about a failed check, `n`/`p` next/previous lesson. ASK is a chat with
//! Mabel for this lesson (its own session); her tutor material goes along with the questions.
//!
//! Everything but ASK works without her: lessons, checks and progress are files and commands.

use std::any::Any;
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent};
use futures::StreamExt;
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Wrap};

use crate::api::ChatEvent;
use crate::channel::{Channel, Ctx, Msg, Outcome, View};
use crate::courses::{self, Course, FULL_CONTEXT_EVERY, Progress};
use crate::keys;
use crate::palette::Palette;
use crate::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};

const ID: &str = "course";
const OUTLINE: usize = 0;
const LESSON: usize = 1;
const ASK: usize = 2;
const CHECK_LIMIT: Duration = Duration::from_secs(300);

/// A line in OUTLINE: a course or module heading, or a lesson (course index, lesson id).
enum Row {
    Course(String),
    Module(String),
    Lesson { course: usize, id: String, label: String },
}

struct Chat {
    who: Who,
    text: String,
}

#[derive(PartialEq)]
enum Who {
    You,
    Mabel,
    Note,
}

enum Got {
    Session { lesson: String, result: Result<String, String> },
    History { lesson: String, items: Vec<Chat> },
    Stream { turn: u32, event: ChatEvent },
    TurnDone { turn: u32 },
    Check { lesson: String, ok: bool, output: String },
}

pub struct CourseApp {
    vault: std::path::PathBuf,
    courses: Vec<Course>,
    progress: Vec<Progress>,
    rows: Vec<Row>,
    /// The highlighted row in OUTLINE.
    sel: usize,
    /// The open lesson: (course index, lesson id).
    open: Option<(usize, String)>,
    scroll: u16,
    /// Rendered lesson lines and the width they were made for.
    cache: Option<(String, u16, Vec<Line<'static>>)>,
    check: Option<(String, bool, String)>,
    checking: bool,
    chat: Vec<Chat>,
    prompt: Input,
    busy: bool,
    turn: u32,
    asked_at: Option<Instant>,
    /// A question waiting for its lesson's session to be made.
    queued: Option<String>,
    show_outline: bool,
    loaded: bool,
    pal: Palette,
}

impl CourseApp {
    pub fn new(cfg: &crate::config::Config) -> Self {
        CourseApp {
            vault: crate::config::expand(&cfg.vault),
            courses: vec![],
            progress: vec![],
            rows: vec![],
            sel: 0,
            open: None,
            scroll: 0,
            cache: None,
            check: None,
            checking: false,
            chat: vec![],
            prompt: Input::new("ask Mabel about this lesson…"),
            busy: false,
            turn: 0,
            asked_at: None,
            queued: None,
            show_outline: false,
            loaded: false,
            pal: Palette::from_config(&cfg.raw),
        }
    }

    fn load(&mut self) {
        self.courses = courses::load_all(&self.vault);
        self.progress = self.courses.iter().map(Progress::load).collect();
        self.rows.clear();
        for (ci, c) in self.courses.iter().enumerate() {
            self.rows.push(Row::Course(c.title.clone()));
            for (mi, m) in c.modules.iter().enumerate() {
                self.rows.push(Row::Module(format!("{}. {}", mi + 1, m.title)));
                for (li, l) in m.lessons.iter().enumerate() {
                    let label = format!("{}.{} {}", mi + 1, li + 1, l.title);
                    self.rows.push(Row::Lesson { course: ci, id: l.id.clone(), label });
                }
            }
        }
        self.cache = None;
        self.loaded = true;
        // Open where he left off: the first course's current lesson, else its first one.
        if self.open.is_none()
            && let Some(c) = self.courses.first()
        {
            let id = self.progress[0].current.clone().or_else(|| c.lessons().next().map(|l| l.id.clone()));
            if let Some(id) = id {
                self.sel = self.row_of(0, &id).unwrap_or(0);
                self.open = Some((0, id));
            }
        }
    }

    fn row_of(&self, course: usize, id: &str) -> Option<usize> {
        self.rows.iter().position(|r| matches!(r, Row::Lesson { course: c, id: i, .. } if *c == course && i == id))
    }

    fn lesson(&self) -> Option<(&Course, &courses::Lesson)> {
        let (ci, id) = self.open.as_ref()?;
        let c = self.courses.get(*ci)?;
        Some((c, c.lesson(id)?))
    }

    fn save_progress(&mut self, ci: usize, ctx: &mut Ctx) {
        if let (Some(c), Some(p)) = (self.courses.get(ci), self.progress.get(ci))
            && let Err(e) = p.save(c)
        {
            ctx.error("COURSE", &format!("couldn't save progress: {e}"));
        }
    }

    /// Open a lesson: its text, its chat (fetched from Mabel if there is one).
    fn open_lesson(&mut self, ci: usize, id: String, ctx: &mut Ctx) {
        self.open = Some((ci, id.clone()));
        self.scroll = 0;
        self.cache = None;
        self.check = None;
        self.chat.clear();
        self.queued = None;
        if let Some(p) = self.progress.get_mut(ci) {
            p.current = Some(id.clone());
        }
        self.save_progress(ci, ctx);
        if let Some(session) = self.progress.get(ci).and_then(|p| p.sessions.get(&id)).cloned() {
            ctx.spawn(ID, move |api| async move {
                let items = api
                    .history(&session)
                    .await
                    .map(|turns| {
                        turns
                            .iter()
                            .map(|t| {
                                let text = t["content"].as_str().unwrap_or("");
                                match t["role"].as_str() {
                                    Some("user") => {
                                        Chat { who: Who::You, text: courses::question_of(text).to_string() }
                                    }
                                    _ => Chat { who: Who::Mabel, text: text.to_string() },
                                }
                            })
                            .collect()
                    })
                    .unwrap_or_else(|_| {
                        vec![Chat {
                            who: Who::Note,
                            text: "(couldn't load this lesson's chat: Mabel isn't answering)".into(),
                        }]
                    });
                Got::History { lesson: id, items }
            });
        }
    }

    fn step_lesson(&mut self, forward: bool, ctx: &mut Ctx) {
        let Some((ci, id)) = self.open.clone() else { return };
        let ids: Vec<String> = self.courses[ci].lessons().map(|l| l.id.clone()).collect();
        let Some(i) = ids.iter().position(|x| *x == id) else { return };
        let next = if forward { ids.get(i + 1) } else { i.checked_sub(1).and_then(|j| ids.get(j)) };
        if let Some(next) = next.cloned() {
            self.sel = self.row_of(ci, &next).unwrap_or(self.sel);
            self.open_lesson(ci, next, ctx);
        }
    }

    fn mark(&mut self, done: Option<bool>, ctx: &mut Ctx) {
        let Some((ci, id)) = self.open.clone() else { return };
        let p = &mut self.progress[ci];
        let now = done.unwrap_or(!p.done.contains(&id));
        if now {
            p.done.insert(id);
        } else {
            p.done.remove(&id);
        }
        self.save_progress(ci, ctx);
        ctx.status(if now { "✓ lesson done" } else { "lesson not done" });
    }

    fn run_check(&mut self, ctx: &mut Ctx) {
        let Some((course, lesson)) = self.lesson() else { return };
        let Some(check) = lesson.check.clone() else {
            ctx.status("this lesson has no check: d marks it done");
            return;
        };
        if self.checking {
            return;
        }
        let dir = course.project_dir(lesson);
        let id = lesson.id.clone();
        self.checking = true;
        ctx.status(&format!("running the check: {check}"));
        ctx.run(ID, async move {
            if !dir.is_dir() {
                return Got::Check { lesson: id, ok: false, output: format!("The project folder doesn't exist yet:\n{}\n\nThe lesson's steps make it.", dir.display()) };
            }
            let run = tokio::process::Command::new("sh")
                .arg("-c")
                .arg(&check)
                .current_dir(&dir)
                .env("CARGO_TERM_COLOR", "never")
                .kill_on_drop(true)
                .output();
            let (ok, output) = match tokio::time::timeout(CHECK_LIMIT, run).await {
                Err(_) => (false, "The check took over 5 minutes and was stopped.".to_string()),
                Ok(Err(e)) => (false, format!("Couldn't run the check: {e}")),
                Ok(Ok(out)) => {
                    let text = String::from_utf8_lossy(&out.stdout).into_owned() + &String::from_utf8_lossy(&out.stderr);
                    let mut text: String = text.lines().rev().take(40).collect::<Vec<_>>().into_iter().rev().collect::<Vec<_>>().join("\n");
                    if out.status.code() == Some(127) {
                        text.push_str("\n\n(This device doesn't have the tool the check needs. Checks run where it's installed, e.g. the PC.)");
                    }
                    (out.status.success(), text)
                }
            };
            Got::Check { lesson: id, ok, output }
        });
    }

    fn ask(&mut self, question: String, ctx: &mut Ctx) {
        let Some((ci, id)) = self.open.clone() else {
            ctx.status("open a lesson first");
            return;
        };
        if self.busy {
            return;
        }
        let Some(session) = self.progress[ci].sessions.get(&id).cloned() else {
            // A chat of its own for this lesson, made on the first question.
            self.queued = Some(question);
            self.busy = true;
            let name = {
                let (c, l) = self.lesson().map(|(c, l)| (c.title.clone(), l.title.clone())).unwrap_or_default();
                format!("Course · {c} · {l}")
            };
            ctx.spawn(ID, move |api| async move {
                let result = api
                    .new_session(&name, None)
                    .await
                    .map_err(|e| format!("{e:#}"))
                    .and_then(|s| s["id"].as_str().map(String::from).ok_or_else(|| "no session id".to_string()));
                Got::Session { lesson: id, result }
            });
            return;
        };
        let asked = self.progress[ci].asked.entry(id.clone()).or_insert(0);
        let full = (*asked).is_multiple_of(FULL_CONTEXT_EVERY);
        *asked += 1;
        let Some((course, lesson)) = self.lesson() else { return };
        let message = courses::context(course, lesson, &question, full);
        self.save_progress(ci, ctx);
        self.chat.push(Chat { who: Who::You, text: question });
        self.chat.push(Chat { who: Who::Mabel, text: String::new() });
        self.busy = true;
        self.turn += 1;
        self.asked_at = Some(Instant::now());
        let (api, tx, turn) = (ctx.api.clone(), ctx.tx.clone(), self.turn);
        tokio::spawn(async move {
            let mut stream = api.stream(&session, &message, None).await;
            while let Some(event) = stream.next().await {
                if tx.send(Msg::For(ID, Box::new(Got::Stream { turn, event }))).is_err() {
                    return;
                }
            }
            let _ = tx.send(Msg::For(ID, Box::new(Got::TurnDone { turn })));
        });
    }

    fn reply(&mut self) -> Option<&mut String> {
        match self.chat.last_mut() {
            Some(Chat { who: Who::Mabel, text }) => Some(text),
            _ => None,
        }
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn render_outline(&self, frame: &mut Frame, area: Rect, view: &View) {
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

    fn render_lesson(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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
            let body = crate::widgets::markdown::render(&lesson.body, width, &pal, Style::new().fg(pal.amber));
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

    fn render_ask(&mut self, frame: &mut Frame, area: Rect, view: &View) {
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

impl Channel for CourseApp {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "COURSE"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        if !self.loaded {
            self.load();
        }
        self.pal = *view.pal;
        let outline_w = if view.narrow && !self.show_outline { 0 } else { 30.min(area.width / 3) };
        let [outline, rest] = Layout::horizontal([Constraint::Length(outline_w), Constraint::Fill(1)]).areas(area);
        let (lesson, ask) = if view.narrow {
            let [a, b] = Layout::vertical([Constraint::Percentage(58), Constraint::Fill(1)]).areas(rest);
            (a, b)
        } else {
            let [a, b] = Layout::horizontal([Constraint::Percentage(58), Constraint::Fill(1)]).areas(rest);
            (a, b)
        };
        if outline_w > 0 {
            self.render_outline(frame, outline, view);
        }
        self.render_lesson(frame, lesson, view);
        self.render_ask(frame, ask, view);
        vec![outline, lesson, ask]
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::ctrl(&key, 's') {
            self.show_outline = !self.show_outline;
            if self.show_outline {
                ctx.focus(OUTLINE);
            }
            return Outcome::Used;
        }
        match section {
            OUTLINE => match (keys::ch(&key), key.code) {
                (Some('j'), _) | (_, KeyCode::Down) => {
                    let next = (self.sel + 1..self.rows.len()).find(|&i| matches!(self.rows[i], Row::Lesson { .. }));
                    self.sel = next.unwrap_or(self.sel);
                }
                (Some('k'), _) | (_, KeyCode::Up) => {
                    let prev = (0..self.sel).rev().find(|&i| matches!(self.rows[i], Row::Lesson { .. }));
                    self.sel = prev.unwrap_or(self.sel);
                }
                (_, KeyCode::Enter) | (Some('l'), _) => {
                    if let Some(Row::Lesson { course, id, .. }) = self.rows.get(self.sel) {
                        let (c, i) = (*course, id.clone());
                        self.open_lesson(c, i, ctx);
                        ctx.focus(LESSON);
                    }
                }
                (Some('r'), _) => {
                    self.loaded = false;
                    ctx.status("courses read again");
                }
                _ => return Outcome::Ignored,
            },
            LESSON => match (keys::ch(&key), key.code) {
                (Some('j'), _) | (_, KeyCode::Down) => self.scroll = self.scroll.saturating_add(1),
                (Some('k'), _) | (_, KeyCode::Up) => self.scroll = self.scroll.saturating_sub(1),
                (_, KeyCode::PageDown) | (Some(' '), _) => self.scroll = self.scroll.saturating_add(15),
                (_, KeyCode::PageUp) => self.scroll = self.scroll.saturating_sub(15),
                (Some('g'), _) => self.scroll = 0,
                (Some('G'), _) => self.scroll = u16::MAX,
                (Some('c'), _) => self.run_check(ctx),
                (Some('d'), _) => self.mark(None, ctx),
                (Some('n'), _) => self.step_lesson(true, ctx),
                (Some('p'), _) => self.step_lesson(false, ctx),
                (Some('s'), _) => {
                    if let Some((c, l)) = self.lesson() {
                        match courses::start_files(c, l) {
                            Ok(0) => ctx.status("no starter files to copy (or they're all there already)"),
                            Ok(n) => ctx.toast(
                                "COURSE",
                                &format!("{n} starter files copied into {}", c.project_dir(l).display()),
                            ),
                            Err(e) => ctx.error("COURSE", &format!("couldn't copy the starter files: {e}")),
                        }
                    }
                }
                (Some('a'), _) => {
                    if let Some((_, false, out)) = self.check.clone() {
                        self.ask(format!("My check failed. This is what it said:\n\n```\n{out}\n```"), ctx);
                        ctx.focus(ASK);
                    }
                }
                (Some('i'), _) => ctx.focus(ASK),
                _ => return Outcome::Ignored,
            },
            _ => {
                if self.prompt.key(&key) == InputResult::Submit {
                    let q = self.prompt.value.trim().to_string();
                    if !q.is_empty() && !self.busy {
                        self.prompt.clear();
                        self.ask(q, ctx);
                    }
                }
                return if matches!(key.code, KeyCode::Esc) { Outcome::Ignored } else { Outcome::Used };
            }
        }
        Outcome::Used
    }

    fn paste(&mut self, section: usize, text: &str, _ctx: &mut Ctx) {
        if section == ASK {
            for c in text.chars().filter(|c| *c != '\n') {
                self.prompt.key(&KeyEvent::new(KeyCode::Char(c), crossterm::event::KeyModifiers::NONE));
            }
        }
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Session { lesson, result } => {
                self.busy = false;
                let question = self.queued.take();
                match result {
                    Ok(session) => {
                        if let Some(ci) = self.open.as_ref().map(|(c, _)| *c) {
                            self.progress[ci].sessions.insert(lesson, session);
                            self.save_progress(ci, ctx);
                        }
                        if let Some(q) = question {
                            self.ask(q, ctx);
                        }
                    }
                    Err(_) => self.chat.push(Chat {
                        who: Who::Note,
                        text: "Mabel isn't answering right now (she may be resting). The lesson, the check and your progress all still work.".into(),
                    }),
                }
            }
            Got::History { lesson, items } => {
                if self.open.as_ref().is_some_and(|(_, id)| *id == lesson) && self.chat.is_empty() {
                    self.chat = items;
                }
            }
            Got::Stream { turn, event } if turn == self.turn => match event {
                ChatEvent::Delta(d) => {
                    if let Some(t) = self.reply() {
                        t.push_str(&d);
                    }
                }
                ChatEvent::Event { kind, body } => match kind.as_str() {
                    "final_response" => {
                        let content = body.get("content").and_then(|c| c.as_str()).unwrap_or("").to_string();
                        if let Some(t) = self.reply()
                            && t.trim().is_empty()
                        {
                            *t = content;
                        }
                    }
                    "error" => {
                        if let Some(t) = self.reply()
                            && t.trim().is_empty()
                        {
                            *t = "*(Mabel isn't answering right now: she may be resting. The lesson, the check and your progress all still work.)*".into();
                        }
                    }
                    _ => {}
                },
            },
            Got::Stream { .. } => {}
            Got::TurnDone { turn } if turn == self.turn => {
                self.busy = false;
                if let Some(t) = self.reply()
                    && t.trim().is_empty()
                {
                    *t = "*(no answer came back)*".into();
                }
            }
            Got::TurnDone { .. } => {}
            Got::Check { lesson, ok, output } => {
                self.checking = false;
                if ok && let Some(ci) = self.open.as_ref().filter(|(_, id)| *id == lesson).map(|(c, _)| *c) {
                    self.progress[ci].done.insert(lesson.clone());
                    self.save_progress(ci, ctx);
                    ctx.toast("COURSE", "✓ check passed: lesson done");
                }
                ctx.status("");
                self.check = Some((lesson, ok, output));
            }
        }
    }

    fn next_wake(&self) -> Option<Instant> {
        (self.busy || self.checking).then(|| Instant::now() + Duration::from_millis(500))
    }
}
