//! PIXEL's editing: marking changes, moving the cursor, strokes and tools, colours, undo,
//! and playing the animation in PREVIEW.

use super::*;

impl PixelView {
    // ── editing ──────────────────────────────────────────────────────────

    /// After any change: mark the picture unsaved if pixels changed, show `say`, and pull the
    /// cursor, layer and frame back inside (a resize or a deleted frame can leave them out).
    pub(super) fn changed(&mut self, pixels: bool, say: &str) {
        if pixels {
            self.dirty = true;
        }
        self.message = say.to_string();
        self.cx = self.cx.min(self.doc.w as i64 - 1);
        self.cy = self.cy.min(self.doc.h as i64 - 1);
        self.layer = self.layer.min(self.doc.layers.len() - 1);
        self.frame = self.frame.min(self.doc.frames.len() - 1);
    }

    /// Move the cursor (staying on the picture). With the pen down, moving draws.
    pub(super) fn movement(&mut self, dx: i64, dy: i64) {
        let nx = (self.cx + dx).clamp(0, self.doc.w as i64 - 1);
        let ny = (self.cy + dy).clamp(0, self.doc.h as i64 - 1);
        if self.pen && matches!(self.tool, Tool::Pencil | Tool::Eraser) {
            return self.stroke_to(nx, ny, false);
        }
        (self.cx, self.cy) = (nx, ny);
        self.message.clear();
    }

    /// Draw a line from the cursor to (x, y) with the pencil's colour (or erase) and move there.
    pub(super) fn stroke_to(&mut self, x: i64, y: i64, use_bg: bool) {
        let color = if self.tool == Tool::Eraser {
            None
        } else if use_bg {
            self.bg
        } else {
            self.fg
        };
        self.doc.put(self.frame, self.layer, &px::line(self.cx, self.cy, x, y), color, self.mirror);
        (self.cx, self.cy) = (x, y);
        self.changed(true, "");
    }

    /// space/enter (shift: with bg): use the tool at the cursor. Pencil and eraser paint one
    /// pixel; fill floods; pick takes the colour; line/rect/ellipse take two presses (the first
    /// sets a corner, the second draws).
    pub(super) fn act(&mut self, use_bg: bool) {
        let color = if use_bg { self.bg } else { self.fg };
        let (x, y) = (self.cx, self.cy);
        match self.tool {
            Tool::Pick => {
                let picked = self.doc.composite(self.frame)[y as usize][x as usize];
                if use_bg {
                    self.bg = picked;
                } else {
                    self.fg = picked;
                }
                self.tool = self.prev_tool;
                let name = picked.map_or("transparent".into(), px::hex);
                self.changed(false, &format!("picked {name}"));
            }
            Tool::Line | Tool::Rect | Tool::Ellipse => match self.anchor {
                None => {
                    self.anchor = Some((x, y));
                    let msg = format!("{} from {x},{y}: move, then space · esc cancels", tool_name(self.tool));
                    self.changed(false, &msg);
                }
                Some((ax, ay)) => {
                    self.hist.push(&self.doc);
                    let pts = self.shape(ax, ay, x, y);
                    self.doc.put(self.frame, self.layer, &pts, color, self.mirror);
                    self.anchor = None;
                    self.changed(true, "");
                }
            },
            Tool::Fill => {
                self.hist.push(&self.doc);
                let area = px::flood(&self.doc.frames[self.frame].cels[self.layer], x, y);
                self.doc.put(self.frame, self.layer, &area, color, self.mirror);
                self.changed(true, "");
            }
            Tool::Pencil | Tool::Eraser => {
                self.hist.push(&self.doc);
                let c = if self.tool == Tool::Eraser { None } else { color };
                self.doc.put(self.frame, self.layer, &[(x, y)], c, self.mirror);
                self.changed(true, "");
            }
        }
    }

    /// esc: a shape, the pen or playback first; false if there was nothing to cancel.
    pub(super) fn cancel(&mut self) -> bool {
        if self.anchor.is_some() || self.pen || self.playing.is_some() {
            (self.anchor, self.pen, self.playing) = (None, false, None);
            self.changed(false, "");
            return true;
        }
        false
    }

    /// t: pen down/up. Pen down paints the cursor's pixel and then every pixel you move over
    /// (one undo step for the whole stroke).
    pub(super) fn toggle_pen(&mut self) {
        if !matches!(self.tool, Tool::Pencil | Tool::Eraser) {
            self.set_tool(Tool::Pencil);
        }
        self.pen = !self.pen;
        if self.pen {
            self.hist.push(&self.doc);
            let c = if self.tool == Tool::Eraser { None } else { self.fg };
            self.doc.put(self.frame, self.layer, &[(self.cx, self.cy)], c, self.mirror);
            self.changed(true, "pen down: moving draws · t or esc lifts it");
        } else {
            self.changed(false, "pen up");
        }
    }

    /// Switch tool (remembering the previous one for pick to return to; shapes in progress end).
    pub(super) fn set_tool(&mut self, t: Tool) {
        if t == Tool::Pick && self.tool != Tool::Pick {
            self.prev_tool = self.tool;
        }
        (self.tool, self.anchor) = (t, None);
        if !matches!(t, Tool::Pencil | Tool::Eraser) {
            self.pen = false;
        }
        self.changed(false, "");
    }

    /// , . or [ ]: fg becomes the next or previous palette colour.
    pub(super) fn step_colour(&mut self, step: isize) {
        let pal = &self.doc.palette;
        if pal.is_empty() {
            return;
        }
        let i = self.fg.and_then(|c| pal.iter().position(|p| *p == c)).map_or(-1, |i| i as isize);
        self.fg = Some(pal[(i + step).rem_euclid(pal.len() as isize) as usize]);
    }

    /// u undoes, ctrl+r redoes.
    pub(super) fn undo(&mut self, back: bool) {
        if self.hist.step(&mut self.doc, back) {
            self.changed(true, if back { "undone" } else { "redone" });
        } else {
            self.changed(false, if back { "nothing to undo" } else { "nothing to redo" });
        }
    }

    /// p: play or stop the animation in PREVIEW: the tag the current frame is in (ping-pong
    /// tags go there and back), or every frame.
    pub(super) fn toggle_play(&mut self) {
        if self.playing.take().is_some() {
            return;
        }
        let frames: Vec<usize> = match self.doc.tag_at(self.frame) {
            Some(tag) => {
                let mut f: Vec<usize> = (tag.start..=tag.end).collect();
                if tag.extra.get("direction").and_then(|d| d.as_str()) == Some("pingpong") && f.len() > 2 {
                    let back: Vec<usize> = f[1..f.len() - 1].iter().rev().copied().collect();
                    f.extend(back);
                }
                f
            }
            None => (0..self.doc.frames.len()).collect(),
        };
        self.playing = Some((frames, 0, Instant::now()));
    }

    /// The frame the preview shows now (advancing a playing animation).
    pub(super) fn preview_frame(&mut self, now: Instant) -> usize {
        let Some((frames, i, since)) = &mut self.playing else { return self.frame };
        let f = frames[*i % frames.len()];
        let ms = self.doc.frames.get(f).map_or(100, |fr| fr.ms).max(20) as u64;
        if now.duration_since(*since) >= Duration::from_millis(ms) {
            *i += 1;
            *since = now;
        }
        frames[*i % frames.len()]
    }
}
