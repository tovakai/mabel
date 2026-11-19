//! ASK: questions to Mabel about the lesson, with the tutor's material attached (see
//! `courses::context`), one chat session per lesson.

use super::*;

impl CourseApp {
    /// Send a question about the open lesson. The first one makes the lesson's chat session;
    /// the tutor's material goes with question 1 and every 8th, his files with every one.
    pub(super) fn ask(&mut self, question: String, ctx: &mut Ctx) {
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

    /// Her answer being streamed (the last chat line, if it's hers).
    pub(super) fn reply(&mut self) -> Option<&mut String> {
        match self.chat.last_mut() {
            Some(Chat { who: Who::Mabel, text }) => Some(text),
            _ => None,
        }
    }
}
