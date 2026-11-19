//! Drawing PIXEL: the canvas (zoomed, with grid and onion skin), TOOLS, PALETTE, PREVIEW,
//! LAYERS, FRAMES, the status line, and the browser's picture preview.

use super::*;

/// The browser's preview of a sprite: its first frame (half-blocks, scaled to fit) and its facts.
pub fn thumbnail(path: &Path, width: u16, height: u16, pal: &Palette) -> Vec<Line<'static>> {
    let Ok(doc) = px::open_any(path) else { return vec![Line::styled("can't read it", Style::new().fg(pal.red))] };
    let grid = doc.composite(0);
    let (room_w, room_h) = ((width as usize).saturating_sub(2).max(4), (height as usize).saturating_sub(7).max(2));
    let mut out = vec![Line::raw("")];
    if doc.w * 2 <= room_w && doc.h <= room_h {
        // Square pixels: 2 cells × 1 row each (bigger while it fits).
        let scale = (room_w / (doc.w * 2)).min(room_h / doc.h).clamp(1, 4);
        for (y, row) in grid.iter().enumerate() {
            for _ in 0..scale {
                let mut spans = vec![Span::raw(" ")];
                for (x, c) in row.iter().enumerate() {
                    spans.push(Span::styled("  ".repeat(scale), Style::new().bg(rgb(c.unwrap_or(checker(x, y))))));
                }
                out.push(Line::from(spans));
            }
        }
    } else {
        let step = doc.w.div_ceil(room_w).max(doc.h.div_ceil(room_h * 2)).max(1);
        let rows: Vec<usize> = (0..doc.h).step_by(step).collect();
        let cols: Vec<usize> = (0..doc.w).step_by(step).collect();
        for pair in rows.chunks(2) {
            let mut spans = vec![Span::raw(" ")];
            for &x in &cols {
                let top = grid[pair[0]][x].unwrap_or(checker(x, pair[0]));
                let bottom = pair.get(1).map(|&y1| grid[y1][x].unwrap_or(checker(x, y1)));
                let style = Style::new().fg(rgb(top)).bg(bottom.map_or(pal.panel, rgb));
                spans.push(Span::styled("▀", style));
            }
            out.push(Line::from(spans));
        }
    }
    let plural = |n: usize| if n == 1 { "" } else { "s" };
    out.push(Line::raw(""));
    out.push(Line::styled(
        format!(
            " {}×{} · {} frame{} · {} layer{}",
            doc.w,
            doc.h,
            doc.frames.len(),
            plural(doc.frames.len()),
            doc.layers.len(),
            plural(doc.layers.len())
        ),
        Style::new().fg(pal.amber),
    ));
    if !doc.tags.is_empty() {
        let tags: Vec<String> = doc.tags.iter().map(|t| format!("{} {}–{}", t.name, t.start + 1, t.end + 1)).collect();
        out.push(Line::styled(format!(" {}", tags.join(" · ")), Style::new().fg(pal.teal)));
    }
    let kind = match doc.kind {
        px::Kind::Mpx => "mpx",
        px::Kind::Png => "png",
        px::Kind::Avatar => "avatar",
    };
    out.push(Line::styled(format!(" {} colours · {kind}", doc.palette.len()), Style::new().fg(pal.amber_dim)));
    out
}

impl PixelView {
    // ── drawing ──────────────────────────────────────────────────────────

