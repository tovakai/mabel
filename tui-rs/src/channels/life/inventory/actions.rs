//! What INVENTORY does: modes, going into and out of places, writing to tk-index, every key,
//! and the popups (forms, questions).

use super::*;

impl Inventory {
    // ── actions ──────────────────────────────────────────────────────────

    /// Show something else (browse, search, inbox, loose ends), from the top.
    pub(super) fn set_mode(&mut self, mode: Mode, ctx: &Ctx) {
        self.mode = mode;
        self.sel = 0;
        self.scroll = 0;
        self.rebuild(&ctx.pal);
    }

    /// Go into the place at `route`.
    pub(super) fn zoom(&mut self, route: &str, ctx: &Ctx) {
        self.mode = Mode::Browse;
        self.route = self.world.as_ref().map_or(route.to_string(), |w| w.resolve(route));
        self.sel = 0;
        self.scroll = 0;
        self.rebuild(&ctx.pal);
    }

    /// enter (or a click) on the selected thing: zoom into a place, show an item, open a
    /// suggestion's item, or show the loose ends.
    pub(super) fn activate(&mut self, ctx: &mut Ctx) {
        match self.targets.get(self.sel).cloned() {
            Some(Target::Zoom(r)) => self.zoom(&r, ctx),
            Some(Target::Pick(id)) => self.pick(&id, ctx),
            Some(Target::Suggest(n)) => {
                if let Some(id) = self.suggestions.get(n).and_then(|s| s.ids.first()).cloned() {
                    self.show_item(&id, ctx);
                }
            }
            Some(Target::Loose) => self.set_mode(Mode::Loose, ctx),
            None => {}
        }
    }

    /// Show an item on the card.
    pub(super) fn pick(&mut self, id: &str, ctx: &Ctx) {
        self.show_item(id, ctx);
        // Redraw a map with this bin highlighted.
        if self.mode == Mode::Browse {
            self.rebuild(&ctx.pal);
        }
    }

    /// backspace: out of search/inbox/loose ends back to browsing, or up one place.
    pub(super) fn up(&mut self, ctx: &Ctx) {
        if self.mode != Mode::Browse {
            return self.set_mode(Mode::Browse, ctx);
        }
        if let (Some(w), false) = (&self.world, self.route.is_empty()) {
            let chain = w.chain(&self.route);
            self.route = if chain.len() > 1 { chain[chain.len() - 2].clone() } else { String::new() };
            self.sel = 0;
            self.scroll = 0;
            self.rebuild(&ctx.pal);
        }
    }

    /// A write through tk-index; the world and the card reload after it.
    pub(super) fn write(&self, path: String, data: Value, done: String, ctx: &Ctx) {
        let idx = self.idx.clone();
        ctx.run(ID, async move { Got::Wrote(idx.post(&path, &data).await.map(|_| done).map_err(|e| e.to_string())) });
    }

    /// The item on the card, or a toast asking to pick one (for e, m, c, r, k).
    pub(super) fn need_item(&self, ctx: &mut Ctx) -> Option<Value> {
        if self.item.is_none() {
            ctx.toast("INVENTORY", "Pick an item first");
        }
        self.item.clone()
    }

    /// The housekeeping suggestion that's selected, if one is.
    pub(super) fn focused_suggestion(&self) -> Option<usize> {
        match self.targets.get(self.sel) {
            Some(Target::Suggest(n)) => Some(*n),
            _ => None,
        }
    }

