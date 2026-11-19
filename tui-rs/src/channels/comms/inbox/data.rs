//! INBOX's data: loading chats and messages through Mabel's Beeper relay (and polling them),
//! sending, opening a chat, and the compose line (new message, reply, edit).

use super::*;

impl Inbox {
    // ── data ─────────────────────────────────────────────────────────────

    /// Fetch the chat list (with the filter and unread-only), and the first time also which
    /// network each account is. `quiet`: a poll, so no "loading" and errors stay small.
    pub(super) fn load_chats(&mut self, ctx: &Ctx, quiet: bool) {
        self.generation += 1;
        self.last_chats = Instant::now();
        let generation = self.generation;
        let known = (!self.accounts.is_empty()).then(|| self.accounts.clone());
        let (filter, unread, network) = (self.filter.clone(), self.unread_only, self.network.clone());
        ctx.spawn(ID, move |api| async move {
            let mut fetched = None;
            let accounts = match known {
                Some(a) => a,
                None => match api.beeper_status().await {
                    Err(e) => return Got::Chats { generation, accounts: None, result: Err(e.to_string()), quiet },
                    Ok(st) if !st["token"].as_bool().unwrap_or(false) => {
                        let e = "set-token: Beeper isn't connected".to_string();
                        return Got::Chats { generation, accounts: None, result: Err(e), quiet };
                    }
                    Ok(st) => {
                        let a: HashMap<String, String> = st["accounts"]
                            .as_array()
                            .into_iter()
                            .flatten()
                            .map(|a| {
                                let id = s(a, "accountID").to_string();
                                let net = match s(a, "network") {
                                    "" => id.clone(),
                                    n => n.to_string(),
                                };
                                (id, net)
                            })
                            .collect();
                        fetched = Some(a.clone());
                        a
                    }
                },
            };
            let mut params: Vec<(&str, String)> = vec![("limit", "100".into())];
            if !filter.is_empty() {
                params.push(("query", filter.clone()));
            }
            if unread {
                params.push(("unreadOnly", "true".into()));
            }
            if let Some(net) = &network {
                for (id, n) in &accounts {
                    if n == net {
                        params.push(("accountIDs", id.clone()));
                    }
                }
            }
            let path = if !filter.is_empty() || unread { "v1/chats/search" } else { "v1/chats" };
            let borrowed: Vec<(&str, &str)> = params.iter().map(|(k, v)| (*k, v.as_str())).collect();
            let result = api.beeper(Method::GET, path, &borrowed, None).await.map(items).map(|chats| {
                chats
                    .into_iter()
                    .filter(|c| match (&network, path) {
                        (Some(net), "v1/chats") => accounts.get(s(c, "accountID")) == Some(net),
                        _ => true,
                    })
                    .collect()
            });
            Got::Chats { generation, accounts: fetched, result: result.map_err(|e| e.to_string()), quiet }
        });
    }

    /// Fetch the open chat's newest messages.
    pub(super) fn load_messages(&mut self, ctx: &Ctx, quiet: bool) {
        self.last_open = Instant::now();
        let Some(chat) = self.open.as_ref().map(|c| s(c, "id").to_string()) else { return };
        ctx.spawn(ID, move |api| async move {
            let result = api.beeper(Method::GET, &format!("v1/chats/{chat}/messages"), &[], None).await;
            let result = result.map(items).map_err(|e| e.to_string()).map(|msgs| {
                let mut msgs: Vec<Value> =
                    msgs.into_iter().filter(|m| !m["isHidden"].as_bool().unwrap_or(false)).collect();
                let key = |m: &Value| match s(m, "sortKey") {
                    "" => s(m, "timestamp").to_string(),
                    k => k.to_string(),
                };
                msgs.sort_by_key(key);
                msgs
            });
            Got::Messages { chat, result, quiet }
        });
    }

    /// Send something to Beeper (a message, edit, reaction, read mark) in the background;
    /// `reload`: fetch the chat again afterwards.
    pub(super) fn post(&self, ctx: &Ctx, method: Method, path: String, body: Value, reload: bool) {
        ctx.spawn(ID, move |api| async move {
            let error = api.beeper(method, &path, &[], Some(&body)).await.err().map(|e| format!("✖ {e}"));
            Got::Posted { reload, error }
        });
    }

