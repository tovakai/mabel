//! Talking with Mabel: her chat list, opening and starting chats, sending a message, the
//! streamed reply (with tool lines), and approving or denying an action she's holding.

use super::*;

impl Comms {
    // ── chats ────────────────────────────────────────────────────────────

    /// Ask for her chat list (in the background).
    pub(super) fn load_sessions(&self, ctx: &Ctx, select_first: bool) {
        ctx.spawn(ID, move |api| async move {
            Got::Sessions { result: api.sessions().await.map_err(|e| e.to_string()), select_first }
        });
    }

    /// Open a chat: its title now, its messages when they arrive.
    pub(super) fn open_session(&mut self, session: Value, ctx: &mut Ctx) {
        self.generation += 1;
        let generation = self.generation;
        let name = session["name"].as_str().filter(|n| !n.is_empty()).unwrap_or("untitled");
        self.title = format!("COMMS ░ {}", name.to_uppercase().chars().take(40).collect::<String>());
        self.items.clear();
        self.scroll_up = 0;
        let id = session["id"].as_str().unwrap_or("").to_string();
        self.session = Some(session);
        ctx.spawn(ID, move |api| async move {
            Got::History { generation, result: api.history(&id).await.map_err(|e| e.to_string()) }
        });
    }

    /// ctrl+n: a new chat named after now, with the open chat's model settings (or the newest
    /// chat's, if none is open).
    pub(super) fn new_session(&self, ctx: &Ctx) {
        let like = self.session.clone();
        let name = format!("Terminal {}", Local::now().format("%d %b %H:%M"));
        ctx.spawn(ID, move |api| async move {
            Got::Created(api.new_session(&name, like.as_ref()).await.map_err(|e| e.to_string()))
        });
    }

    // ── talking ──────────────────────────────────────────────────────────

    /// enter: send the prompt (with VAULT's note attached, if one is waiting).
    pub(super) fn send(&mut self, ctx: &mut Ctx) {
        let text = self.prompt.value.trim().to_string();
        if text.is_empty() || self.busy {
            return;
        }
        if self.session.is_none() {
            ctx.toast("COMMS", "No chat open: ctrl+n starts one");
            return;
        }
        self.prompt.clear();
        if let Some((name, rel, content)) = self.pending_note.take() {
            self.prompt.placeholder = PLACEHOLDER.into();
            let body: String = content.chars().take(8000).collect();
            let full = format!("About my note “{name}” ({rel}):\n\n{body}\n\n---\n{text}");
            self.run_turn(&full, None, Some(format!("[about “{name}”] {text}")), ctx);
            return;
        }
        self.run_turn(&text, None, None, ctx);
    }

    /// Send `text` (or, with `approval`, answer a held action) and stream her reply into a new
    /// message. `shown` is what the conversation shows for it, when that differs from `text`.
    pub(super) fn run_turn(
        &mut self,
        text: &str,
        approval: Option<(String, String)>,
        shown: Option<String>,
        ctx: &mut Ctx,
    ) {
        let Some(session_id) = self.session.as_ref().and_then(|s| s["id"].as_str()).map(String::from) else { return };
        self.busy = true;
        self.turn += 1;
        ctx.status("◉ MABEL IS TYPING");
        if !text.is_empty() {
            self.items.push(Item::Msg {
                role: Role::You,
                text: shown.unwrap_or_else(|| text.into()),
                when: stamp(None),
                cache: None,
            });
        }
        self.items.push(Item::Msg { role: Role::Mabel, text: String::new(), when: stamp(None), cache: None });
        self.reply = Some(self.items.len() - 1);
        self.got_text = false;
        self.scroll_up = 0;

        let (api, tx, turn, text) = (ctx.api.clone(), ctx.tx.clone(), self.turn, text.to_string());
        tokio::spawn(async move {
            let approval = approval.as_ref().map(|(id, d)| (id.as_str(), d.as_str()));
            let mut stream = api.stream(&session_id, &text, approval).await;
            while let Some(event) = stream.next().await {
                if tx.send(Msg::For(ID, Box::new(Got::Stream { turn, event }))).is_err() {
                    return;
                }
            }
            let _ = tx.send(Msg::For(ID, Box::new(Got::TurnDone { turn })));
        });
    }

