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

use crate::channels::comms::courses::{self, Course, FULL_CONTEXT_EVERY, Progress};
use crate::os::api::ChatEvent;
use crate::os::channel::{Channel, Ctx, Msg, Outcome, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};

// COURSE is split by job; the pieces add to `impl CourseApp`.
/// asking Mabel
mod ask;
/// drawing
mod draw;
/// lessons: open, move, mark, check
mod lesson;

/// The id messages and help use.
const ID: &str = "course";
// The three sections, in the order `render` returns them.
const OUTLINE: usize = 0;
const LESSON: usize = 1;
const ASK: usize = 2;
/// A lesson's check is stopped after this long.
const CHECK_LIMIT: Duration = Duration::from_secs(300);

/// A line in OUTLINE: a course or module heading, or a lesson (course index, lesson id).
enum Row {
    Course(String),
    Module(String),
    Lesson { course: usize, id: String, label: String },
}

/// One line of the ASK chat.
struct Chat {
    who: Who,
    text: String,
}

/// Who a chat line is from (Note: COURSE itself, e.g. "she's resting").
#[derive(PartialEq)]
enum Who {
    You,
    Mabel,
    Note,
}

/// Background work reporting back.
enum Got {
    /// A lesson's chat session was made (or couldn't be: she's down).
    Session {
        lesson: String,
        result: Result<String, String>,
    },
    /// A lesson's earlier chat, fetched when it's opened.
    History {
        lesson: String,
        items: Vec<Chat>,
    },
    /// Her answer arriving, and its end (old turns are ignored).
    Stream {
        turn: u32,
        event: ChatEvent,
    },
    TurnDone {
        turn: u32,
    },
    /// A check finished: passed?, and the end of its output.
    Check {
        lesson: String,
        ok: bool,
        output: String,
    },
}

/// The COMMS ▸ COURSE screen.
pub struct CourseApp {
    /// The vault, whose Courses/ folder holds the courses.
    vault: std::path::PathBuf,
    /// Every course, its progress (same order), and OUTLINE's lines.
    courses: Vec<Course>,
    progress: Vec<Progress>,
    rows: Vec<Row>,
    /// The highlighted row in OUTLINE.
    sel: usize,
    /// The open lesson: (course index, lesson id).
    open: Option<(usize, String)>,
    /// How far the lesson is scrolled.
    scroll: u16,
    /// Rendered lesson lines and the width they were made for.
    cache: Option<(String, u16, Vec<Line<'static>>)>,
    /// The last check's result: (lesson id, passed?, output), and whether one is running.
    check: Option<(String, bool, String)>,
    checking: bool,
    /// ASK: the chat for the open lesson, and the question being typed.
    chat: Vec<Chat>,
    prompt: Input,
    /// Waiting for her answer (one question at a time).
    busy: bool,
    /// Counts questions, so a late piece of an old answer can't land in a new one.
    turn: u32,
    /// When the question went (the "thinking… 12s").
    asked_at: Option<Instant>,
    /// A question waiting for its lesson's session to be made.
    queued: Option<String>,
    /// ctrl+s: OUTLINE shown on the deck (it's hidden there by default).
    show_outline: bool,
    /// The courses have been read (on first draw; r in OUTLINE reads them again).
    loaded: bool,
    pal: Palette,
}

impl CourseApp {
    /// An empty COURSE; it reads the courses on first draw.
    pub fn new(cfg: &crate::os::config::Config) -> Self {
        CourseApp {
            vault: crate::os::config::expand(&cfg.vault),
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

    /// Read every course and its progress, build OUTLINE, and open where he left off.
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

    /// Which OUTLINE line a lesson is on.
    fn row_of(&self, course: usize, id: &str) -> Option<usize> {
        self.rows.iter().position(|r| matches!(r, Row::Lesson { course: c, id: i, .. } if *c == course && i == id))
    }

    /// The open lesson, and its course.
    fn lesson(&self) -> Option<(&Course, &courses::Lesson)> {
        let (ci, id) = self.open.as_ref()?;
        let c = self.courses.get(*ci)?;
        Some((c, c.lesson(id)?))
    }

    /// Write course `ci`'s progress.md (a red toast if it can't).
    fn save_progress(&mut self, ci: usize, ctx: &mut Ctx) {
        if let (Some(c), Some(p)) = (self.courses.get(ci), self.progress.get(ci))
            && let Err(e) = p.save(c)
        {
            ctx.error("COURSE", &format!("couldn't save progress: {e}"));
        }
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
