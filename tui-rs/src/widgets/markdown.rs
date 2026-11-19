//! Markdown to terminal lines, in the MABEL-OS colours: headings, emphasis, code, lists, quotes,
//! rules, tables and links. Used for her replies in COMMS (and later VAULT and INBOX).

use pulldown_cmark::{Alignment, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

use super::wrap::{self, width};
use crate::palette::Palette;

/// Render `text` to lines no wider than `max` cells. `base` is the body style.
pub fn render(text: &str, max: u16, pal: &Palette, base: Style) -> Vec<Line<'static>> {
    let mut r = Renderer::new(pal, base, max.max(8) as usize);
    let opts = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH | Options::ENABLE_TASKLISTS;
    for event in Parser::new_ext(text, opts) {
        r.event(event);
    }
    r.flush();
    while r.out.last().is_some_and(is_blank) {
        r.out.pop();
    }
    r.out
}

/// A separator line: nothing but spaces and quote bars.
fn is_blank(line: &Line) -> bool {
    line.spans.iter().all(|s| s.content.chars().all(|c| c == ' ' || c == '▌'))
}

struct ListState {
    next: Option<u64>, // Some(n) for ordered lists
}

struct Renderer<'p> {
    pal: &'p Palette,
    base: Style,
    max: usize,
    out: Vec<Line<'static>>,
    /// The paragraph (or heading, or item) being collected.
    spans: Vec<Span<'static>>,
    styles: Vec<Style>,
    lists: Vec<ListState>,
    /// A new list item's bullet, waiting for its first text.
    bullet: Option<String>,
    quote: usize,
    code: Option<String>,
    link: Option<String>,
    table: Option<Table>,
}

#[derive(Default)]
struct Table {
    align: Vec<Alignment>,
    rows: Vec<Vec<Vec<Span<'static>>>>,
    head_rows: usize,
}

impl<'p> Renderer<'p> {
    fn new(pal: &'p Palette, base: Style, max: usize) -> Self {
        Renderer {
            pal,
            base,
            max,
            out: vec![],
            spans: vec![],
            styles: vec![base],
            lists: vec![],
            bullet: None,
            quote: 0,
            code: None,
            link: None,
            table: None,
        }
    }

    fn style(&self) -> Style {
        *self.styles.last().unwrap_or(&self.base)
    }

    fn push_style(&mut self, f: impl FnOnce(Style) -> Style) {
        let s = f(self.style());
        self.styles.push(s);
    }

    fn pop_style(&mut self) {
        if self.styles.len() > 1 {
            self.styles.pop();
        }
    }

    /// Indent for the current nesting: quote bars, then list indents.
    fn prefixes(&mut self) -> (Vec<Span<'static>>, Vec<Span<'static>>) {
        let bar = Span::styled("▌ ", Style::new().fg(self.pal.amber_dim));
        let mut first: Vec<Span<'static>> = (0..self.quote).map(|_| bar.clone()).collect();
        let mut rest = first.clone();
        let depth = self.lists.len();
        if depth > 0 {
            let indent = "  ".repeat(depth - 1);
            match self.bullet.take() {
                Some(b) => {
                    let pad = " ".repeat(width(&b));
                    first.push(Span::styled(format!("{indent}{b}"), Style::new().fg(self.pal.amber_dim)));
                    rest.push(Span::raw(format!("{indent}{pad}")));
                }
                None => {
                    let pad = format!("{indent}  ");
                    first.push(Span::raw(pad.clone()));
                    rest.push(Span::raw(pad));
                }
            }
        }
        (first, rest)
    }

    fn flush(&mut self) {
        if self.spans.is_empty() && self.bullet.is_none() {
            return;
        }
        let spans = std::mem::take(&mut self.spans);
        let (first, rest) = self.prefixes();
        self.out.extend(wrap::wrap(&spans, self.max, &first, &rest));
    }

    fn blank(&mut self) {
        if self.out.last().is_some_and(|l| !is_blank(l)) {
            let (_, rest) = self.prefixes_peek();
            self.out.push(Line::from(rest));
        }
    }

    /// Prefix for an empty separator line, without consuming a waiting bullet.
    fn prefixes_peek(&self) -> (Vec<Span<'static>>, Vec<Span<'static>>) {
        let bar = Span::styled("▌", Style::new().fg(self.pal.amber_dim));
        let rest: Vec<Span<'static>> = (0..self.quote).map(|_| bar.clone()).collect();
        (rest.clone(), rest)
    }

    fn text(&mut self, s: &str) {
        if let Some(code) = &mut self.code {
            code.push_str(s);
            return;
        }
        let span = Span::styled(s.to_string(), self.style());
        match &mut self.table {
            Some(t) => {
                if let Some(cell) = t.rows.last_mut().and_then(|r| r.last_mut()) {
                    cell.push(span);
                }
            }
            None => self.spans.push(span),
        }
    }