    /// The text of the reply being streamed (and drop its cached lines, so it's redrawn).
    pub(super) fn reply_text(&mut self) -> Option<&mut String> {
        match self.reply.and_then(|i| self.items.get_mut(i)) {
            Some(Item::Msg { text, cache, .. }) => {
                *cache = None;
                Some(text)
            }
            _ => None,
        }
    }

    /// A piece of the stream: reply text (her face talks), the whole reply at once, a tool
    /// line, an approval card, an error, or a control message (music commands).
    pub(super) fn on_stream(&mut self, event: ChatEvent, ctx: &mut Ctx) {
        match event {
            ChatEvent::Delta(delta) => {
                self.got_text = true;
                if !delta.trim().is_empty() {
                    self.talk(TALK_HOLD);
                }
                if let Some(text) = self.reply_text() {
                    text.push_str(&delta);
                }
            }
            ChatEvent::Event { kind, body } => match kind.as_str() {
                "final_response" => {
                    let content = body.get("content").and_then(|c| c.as_str()).unwrap_or("").to_string();
                    if !self.got_text && !content.trim().is_empty() {
                        // It all arrived at once: say it now, about as long as it takes to read.
                        let secs = (content.chars().count() as f64 / READ_SPEED).clamp(1.5, 8.0);
                        self.talk(Duration::from_secs_f64(secs));
                    }
                    if let Some(text) = self.reply_text() {
                        *text = content;
                    }
                    self.got_text = true;
                }
                "tool_start" => {
                    let name = body.get("tool").or(body.get("name")).and_then(|t| t.as_str()).unwrap_or("tool");
                    let at = self.reply.unwrap_or(self.items.len());
                    self.items.insert(at, Item::Tool(name.into()));
                    self.reply = self.reply.map(|i| i + 1);
                }
                "tool_output" => {
                    let card =
                        body.get("ask_user").filter(|a| a["approval_id"].as_str().is_some_and(|i| !i.is_empty()));
                    if let Some(card) = card {
                        self.items.push(Item::Approval(card.clone()));
                        self.scroll_up = 0;
                        ctx.focus(LOG); // y / n answer it
                    }
                }
                "ui_control" => {
                    if let Some(command) =
                        body.get("data").and_then(|d| d.get("music_command")).filter(|c| c.is_object())
                    {
                        ctx.player.apply(command);
                        ctx.toast("♪", "Mabel put something on");
                    }
                }
                "error" => {
                    let e =
                        body.get("error").map(|e| e.as_str().map_or(e.to_string(), String::from)).unwrap_or_default();
                    self.items.push(Item::Error(e));
                }
                _ => {}
            },
        }
    }

    /// The reply ended: drop an empty reply bubble (a turn that only asked for approval).
    pub(super) fn turn_done(&mut self, ctx: &mut Ctx) {
        if !self.got_text
            && let Some(i) = self.reply
            && matches!(self.items.get(i), Some(Item::Msg { role: Role::Mabel, text, .. }) if text.is_empty())
        {
            self.items.remove(i);
        }
        self.reply = None;
        self.busy = false;
        ctx.status("");
    }

    /// Answer the newest approval card.
    pub(super) fn decide(&mut self, decision: &'static str, ctx: &mut Ctx) -> bool {
        if self.busy {
            return false;
        }
        let Some(i) = self.items.iter().rposition(|it| matches!(it, Item::Approval(_))) else { return false };
        let Item::Approval(card) = self.items.remove(i) else { return false };
        let id = card["approval_id"].as_str().unwrap_or("").to_string();
        self.run_turn("", Some((id, decision.into())), None, ctx);
        ctx.focus(PROMPT);
        true
    }

    /// Answer a specific approval card (a click on its button).
    pub(super) fn decide_id(&mut self, id: &str, decision: &'static str, ctx: &mut Ctx) {
        if self.busy {
            return;
        }
        if let Some(i) = self.items.iter().position(|it| matches!(it, Item::Approval(c) if c["approval_id"] == id)) {
            self.items.remove(i);
            self.run_turn("", Some((id.into(), decision.into())), None, ctx);
            ctx.focus(PROMPT);
        }
    }
}