    /// A colour as a little block (shaded ░░ for transparent).
    pub(super) fn swatch(c: Px, pal: &Palette) -> Span<'static> {
        match c {
            Some(c) => Span::styled("██", Style::new().fg(rgb(c))),
            None => Span::styled("░░", Style::new().fg(pal.amber_dim)),
        }
    }

    /// CANVAS: the picture at the current zoom (half-blocks at zoom 1, square pixels above),
    /// scrolled so the cursor stays in view, with the checkerboard behind transparent pixels,
    /// the grid, and the cursor box.
    pub(super) fn render_canvas(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let name = self
            .doc
            .path
            .as_ref()
            .and_then(|p| p.file_name())
            .map_or("untitled".into(), |n| n.to_string_lossy().into_owned());
        let block =
            ui::panel(pal, &format!("CANVAS · {name}{}", if self.dirty { " ●" } else { "" }), view.panel_state(CANVAS))
                .padding(ratatui::widgets::Padding::ZERO)
                .title_bottom(
                    Line::styled(
                        format!(" {}×{} · zoom {} ", self.doc.w, self.doc.h, self.zoom),
                        Style::new().fg(pal.amber_dim),
                    )
                    .right_aligned(),
                );
        let inner = block.inner(area);
        frame.render_widget(block, area);
        frame.render_widget(Paragraph::new("").style(Style::new().bg(pal.bg)), inner);
        self.canvas_rect = inner;
        if self.zoom == 0 {
            self.fit_zoom();
        }
        let (pw, ph) = self.cell();
        let focused = view.inside(CANVAS) || self.anchor.is_some();
        let blink = (std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis())
            / 500)
            .is_multiple_of(2);
        let grid = self.view_grid();
        // Pixels across and down that fit; scroll to keep the cursor in view.
        let (fit_w, fit_h) = if ph == 0 {
            (inner.width as i64, inner.height as i64 * 2)
        } else {
            (inner.width as i64 / pw, inner.height as i64 / ph)
        };
        if self.cx < self.ox {
            self.ox = self.cx;
        } else if self.cx >= self.ox + fit_w.max(1) {
            self.ox = self.cx - fit_w + 1;
        }
        if self.cy < self.oy {
            self.oy = self.cy;
        } else if self.cy >= self.oy + fit_h.max(1) {
            self.oy = self.cy - fit_h + 1;
        }
        self.ox = self.ox.clamp(0, (self.doc.w as i64 - fit_w).max(0));
        self.oy = self.oy.clamp(0, (self.doc.h as i64 - fit_h).max(0));
        let color_at = |x: i64, y: i64| grid[y as usize][x as usize].unwrap_or(checker(x as usize, y as usize));
        let buf = frame.buffer_mut();
        if ph == 0 {
            for row in 0..inner.height as i64 {
                let (y0, y1) = (self.oy + row * 2, self.oy + row * 2 + 1);
                if y0 >= self.doc.h as i64 {
                    break;
                }
                for col in 0..(inner.width as i64).min(self.doc.w as i64 - self.ox) {
                    let x = self.ox + col;
                    let mut top = color_at(x, y0);
                    let mut bottom = if y1 < self.doc.h as i64 { color_at(x, y1) } else { [0x0c, 0x08, 0x04] };
                    let (mut tc, mut bc) = (rgb(top), rgb(bottom));
                    if focused && blink {
                        if (x, y0) == (self.cx, self.cy) {
                            tc = contrast(top);
                        }
                        if (x, y1) == (self.cx, self.cy) {
                            bc = contrast(bottom);
                        }
                    }
                    let _ = (&mut top, &mut bottom);
                    buf[(inner.x + col as u16, inner.y + row as u16)].set_symbol("▀").set_fg(tc).set_bg(bc);
                }
            }
        } else {
            for py in self.oy..(self.oy + fit_h).min(self.doc.h as i64) {
                for pxx in self.ox..(self.ox + fit_w).min(self.doc.w as i64) {
                    let c = color_at(pxx, py);
                    let cur = focused && (pxx, py) == (self.cx, self.cy);
                    for sub in 0..ph {
                        let y = inner.y + ((py - self.oy) * ph + sub) as u16;
                        let x0 = inner.x + ((pxx - self.ox) * pw) as u16;
                        let text: String = if cur && sub == (ph - 1) / 2 {
                            format!("[{}]", " ".repeat((pw - 2).max(0) as usize))
                        } else if !cur && self.grid && pw >= 4 {
                            format!("▏{}", " ".repeat((pw - 1) as usize))
                        } else {
                            " ".repeat(pw as usize)
                        };
                        let style = if cur {
                            Style::new().fg(contrast(c)).bg(rgb(c)).add_modifier(Modifier::BOLD)
                        } else if self.grid && pw >= 4 {
                            Style::new().fg(rgb(mix(c, [0, 0, 0], 0.35))).bg(rgb(c))
                        } else {
                            Style::new().bg(rgb(c))
                        };
                        for (i, ch) in text.chars().enumerate() {
                            if x0 + (i as u16) < inner.right() && y < inner.bottom() {
                                buf[(x0 + i as u16, y)].set_char(ch).set_style(style);
                            }
                        }
                    }
                }
            }
        }
    }

    /// TOOLS: each tool with its key (the current one lit), fg and bg, and the toggles.
    pub(super) fn tools_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let mut out = vec![];
        for (k, t, name) in TOOLS {
            let on = t == self.tool;
            let style = if on { bold(pal.amber_hi) } else { Style::new().fg(pal.amber) };
            out.push(Line::from(vec![
                Span::styled(
                    format!("{}{k} ", if on { "▸ " } else { "  " }),
                    if on { bold(pal.amber_hi) } else { Style::new().fg(pal.amber_dim) },
                ),
                Span::styled(name, style),
            ]));
        }
        out.push(Line::raw(""));
        let name = |c: Px| c.map_or("clear".into(), px::hex);
        out.push(Line::from(vec![
            Span::styled("FG ", Style::new().fg(pal.amber_dim)),
            Self::swatch(self.fg, pal),
            Span::styled(format!(" {}", name(self.fg)), Style::new().fg(pal.amber)),
        ]));
        out.push(Line::from(vec![
            Span::styled("BG ", Style::new().fg(pal.amber_dim)),
            Self::swatch(self.bg, pal),
            Span::styled(format!(" {}", name(self.bg)), Style::new().fg(pal.amber)),
        ]));
        out.push(Line::raw(""));
        for (k, what, on) in [
            ("F", "filled", self.filled),
            ("m", "mirror", self.mirror),
            ("g", "grid", self.grid),
            ("O", "onion", self.onion),
        ] {
            out.push(Line::from(vec![
                Span::styled(format!("{k} {what:<7}"), Style::new().fg(pal.amber_dim)),
                Span::styled(
                    if on { "on" } else { "off" },
                    if on { bold(pal.teal) } else { Style::new().fg(pal.amber_dim) },
                ),
            ]));
        }
        out.push(Line::from(vec![
            Span::styled("t pen     ", Style::new().fg(pal.amber_dim)),
            Span::styled(
                if self.pen { "DOWN" } else { "up" },
                if self.pen { bold(pal.red) } else { Style::new().fg(pal.amber_dim) },
            ),
        ]));
        out
    }

    /// PALETTE: the swatches, `SWATCHES_PER_ROW` to a row, fg marked.
    pub(super) fn palette_lines(&self, pal: &Palette, focused: bool) -> Vec<Line<'static>> {
        let mut out = vec![];
        for row in self.doc.palette.chunks(SWATCHES_PER_ROW).enumerate() {
            let (r, colours) = row;
            let spans: Vec<Span> = colours
                .iter()
                .enumerate()
                .map(|(i, c)| {
                    let n = r * SWATCHES_PER_ROW + i;
                    if focused && n == self.palette_at {
                        Span::styled("▐▌", Style::new().fg(contrast(*c)).bg(rgb(*c)).add_modifier(Modifier::BOLD))
                    } else if Some(*c) == self.fg {
                        Span::styled("[]", Style::new().fg(contrast(*c)).bg(rgb(*c)).add_modifier(Modifier::BOLD))
                    } else if Some(*c) == self.bg {
                        Span::styled("<>", Style::new().fg(contrast(*c)).bg(rgb(*c)))
                    } else {
                        Span::styled("██", Style::new().fg(rgb(*c)))
                    }
                })
                .collect();
            out.push(Line::from(spans));
        }
        out.push(Line::raw(""));
        if focused {
            out.push(Line::styled("enter FG · b BG · x clear BG · esc", Style::new().fg(pal.sand)));
        } else {
            out.push(Line::styled("P pick · , . or [ ] step", Style::new().fg(pal.amber_dim)));
            out.push(Line::styled("x swap · # hex", Style::new().fg(pal.amber_dim)));
        }
        out
    }

    /// PREVIEW: the frame at actual size (half-blocks), or the playing animation.
    pub(super) fn render_preview(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let block = ui::panel(pal, "PREVIEW", PanelState::Idle);
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let f = self.preview_frame(view.now);
        let grid = self.doc.composite(f.min(self.doc.frames.len() - 1));
        let step = self
            .doc
            .w
            .div_ceil(inner.width.max(1) as usize)
            .max(self.doc.h.div_ceil((inner.height.max(1) as usize) * 2))
            .max(1);
        let rows: Vec<usize> = (0..self.doc.h).step_by(step).collect();
        let cols: Vec<usize> = (0..self.doc.w).step_by(step).collect();
        let buf = frame.buffer_mut();
        for (r, pair) in rows.chunks(2).enumerate() {
            if r as u16 >= inner.height {
                break;
            }
            for (cx, &x) in cols.iter().enumerate() {
                if cx as u16 >= inner.width {
                    break;
                }
                let top = grid[pair[0]][x];
                let bottom = pair.get(1).and_then(|&y| grid[y][x]);
                let cell = &mut buf[(inner.x + cx as u16, inner.y + r as u16)];
                match (top, bottom) {
                    (None, None) => {}
                    (None, Some(b)) => {
                        cell.set_symbol("▄").set_fg(rgb(b));
                    }
                    (Some(t), b) => {
                        cell.set_symbol("▀").set_fg(rgb(t)).set_bg(b.map_or(pal.panel, rgb));
                    }
                }
            }
        }
    }

    /// LAYERS: top layer first, the current one highlighted, ● shown / ○ hidden.
    pub(super) fn layer_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let mut out: Vec<Line> = (0..self.doc.layers.len())
            .rev()
            .map(|i| {
                let l = &self.doc.layers[i];
                let style = if i == self.layer {
                    bold(pal.amber).add_modifier(Modifier::REVERSED)
                } else {
                    Style::new().fg(pal.amber)
                };
                Line::from(vec![
                    Span::styled(
                        if l.visible { "● " } else { "○ " },
                        Style::new().fg(if l.visible { pal.teal } else { pal.amber_dim }),
                    ),
                    Span::styled(l.name.clone(), style),
                ])
            })
            .collect();
        out.push(Line::styled("{ } pick · A add · V hide", Style::new().fg(pal.amber_dim)));
        out
    }

    /// FRAMES: each frame with its duration and tag, the current one marked.
    pub(super) fn frame_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let mut out = vec![];
        for chunk in (0..self.doc.frames.len()).collect::<Vec<_>>().chunks(8) {
            let spans: Vec<Span> = chunk
                .iter()
                .flat_map(|&i| {
                    let style = if i == self.frame {
                        bold(pal.amber).add_modifier(Modifier::REVERSED)
                    } else {
                        Style::new().fg(pal.amber)
                    };
                    [Span::styled(format!("{:02}", i + 1), style), Span::raw(" ")]
                })
                .collect();
            out.push(Line::from(spans));
        }
        let mut info = vec![Span::styled(format!("{} ms", self.doc.frames[self.frame].ms), Style::new().fg(pal.amber))];
        if let Some(tag) = self.doc.tag_at(self.frame) {
            info.push(Span::styled(
                format!(" · {} {}–{}", tag.name, tag.start + 1, tag.end + 1),
                Style::new().fg(pal.teal),
            ));
        }
        if self.playing.is_some() {
            info.push(Span::styled(" · ▶", bold(pal.teal)));
        }
        out.push(Line::from(info));
        out.push(Line::styled("< > pick · N new · p play", Style::new().fg(pal.amber_dim)));
        out
    }

    /// The line under the panels: the cursor's position and the colour under it, the tool,
    /// the layer and frame, and the last message.
    pub(super) fn status_line(&self, pal: &Palette) -> Line<'static> {
        let c = self.doc.composite(self.frame)[self.cy as usize][self.cx as usize];
        let mut spans = vec![
            Span::styled(format!(" x {:>3} y {:>3} ", self.cx, self.cy), bold(pal.amber_hi)),
            Span::styled("│ ", Style::new().fg(pal.amber_dim)),
            Self::swatch(c, pal),
            Span::styled(format!(" {:<8}", c.map_or("clear".into(), px::hex)), Style::new().fg(pal.amber)),
            Span::styled(format!("│ {} ", tool_name(self.tool)), Style::new().fg(pal.amber)),
            Span::styled(
                format!(
                    "│ layer {}/{} │ frame {}/{} ",
                    self.layer + 1,
                    self.doc.layers.len(),
                    self.frame + 1,
                    self.doc.frames.len()
                ),
                Style::new().fg(pal.amber_dim),
            ),
        ];
        if !self.message.is_empty() {
            spans.push(Span::styled(format!("│ {}", self.message), bold(pal.sand)));
        }
        Line::from(spans)
    }
}
