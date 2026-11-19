//! INVENTORY's map: a zone's grid of spots drawn in box characters, with the selected spot lit.

use super::*;

impl Inventory {
    // ── the map ──────────────────────────────────────────────────────────

    /// A Gridfinity grid / pegboard / compartment box as box-drawing, bins at their footprints.
    pub(super) fn draw_grid(&self, zone: &Zone, width: usize, pal: &Palette) -> Vec<Line<'static>> {
        let Zone::Grid { cols, rows, blocks, subs, compartments, .. } = zone else { return vec![] };
        let (c_n, r_n) = (*cols as usize, *rows as usize);
        let cw = ((width.saturating_sub(6)) / c_n.max(1)).clamp(5, 14);
        let chh = if cw >= 8 { 3 } else { 2 };
        let (w, h) = (c_n * cw + 1, r_n * chh + 1);
        let mut chars = vec![vec![' '; w]; h];
        let mut style = vec![vec![Style::new().fg(pal.amber_faint); w]; h];
        let put = |chars: &mut Vec<Vec<char>>, style: &mut Vec<Vec<Style>>, x: usize, y: usize, ch: char, st: Style| {
            if x < w && y < h {
                chars[y][x] = ch;
                style[y][x] = st;
            }
        };
        for r in 0..r_n {
            for c in 0..c_n {
                put(&mut chars, &mut style, c * cw + cw / 2, r * chh + chh / 2, '·', Style::new().fg(pal.amber_faint));
            }
        }
        let draw_box = |chars: &mut Vec<Vec<char>>,
                        style: &mut Vec<Vec<Style>>,
                        c: i64,
                        r: i64,
                        bw: i64,
                        bh: i64,
                        label: &str,
                        colour: Color,
                        dashed: bool,
                        extra: &str| {
            let (x0, y0) = ((c - 1).max(0) as usize * cw, (r - 1).max(0) as usize * chh);
            let (x1, y1) = (((c - 1 + bw).max(1)) as usize * cw, ((r - 1 + bh).max(1)) as usize * chh);
            let (hz, vt) = if dashed { ('╌', '╎') } else { ('─', '│') };
            let st = Style::new().fg(colour);
            for x in x0 + 1..x1 {
                put(chars, style, x, y0, hz, st);
                put(chars, style, x, y1, hz, st);
            }
            for y in y0 + 1..y1 {
                put(chars, style, x0, y, vt, st);
                put(chars, style, x1, y, vt, st);
                for x in x0 + 1..x1 {
                    put(chars, style, x, y, ' ', st);
                }
            }
            for (x, y, ch) in [(x0, y0, '┌'), (x1, y0, '┐'), (x0, y1, '└'), (x1, y1, '┘')] {
                put(chars, style, x, y, ch, st);
            }
            let inner_w = (x1 - x0).saturating_sub(1).max(1);
            let inner_h = (y1 - y0).saturating_sub(1).max(1);
            let mut lines: Vec<String> = vec![];
            let mut line = String::new();
            for word in label.split_whitespace() {
                if line.chars().count() + word.chars().count() + usize::from(!line.is_empty()) <= inner_w {
                    if !line.is_empty() {
                        line.push(' ');
                    }
                    line.push_str(word);
                } else {
                    if !line.is_empty() {
                        lines.push(std::mem::take(&mut line));
                    }
                    line = word.chars().take(inner_w).collect();
                }
            }
            if !line.is_empty() {
                lines.push(line);
            }
            lines.truncate(inner_h);
            let extra_n = extra.chars().count();
            if extra_n > 0
                && lines.len() == inner_h
                && let Some(last) = lines.last_mut()
                && last.chars().count() + extra_n + 1 > inner_w
            {
                *last = last.chars().take(inner_w.saturating_sub(extra_n + 1)).collect();
            }
            for (i, text) in lines.iter().enumerate() {
                for (j, ch) in text.chars().enumerate() {
                    put(chars, style, x0 + 1 + j, y0 + 1 + i, ch, st);
                }
            }
            for (j, ch) in extra.chars().enumerate() {
                put(
                    chars,
                    style,
                    (x1 + j).saturating_sub(extra_n),
                    y1.saturating_sub(1),
                    ch,
                    Style::new().fg(pal.teal),
                );
            }
        };
        for b in blocks {
            let colour = if b.item.status() == "away" { pal.red } else { hue(b.item.category.as_deref(), pal) };
            if *compartments {
                draw_box(&mut chars, &mut style, b.c, b.r, 1, 1, &b.item.name, colour, false, "");
            } else {
                let extra = if b.kids > 0 { format!("+{}", b.kids) } else { String::new() };
                draw_box(&mut chars, &mut style, b.c, b.r, b.w, b.h, &b.item.name, colour, b.assumed, &extra);
            }
        }
        for s in subs {
            draw_box(
                &mut chars,
                &mut style,
                s.c,
                s.r,
                1,
                1,
                &format!("▦ {}", s.layout.label.clone().unwrap_or_default()),
                pal.teal,
                false,
                "",
            );
        }
        if let Some(sel) = &self.selected
            && let Some(b) = blocks.iter().find(|b| &b.item.id == sel)
        {
            let (x0, y0) = ((b.c - 1).max(0) as usize * cw, (b.r - 1).max(0) as usize * chh);
            let (x1, y1) = ((b.c - 1 + b.w) as usize * cw, (b.r - 1 + b.h) as usize * chh);
            for row in style.iter_mut().take((y1 + 1).min(h)).skip(y0) {
                for st in row.iter_mut().take((x1 + 1).min(w)).skip(x0) {
                    *st = st.add_modifier(Modifier::REVERSED | Modifier::BOLD);
                }
            }
        }
        let mut out = vec![];
        let header: String = (1..=c_n).map(|c| format!("{:<cw$}", format!("C{c:02}"))).collect();
        out.push(Line::styled(
            format!("    {}", header.chars().take(w).collect::<String>()),
            Style::new().fg(pal.beige_dark),
        ));
        for y in 0..h {
            let label =
                if y % chh == chh / 2 && y / chh < r_n { format!("R{:02} ", y / chh + 1) } else { "    ".into() };
            let mut spans = vec![Span::styled(label, Style::new().fg(pal.beige_dark))];
            let mut run = String::new();
            let mut run_style = style[y][0];
            for x in 0..w {
                if style[y][x] != run_style && !run.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut run), run_style));
                }
                run_style = style[y][x];
                run.push(chars[y][x]);
            }
            spans.push(Span::styled(run, run_style));
            out.push(Line::from(spans));
        }
        out
    }
}