    fn event(&mut self, event: Event) {
        let pal = self.pal;
        match event {
            Event::Start(tag) => match tag {
                Tag::Paragraph => {}
                Tag::Heading { level, .. } => {
                    self.flush();
                    self.blank();
                    let marker = match level {
                        HeadingLevel::H1 => "▙ ",
                        HeadingLevel::H2 => "▚ ",
                        _ => "",
                    };
                    self.push_style(|_| Style::new().fg(pal.amber_hi).add_modifier(Modifier::BOLD));
                    if !marker.is_empty() {
                        self.spans.push(Span::styled(marker, Style::new().fg(pal.amber_dim)));
                    }
                }
                Tag::BlockQuote(_) => {
                    self.flush();
                    self.quote += 1;
                }
                Tag::CodeBlock(_) => {
                    self.flush();
                    self.blank();
                    self.code = Some(String::new());
                }
                Tag::List(start) => {
                    self.flush();
                    if self.lists.is_empty() {
                        self.blank();
                    }
                    self.lists.push(ListState { next: start });
                }
                Tag::Item => {
                    self.flush();
                    let b = match self.lists.last_mut() {
                        Some(ListState { next: Some(n) }) => {
                            let b = format!("{n}. ");
                            *n += 1;
                            b
                        }
                        _ => "• ".to_string(),
                    };
                    self.bullet = Some(b);
                }
                Tag::Emphasis => self.push_style(|s| s.add_modifier(Modifier::ITALIC)),
                Tag::Strong => self.push_style(|s| s.add_modifier(Modifier::BOLD)),
                Tag::Strikethrough => self.push_style(|s| s.add_modifier(Modifier::CROSSED_OUT)),
                Tag::Link { dest_url, .. } => {
                    self.link = Some(dest_url.to_string());
                    self.push_style(|s| s.fg(pal.amber_hi).add_modifier(Modifier::UNDERLINED));
                }
                Tag::Image { dest_url, .. } => {
                    self.link = Some(dest_url.to_string());
                    self.spans.push(Span::styled("▣ ", Style::new().fg(pal.amber_dim)));
                    self.push_style(|s| s.fg(pal.amber_dim));
                }
                Tag::Table(align) => {
                    self.flush();
                    self.blank();
                    self.table = Some(Table { align, ..Default::default() });
                }
                Tag::TableHead => {
                    if let Some(t) = &mut self.table {
                        t.rows.push(vec![]);
                    }
                    self.push_style(|s| s.fg(pal.beige).add_modifier(Modifier::BOLD));
                }
                Tag::TableRow => {
                    if let Some(t) = &mut self.table {
                        t.rows.push(vec![]);
                    }
                }
                Tag::TableCell => {
                    if let Some(row) = self.table.as_mut().and_then(|t| t.rows.last_mut()) {
                        row.push(vec![]);
                    }
                }
                _ => {}
            },
            Event::End(tag) => match tag {
                TagEnd::Paragraph => {
                    self.flush();
                    if self.lists.is_empty() {
                        self.blank();
                    }
                }
                TagEnd::Heading(_) => {
                    self.flush();
                    self.pop_style();
                    self.blank();
                }
                TagEnd::BlockQuote(_) => {
                    self.flush();
                    self.quote = self.quote.saturating_sub(1);
                    self.blank();
                }
                TagEnd::CodeBlock => {
                    let code = self.code.take().unwrap_or_default();
                    self.code_block(&code);
                    self.blank();
                }
                TagEnd::List(_) => {
                    self.flush();
                    self.lists.pop();
                    if self.lists.is_empty() {
                        self.blank();
                    }
                }
                TagEnd::Item => self.flush(),
                TagEnd::Emphasis | TagEnd::Strong | TagEnd::Strikethrough => self.pop_style(),
                TagEnd::Link => {
                    self.pop_style();
                    self.link = None;
                }
                TagEnd::Image => {
                    self.pop_style();
                    self.link = None;
                }
                TagEnd::TableHead => {
                    self.pop_style();
                    if let Some(t) = &mut self.table {
                        t.head_rows = t.rows.len();
                    }
                }
                TagEnd::Table => {
                    if let Some(t) = self.table.take() {
                        self.table_lines(t);
                    }
                    self.blank();
                }
                _ => {}
            },
            Event::Text(t) => self.text(&t),
            Event::Code(c) => {
                let span = Span::styled(c.to_string(), Style::new().fg(pal.amber_hi).bg(pal.amber_faint));
                match &mut self.table {
                    Some(t) => {
                        if let Some(cell) = t.rows.last_mut().and_then(|r| r.last_mut()) {
                            cell.push(span);
                        }
                    }
                    None => self.spans.push(span),
                }
            }
            Event::SoftBreak => self.text(" "),
            Event::HardBreak => self.flush(),
            Event::Rule => {
                self.flush();
                let w = self.max.min(40);
                self.out.push(Line::styled("─".repeat(w), Style::new().fg(pal.amber_dim)));
                self.blank();
            }
            Event::TaskListMarker(done) => {
                self.bullet = Some(if done { "☑ " } else { "☐ " }.to_string());
            }
            Event::Html(h) | Event::InlineHtml(h) => self.text(&h),
            Event::FootnoteReference(f) => self.text(&format!("[{f}]")),
            _ => {}
        }
    }

