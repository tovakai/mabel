//! Drawing the editor: wrapping lines to the width, the cursor, the selection, the mode line.

use super::*;

impl Vim {
    // ── drawing ──────────────────────────────────────────────────────────

    /// Soft-wrapped: (line, start column, end column) for each row on screen.
    pub(super) fn rows(&self, width: usize) -> Vec<(usize, usize, usize)> {
        let mut out = vec![];
        for (r, line) in self.lines.iter().enumerate() {
            let mut start = 0;
            let mut used = 0;
            for (i, c) in line.iter().enumerate() {
                let w = c.width().unwrap_or(0);
                if used + w > width && i > start {
                    out.push((r, start, i));
                    start = i;
                    used = 0;
                }
                used += w;
            }
            out.push((r, start, line.len()));
        }
        out
    }

    /// Draw the editor into `area`, scrolled so the cursor stays in view, with the mode and
    /// any pending keys on the last line.
    pub fn render(&mut self, frame: &mut Frame, area: Rect, pal: &Palette, focused: bool) {
        if area.width < 2 || area.height == 0 {
            return;
        }
        let rows = self.rows(area.width as usize);
        // The screen row holding the cursor (its last wrap piece that starts at or before it).
        let cursor_row = rows
            .iter()
            .rposition(|(r, s, e)| *r == self.row && *s <= self.col && (self.col < *e || *e == self.line_len(self.row)))
            .unwrap_or(0);
        let h = area.height as usize;
        if cursor_row < self.scroll {
            self.scroll = cursor_row;
        } else if cursor_row >= self.scroll + h {
            self.scroll = cursor_row + 1 - h;
        }
        let sel = self.selection();
        let in_sel = |r: usize, c: usize| sel.is_some_and(|(a, b)| (r, c) >= a && (r, c) < b);
        let mut heading_of = vec![false; self.lines.len()];
        let mut fenced = false;
        for (r, l) in self.lines.iter().enumerate() {
            let s: String = l.iter().take(4).collect();
            if s.starts_with("```") || s.starts_with("~~~") {
                fenced = !fenced;
            }
            heading_of[r] = !fenced && l.first() == Some(&'#') && l.iter().find(|c| **c != '#') == Some(&' ');
        }
        let mut lines = vec![];
        let mut cursor_at = None;
        for (screen_row, (r, s, e)) in rows.iter().enumerate().skip(self.scroll).take(h) {
            let base = if heading_of[*r] {
                Style::new().fg(pal.amber_hi).add_modifier(Modifier::BOLD)
            } else {
                Style::new().fg(pal.amber)
            };
            let line_bg = if *r == self.row && focused { pal.panel } else { pal.bg };
            let mut spans: Vec<Span> = vec![];
            let mut x = 0u16;
            for i in *s..*e {
                let ch = self.lines[*r][i];
                let mut style = base.bg(line_bg);
                if in_sel(*r, i) {
                    style = style.bg(pal.amber_faint);
                }
                if focused && *r == self.row && i == self.col {
                    cursor_at = Some((x, (screen_row - self.scroll) as u16));
                    if self.mode != Mode::Insert {
                        style = Style::new().fg(pal.bg).bg(pal.amber_hi);
                    }
                }
                spans.push(Span::styled(ch.to_string(), style));
                x += ch.width().unwrap_or(0) as u16;
            }
            if focused && *r == self.row && self.col >= *e && *e == self.line_len(*r) && cursor_at.is_none() {
                cursor_at = Some((x, (screen_row - self.scroll) as u16));
                if self.mode != Mode::Insert {
                    spans.push(Span::styled(" ", Style::new().bg(pal.amber_hi)));
                }
            }
            lines.push(Line::from(spans).style(Style::new().bg(line_bg)));
        }
        frame.render_widget(Paragraph::new(lines).style(Style::new().bg(pal.bg)), area);
        if let (Some((x, y)), Mode::Insert, true) = (cursor_at, self.mode, focused) {
            frame.set_cursor_position(Position::new(area.x + x.min(area.width - 1), area.y + y));
        }
    }
}