    /// Open chat `index` from the list (and mark it read).
    pub(super) fn open_chat(&mut self, index: usize, ctx: &mut Ctx) {
        let Some(chat) = self.chats.get_mut(index) else { return };
        let id = s(chat, "id").to_string();
        let unread = chat["unreadCount"].as_u64().unwrap_or(0) > 0;
        if unread {
            chat["unreadCount"] = json!(0);
        }
        self.open = Some(chat.clone());
        self.messages.clear();
        self.msg_index = None;
        self.reply_to = None;
        self.editing = None;
        self.load_messages(ctx, false);
        if unread {
            self.post(ctx, Method::POST, format!("v1/chats/{id}/read"), json!({}), false);
        }
        ctx.focus(CONV);
    }

    /// The message picked in the conversation (the newest if none is picked).
    pub(super) fn selected(&self) -> Option<&Value> {
        match self.msg_index {
            Some(i) => self.messages.get(i),
            None => self.messages.last(),
        }
    }

    // ── compose ──────────────────────────────────────────────────────────

    /// Put the keys in the compose line, starting with `value`.
    pub(super) fn start_compose(&mut self, value: &str, ctx: &mut Ctx) {
        let Some(open) = &self.open else { return };
        if open["isReadOnly"].as_bool().unwrap_or(false) {
            self.message = "you can't write in this chat".into();
            return;
        }
        self.compose.placeholder = format!("message {} · enter sends · esc", s(open, "title"));
        self.compose.set(value);
        self.mode = if self.editing.is_some() { Mode::Edit } else { Mode::Send };
        ctx.focus(COMPOSE);
    }

    /// Leave the compose line (and forget the reply/edit it was for).
    pub(super) fn stop_compose(&mut self, ctx: &mut Ctx) {
        self.reply_to = None;
        self.editing = None;
        self.compose.clear();
        self.compose.placeholder = IDLE_PLACEHOLDER.into();
        self.mode = Mode::Idle;
        ctx.focus(if self.open.is_some() { CONV } else { CHATS });
    }

    /// enter in the compose line: apply a filter, send a reaction (a number picks one of
    /// `REACTIONS`), save an edit, or send a message (as a reply, if one was picked).
    pub(super) fn submit(&mut self, ctx: &mut Ctx) {
        let text = self.compose.value.trim().to_string();
        match self.mode {
            Mode::Filter => {
                self.filter = text;
                self.index = 0;
                self.load_chats(ctx, false);
                self.stop_compose(ctx);
                ctx.focus(CHATS);
            }
            Mode::React => {
                let key = match text.parse::<usize>() {
                    Ok(n) if (1..=REACTIONS.len()).contains(&n) => REACTIONS[n - 1].to_string(),
                    _ => text,
                };
                if let (false, Some(m), Some(open)) = (key.is_empty(), self.selected(), &self.open) {
                    let path = format!("v1/chats/{}/messages/{}/reactions", s(open, "id"), s(m, "id"));
                    self.post(ctx, Method::POST, path, json!({ "reactionKey": key }), true);
                }
                self.stop_compose(ctx);
            }
            Mode::Edit | Mode::Send | Mode::Idle => {
                let Some(open) = self.open.clone() else {
                    self.stop_compose(ctx);
                    return;
                };
                if text.is_empty() {
                    self.stop_compose(ctx);
                    return;
                }
                let chat = s(&open, "id").to_string();
                match (&self.editing, self.mode) {
                    (Some(m), Mode::Edit) => {
                        let path = format!("v1/chats/{chat}/messages/{}", s(m, "id"));
                        self.post(ctx, Method::PUT, path, json!({ "text": text }), true);
                    }
                    _ => {
                        let mut body = json!({ "text": text });
                        if let Some(r) = &self.reply_to {
                            body["replyToMessageID"] = json!(s(r, "id"));
                        }
                        // Shown straight away as "sending"; the reload brings the real one.
                        self.messages.push(json!({"id": "pending", "isSender": true, "text": text,
                            "timestamp": Utc::now().to_rfc3339(), "sendStatus": {"status": "sending"}}));
                        self.msg_index = None;
                        self.post(ctx, Method::POST, format!("v1/chats/{chat}/messages"), body, true);
                    }
                }
                self.reply_to = None;
                self.editing = None;
                self.compose.clear();
                if self.mode == Mode::Edit {
                    self.mode = Mode::Send;
                }
            }
        }
    }
}