    /// The keys that work in every section (the list at the top of `mod.rs`).
    pub(super) fn common(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.world.is_none() && !matches!(keys::ch(key), Some('g')) {
            return Outcome::Ignored;
        }
        let id = |d: &Value| value_text(&d["id"]);
        let name = |d: &Value| value_text(&d["name"]);
        match (keys::ch(key), key.code) {
            (_, KeyCode::Backspace) => self.up(ctx),
            (Some('/'), _) => ctx.focus(SEARCH),
            (Some('H'), _) => {
                self.route.clear();
                self.set_mode(Mode::Browse, ctx);
            }
            (Some('b'), _) => self.set_mode(Mode::Inbox, ctx),
            (Some('o'), _) => self.set_mode(Mode::Loose, ctx),
            (Some('g'), _) => self.load_world(self.selected.clone(), ctx),
            (Some('e'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let fields = EDIT_FIELDS
                        .iter()
                        .map(|(k, label, kind)| {
                            let kind = match *kind {
                                "line" => Kind::Line,
                                "text" => Kind::Text,
                                opts => Kind::Choice(opts.split('|').map(String::from).collect()),
                            };
                            Field::new(k, label, &value_text(&d[*k]), kind)
                        })
                        .collect();
                    self.popup = Some(Popup::Edit(Form::new(&format!("EDIT #{}", id(&d)), fields), id(&d)));
                }
            }
            (Some('m'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let f = Field::new(
                        "destination",
                        "New home address (16 characters, e.g. GRIDES0001C03R05)",
                        &value_text(&d["address"]),
                        Kind::Line,
                    );
                    self.popup =
                        Some(Popup::Move(Form::new(&format!("MOVE {}", name(&d).to_uppercase()), vec![f]), id(&d)));
                }
            }
            (Some('c'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let fields = vec![
                        Field::new("destination", "Where is it going? (optional)", "", Kind::Line),
                        Field::new("reason", "What for? (optional)", "", Kind::Line),
                    ];
                    self.popup = Some(Popup::Checkout(
                        Form::new(&format!("CHECK OUT {}", name(&d).to_uppercase()), fields),
                        id(&d),
                    ));
                }
            }
            (Some('r'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    self.write(
                        format!("items/{}/return", id(&d)),
                        json!({}),
                        format!("{} is back home", name(&d)),
                        ctx,
                    );
                }
            }
            (Some('k'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let f = Field::new("add", "Also called", "", Kind::Line);
                    self.popup = Some(Popup::Nickname(
                        Form::new(&format!("NICKNAME FOR {}", name(&d).to_uppercase()), vec![f]),
                        id(&d),
                    ));
                }
            }
            (Some('p'), _) => {
                if let (Some(d), Some(w)) = (&self.item, &self.world)
                    && let Some(addr) = d["address"].as_str()
                {
                    let r = w.resolve(&w.place_route(Some(addr)));
                    self.zoom(&r, ctx);
                }
            }
            (Some('u'), _) => {
                let idx = self.idx.clone();
                ctx.run(ID, async move {
                    let last =
                        idx.get("undo").await.ok().map(|v| value_text(&v["last"]["label"])).filter(|l| !l.is_empty());
                    let done = idx.post("undo", &json!({})).await;
                    Got::Wrote(
                        done.map(|_| format!("↶ undid: {}", last.unwrap_or_else(|| "the last change".into())))
                            .map_err(|e| e.to_string()),
                    )
                });
            }
            (Some('a'), _) if self.mode == Mode::Inbox => {
                let Some(n) = self.focused_suggestion() else { return Outcome::Used };
                let s = self.suggestions[n].clone();
                let Some(edit) = s.edit.clone() else {
                    ctx.toast("INVENTORY", "That one is a to-do for you (x ticks it off once done)");
                    return Outcome::Used;
                };
                let idx = self.idx.clone();
                ctx.run(ID, async move {
                    for id in &s.ids {
                        if let Err(e) = idx.post(&format!("items/{id}/edit"), &edit).await {
                            return Got::Applied(Err(format!("#{id}: {e}")), None);
                        }
                    }
                    Got::Applied(Ok(format!("✓ applied to {} item(s): {} (undo: u)", s.ids.len(), s.change)), Some(n))
                });
            }
            (Some('x'), _) if self.mode == Mode::Inbox => {
                if let Some(n) = self.focused_suggestion()
                    && hk::tick(&self.vault, &self.suggestions[n], true)
                {
                    ctx.toast("INVENTORY", "ticked off");
                    self.rebuild(&ctx.pal);
                }
            }
            (Some('v'), _) if self.mode == Mode::Inbox => {
                if let Some(n) = self.focused_suggestion() {
                    let open = OpenNote {
                        vault: "core of truth".into(),
                        rel: hk::NOTE.into(),
                        line: Some(self.suggestions[n].line_no),
                    };
                    ctx.requests.push(Request::Deliver { to: "vault", msg: Box::new(open) });
                }
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    /// Keys while a form is open: the form takes them; when it's filled in, it's sent to
    /// tk-index (esc cancels).
    pub(super) fn popup_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) {
        let Some(mut popup) = self.popup.take() else { return };
        let (form, _) = match &mut popup {
            Popup::Edit(f, id) | Popup::Move(f, id) | Popup::Checkout(f, id) | Popup::Nickname(f, id) => {
                (f, id.clone())
            }
        };
        let answer = form.key(key);
        let values = match answer {
            Answer::Open => {
                self.popup = Some(popup);
                return;
            }
            Answer::Cancel => return,
            Answer::Chosen(v) => v,
        };
        let get =
            |k: &str| values.iter().find(|(key, _)| key == k).map(|(_, v)| v.trim().to_string()).unwrap_or_default();
        let name = self.item.as_ref().map(|d| value_text(&d["name"])).unwrap_or_default();
        match popup {
            Popup::Edit(_, id) => {
                let Some(d) = &self.item else { return };
                let mut changed = serde_json::Map::new();
                for (k, v) in &values {
                    let old = value_text(&d[k.as_str()]);
                    if old.trim() != v.trim() {
                        let value = if k == "quantity" {
                            if v.trim().is_empty() {
                                Value::Null
                            } else {
                                v.trim().replace(',', ".").parse::<f64>().map_or(Value::Null, |f| json!(f))
                            }
                        } else {
                            json!(v)
                        };
                        changed.insert(k.clone(), value);
                    }
                }
                if !changed.is_empty() {
                    self.write(format!("items/{id}/edit"), Value::Object(changed), format!("Edited {name}"), ctx);
                }
            }
            Popup::Move(_, id) => {
                let dest = get("destination").to_uppercase();
                if !dest.is_empty() {
                    self.write(
                        format!("items/{id}/move"),
                        json!({"destination": dest, "reason": "moved in the TUI"}),
                        format!("Moved {name}"),
                        ctx,
                    );
                }
            }
            Popup::Checkout(_, id) => {
                let dest = get("destination");
                let dest = if dest.is_empty() { Value::Null } else { json!(dest) };
                self.write(
                    format!("items/{id}/checkout"),
                    json!({"destination": dest, "reason": get("reason")}),
                    format!("{name} is out"),
                    ctx,
                );
            }
            Popup::Nickname(_, id) => {
                let add = get("add");
                if !add.is_empty() {
                    self.write(format!("items/{id}/nickname"), json!({ "add": add }), "Nickname added".into(), ctx);
                }
            }
        }
    }
}
