//! The lesson side of COURSE: opening a lesson (and its chat), moving between lessons, marking
//! one done, and running its check.

use super::*;

impl CourseApp {
    /// Open a lesson: its text, its chat (fetched from Mabel if there is one).
    /// Open a lesson: its text, and its earlier chat with Mabel (if it has a session).
    pub(super) fn open_lesson(&mut self, ci: usize, id: String, ctx: &mut Ctx) {
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

    /// n / p: the next or previous lesson in the course.
    pub(super) fn step_lesson(&mut self, forward: bool, ctx: &mut Ctx) {
        let Some((ci, id)) = self.open.clone() else { return };
        let ids: Vec<String> = self.courses[ci].lessons().map(|l| l.id.clone()).collect();
        let Some(i) = ids.iter().position(|x| *x == id) else { return };
        let next = if forward { ids.get(i + 1) } else { i.checked_sub(1).and_then(|j| ids.get(j)) };
        if let Some(next) = next.cloned() {
            self.sel = self.row_of(ci, &next).unwrap_or(self.sel);
            self.open_lesson(ci, next, ctx);
        }
    }

    /// d: mark the lesson done or not (`done`: set it; None: flip it).
    pub(super) fn mark(&mut self, done: Option<bool>, ctx: &mut Ctx) {
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

    /// c: run the lesson's check in its project folder, in the background (5 minutes at most).
    pub(super) fn run_check(&mut self, ctx: &mut Ctx) {
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
}
