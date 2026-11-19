//! PIXEL's controls: what each key does on the canvas and in the palette, the `:` line and
//! the ctrl+o browser, and finding the pixel under the mouse.

use super::*;

impl PixelView {
    // ── keys ─────────────────────────────────────────────────────────────

    /// A key on the canvas. Almost everything has a key here (the list is at the top of
    /// `mod.rs`), so you rarely need to leave it.
    pub(super) fn canvas_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        let step = if shift { 4 } else { 1 };
        if keys::ctrl(key, 'r') {
            self.undo(false);
            return Outcome::Used;
        }
        if keys::ctrl(key, 's') {
            self.command("w", ctx);
            return Outcome::Used;
        }
        if keys::ctrl(key, 'o') {
            self.browse();
            return Outcome::Used;
        }
        match (keys::ch(key), key.code) {
            (Some('h'), _) => self.movement(-1, 0),
            (Some('l'), _) => self.movement(1, 0),
            (Some('k'), _) => self.movement(0, -1),
            (Some('j'), _) => self.movement(0, 1),
            (Some('H'), _) => self.movement(-4, 0),
            (Some('L'), _) => self.movement(4, 0),
            (Some('K'), _) => self.movement(0, -4),
            (Some('J'), _) => self.movement(0, 4),
            (_, KeyCode::Left) => self.movement(-step, 0),
            (_, KeyCode::Right) => self.movement(step, 0),
            (_, KeyCode::Up) => self.movement(0, -step),
            (_, KeyCode::Down) => self.movement(0, step),
            (Some(' '), _) | (_, KeyCode::Enter) => self.act(false),
            (Some('t'), _) => self.toggle_pen(),
            (Some(c), _) if TOOLS.iter().any(|(k, _, _)| *k == c) => {
                let t = TOOLS.iter().find(|(k, _, _)| *k == c).map(|(_, t, _)| *t).unwrap_or(Tool::Pencil);
                self.set_tool(t);
            }
            (Some('F'), _) => {
                self.filled = !self.filled;
                self.changed(false, if self.filled { "filled on" } else { "filled off" });
            }
            (Some('m'), _) => {
                self.mirror = !self.mirror;
                self.changed(false, if self.mirror { "mirror on" } else { "mirror off" });
            }
            (Some('g'), _) => {
                self.grid = !self.grid;
                self.changed(false, if self.grid { "grid on" } else { "grid off" });
            }
            (Some('O'), _) => {
                self.onion = !self.onion;
                self.changed(false, if self.onion { "onion on" } else { "onion off" });
            }
            (Some('+' | '='), _) => {
                self.zoom = (self.zoom + 1).min(8);
                self.changed(false, &format!("zoom {}", self.zoom));
            }
            (Some('-'), _) => {
                self.zoom = (self.zoom - 1).max(1);
                self.changed(false, &format!("zoom {}", self.zoom));
            }
            (Some('0'), _) => {
                self.fit_zoom();
                self.changed(false, &format!("zoom {} (fits)", self.zoom));
            }
            (Some('[' | ','), _) => self.step_colour(-1),
            (Some(']' | '.'), _) => self.step_colour(1),
            (Some('x'), _) => std::mem::swap(&mut self.fg, &mut self.bg),
            (Some('#'), _) => self.open_cmdline("hex #"),
            (Some('{'), _) | (_, KeyCode::PageDown) => {
                self.layer = self.layer.saturating_sub(1);
                let say = format!("layer {}", self.doc.layers[self.layer].name);
                self.changed(false, &say);
            }
            (Some('}'), _) | (_, KeyCode::PageUp) => {
                self.layer = (self.layer + 1).min(self.doc.layers.len() - 1);
                let say = format!("layer {}", self.doc.layers[self.layer].name);
                self.changed(false, &say);
            }
            (Some('A'), _) => {
                self.hist.push(&self.doc);
                self.layer = self.doc.add_layer(self.layer);
                let say = format!("added {}", self.doc.layers[self.layer].name);
                self.changed(true, &say);
            }
            (Some('V'), _) => {
                let l = &mut self.doc.layers[self.layer];
                l.visible = !l.visible;
                let say = format!("{} {}", l.name, if l.visible { "shown" } else { "hidden" });
                self.changed(true, &say);
            }
            (Some('<'), _) => self.frame = (self.frame + self.doc.frames.len() - 1) % self.doc.frames.len(),
            (Some('>'), _) => self.frame = (self.frame + 1) % self.doc.frames.len(),
            (Some('N'), _) => {
                self.hist.push(&self.doc);
                self.frame = self.doc.add_frame(self.frame, true);
                let say = format!("frame {} (a copy)", self.frame + 1);
                self.changed(true, &say);
            }
            (Some('p'), _) => self.toggle_play(),
            (Some('P'), _) => {
                self.palette_at = self.fg.and_then(|c| self.doc.palette.iter().position(|p| *p == c)).unwrap_or(0);
                ctx.focus(PALETTE);
            }
            (Some('u'), _) => self.undo(true),
            (Some(':'), _) => self.open_cmdline(""),
            (_, KeyCode::Esc) => return if self.cancel() { Outcome::Used } else { Outcome::Ignored },
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    /// A key in PALETTE: hjkl move between swatches; space/enter makes it fg, b makes it bg,
    /// x clears bg (transparent). Choosing goes back to the canvas.
    pub(super) fn palette_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        let n = self.doc.palette.len();
        if n == 0 {
            return Outcome::Ignored;
        }
        let at = self.palette_at as isize;
        let go = |d: isize| (at + d).clamp(0, n as isize - 1) as usize;
        match (keys::ch(key), key.code) {
            (Some('h'), _) | (_, KeyCode::Left) => self.palette_at = go(-1),
            (Some('l'), _) | (_, KeyCode::Right) => self.palette_at = go(1),
            (Some('k'), _) | (_, KeyCode::Up) => self.palette_at = go(-(SWATCHES_PER_ROW as isize)),
            (Some('j'), _) | (_, KeyCode::Down) => self.palette_at = go(SWATCHES_PER_ROW as isize),
            (Some(' '), _) | (_, KeyCode::Enter) => {
                self.fg = Some(self.doc.palette[self.palette_at]);
                self.message = format!("FG {}", px::hex(self.doc.palette[self.palette_at]));
                ctx.focus(CANVAS);
            }
            (Some('b'), _) => {
                self.bg = Some(self.doc.palette[self.palette_at]);
                self.message = format!("BG {}", px::hex(self.doc.palette[self.palette_at]));
                ctx.focus(CANVAS);
            }
            (Some('x'), _) => {
                self.bg = None;
                self.message = "BG clear".into();
                ctx.focus(CANVAS);
            }
            (_, KeyCode::Esc) => ctx.focus(CANVAS),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    /// Open the `:` line (with `prefill` already typed).
    pub(super) fn open_cmdline(&mut self, prefill: &str) {
        let mut input = Input::new(": command   (try :new 28 28, :avatar, :export strip 4)");
        input.set(prefill);
        self.cmdline = Some(input);
    }

    // ── files ────────────────────────────────────────────────────────────

    /// ctrl+o: the file browser on `pixel_dir`, showing pictures with previews.
    pub(super) fn browse(&mut self) {
        let avatar = avatar_dir(&self.avatar);
        self.browser =
            Some(crate::channels::creative::browser::Browser::new(crate::channels::creative::browser::Spec {
                title: "PIXEL · FILES".into(),
                root: self.dir.clone(),
                suffixes: vec!["mpx".into(), "png".into()],
                also_show: vec![],
                document_dir: Some("avatar.json".into()),
                extras: if avatar.join("avatar.json").exists() {
                    vec![(avatar, "Mabel's avatar".into())]
                } else {
                    vec![]
                },
                new_suffix: "mpx".into(),
                dirty: self.dirty,
                current: self.doc.path.clone(),
                preview: crate::channels::creative::browser::Preview::Sprite,
            }));
    }

    /// What the browser chose: open a picture, or make a new one the same size with the same
    /// palette (saving the current one first if asked).
    pub(super) fn browsed(&mut self, choice: crate::channels::creative::browser::Choice) {
        let result: anyhow::Result<String> = (|| {
            if choice.save_first {
                if self.doc.path.is_none() {
                    return Ok("this one has no name yet: :w name.mpx first".into());
                }
                px::save(&mut self.doc, None)?;
            }
            if choice.new {
                let pal = self.doc.palette.clone();
                let (w, h) = (self.doc.w, self.doc.h);
                self.doc = Doc::new(w as i64, h as i64, "db32");
                self.doc.palette = pal;
                px::save(&mut self.doc, Some(&choice.path))?;
                self.hist = History::default();
                (self.frame, self.layer, self.dirty, self.cx, self.cy) = (0, 0, false, 0, 0);
                self.remember();
                self.zoom = 0;
                return Ok(format!(
                    "new {} ({w}×{h}; :resize to change)",
                    choice.path.file_name().map_or(String::new(), |n| n.to_string_lossy().into_owned())
                ));
            }
            self.open(&choice.path)?;
            Ok(format!(
                "opened {}",
                choice.path.file_name().map_or(String::new(), |n| n.to_string_lossy().into_owned())
            ))
        })();
        self.message = result.unwrap_or_else(|e| format!("✖ {e}"));
    }

    /// Pixel under a mouse position on the canvas.
    pub(super) fn pixel_at(&self, col: u16, row: u16) -> Option<(i64, i64)> {
        let r = self.canvas_rect;
        if !r.contains((col, row).into()) {
            return None;
        }
        let (pw, ph) = self.cell();
        let (x, y) = ((col - r.x) as i64, (row - r.y) as i64);
        let (px_, py_) = if ph == 0 { (self.ox + x, self.oy + y * 2) } else { (self.ox + x / pw, self.oy + y / ph) };
        self.doc.inside(px_, py_).then_some((px_, py_))
    }
}