    fn code_block(&mut self, code: &str) {
        let style = Style::new().fg(self.pal.amber_hi).bg(self.pal.amber_faint);
        let (_, rest) = self.prefixes_peek();
        let inner = self.max.saturating_sub(width_of(&rest) + 2).max(4);
        for raw in code.trim_end_matches('\n').split('\n') {
            let raw = raw.replace('\t', "    ");
            // Long code lines are cut into pieces rather than word-wrapped.
            let mut piece = String::new();
            let mut pieces = vec![];
            for c in raw.chars() {
                if width(&piece) + width(&c.to_string()) > inner {
                    pieces.push(std::mem::take(&mut piece));
                }
                piece.push(c);
            }
            pieces.push(piece);
            for p in pieces {
                let pad = inner.saturating_sub(width(&p));
                let mut line = rest.clone();
                line.push(Span::styled(format!(" {p}{} ", " ".repeat(pad)), style));
                self.out.push(Line::from(line));
            }
        }
    }

    fn table_lines(&mut self, t: Table) {
        let pal = self.pal;
        let cols = t.rows.iter().map(|r| r.len()).max().unwrap_or(0);
        if cols == 0 {
            return;
        }
        let cell_w = |c: &Vec<Span>| c.iter().map(|s| width(&s.content)).sum::<usize>();
        let mut widths = vec![0usize; cols];
        for row in &t.rows {
            for (i, cell) in row.iter().enumerate() {
                widths[i] = widths[i].max(cell_w(cell));
            }
        }
        // Too wide: shrink the widest columns until it fits (cells are cut, not wrapped).
        let total = |w: &Vec<usize>| w.iter().sum::<usize>() + 3 * (w.len().saturating_sub(1));
        while total(&widths) > self.max {
            let (i, w) = widths.iter().enumerate().max_by_key(|(_, w)| **w).map(|(i, w)| (i, *w)).unwrap();
            if w <= 3 {
                break;
            }
            widths[i] = w - 1;
        }
        let sep = Span::styled(" │ ", Style::new().fg(pal.amber_dim));
        for (r, row) in t.rows.iter().enumerate() {
            let mut line: Vec<Span<'static>> = vec![];
            for (i, w) in widths.iter().enumerate() {
                if i > 0 {
                    line.push(sep.clone());
                }
                let cell = row.get(i).cloned().unwrap_or_default();
                let mut used = 0;
                let mut spans = vec![];
                for s in cell {
                    let mut content = String::new();
                    for c in s.content.chars() {
                        let cw = width(&c.to_string());
                        if used + cw > *w {
                            break;
                        }
                        used += cw;
                        content.push(c);
                    }
                    spans.push(Span::styled(content, s.style));
                }
                let pad = " ".repeat(w - used);
                match t.align.get(i) {
                    Some(Alignment::Right) => {
                        line.push(Span::raw(pad));
                        line.extend(spans);
                    }
                    _ => {
                        line.extend(spans);
                        line.push(Span::raw(pad));
                    }
                }
            }
            self.out.push(Line::from(line));
            if r + 1 == t.head_rows {
                let rule: Vec<String> = widths.iter().map(|w| "─".repeat(*w)).collect();
                self.out.push(Line::styled(rule.join("─┼─"), Style::new().fg(pal.amber_dim)));
            }
        }
    }
}

fn width_of(spans: &[Span]) -> usize {
    spans.iter().map(|s| width(&s.content)).sum()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plain(md: &str, w: u16) -> Vec<String> {
        let pal = Palette::base();
        render(md, w, &pal, Style::new())
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect::<String>().trim_end().to_string())
            .collect()
    }

    #[test]
    fn paragraphs_lists_and_quotes() {
        let out = plain("Hej **där**.\n\n- ett\n- två tre fyra fem\n\n> citat", 14);
        assert_eq!(out, vec!["Hej där.", "", "• ett", "• två tre fyra", "  fem", "", "▌ citat"]);
    }

    #[test]
    fn code_and_ordered_lists() {
        let out = plain("1. a\n2. b\n\n```\nfn x() {}\n```", 20);
        assert_eq!(out[0], "1. a");
        assert_eq!(out[1], "2. b");
        assert!(out.iter().any(|l| l.contains("fn x() {}")));
    }

    #[test]
    fn tables_fit() {
        let out = plain("| a | b |\n|---|--:|\n| one | 2 |", 30);
        assert_eq!(out[0], "a   │ b");
        assert!(out[1].contains("┼"));
        assert_eq!(out[2], "one │ 2");
    }
}
