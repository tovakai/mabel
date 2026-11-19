//! Word wrap for styled text, with a prefix on the first line and another on the rest (list
//! bullets, quote bars, indents). Widths are terminal cells, so CJK and emoji count double.

use ratatui::style::Style;
use ratatui::text::{Line, Span};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// How many terminal cells text takes (wide characters count 2).
pub fn width(s: &str) -> usize {
    UnicodeWidthStr::width(s)
}

/// Wrap `spans` to `max` cells. `first` starts the first line, `rest` the following ones.
pub fn wrap(
    spans: &[Span<'static>],
    max: usize,
    first: &[Span<'static>],
    rest: &[Span<'static>],
) -> Vec<Line<'static>> {
    let mut lines = vec![];
    let mut line: Vec<Span<'static>> = first.to_vec();
    let mut used: usize = first.iter().map(|s| width(&s.content)).sum();
    let mut line_has_text = false;
    let indent_of = |prefix: &[Span<'static>]| prefix.iter().map(|s| width(&s.content)).sum::<usize>();
    let rest_indent = indent_of(rest);

    // Words with the whitespace that follows them, each keeping its span's style.
    for (word, gap, style) in words(spans) {
        let w = width(&word);
        if line_has_text && used + w > max {
            lines.push(Line::from(std::mem::take(&mut line)));
            line = rest.to_vec();
            used = rest_indent;
        }
        if used + w > max && w > 0 {
            // A word wider than the line: cut it by characters.
            let mut chunk = String::new();
            for c in word.chars() {
                let cw = c.width().unwrap_or(0);
                if used + width(&chunk) + cw > max && !chunk.is_empty() {
                    line.push(Span::styled(std::mem::take(&mut chunk), style));
                    lines.push(Line::from(std::mem::take(&mut line)));
                    line = rest.to_vec();
                    used = rest_indent;
                }
                chunk.push(c);
            }
            used += width(&chunk);
            line.push(Span::styled(chunk, style));
        } else {
            used += w;
            line.push(Span::styled(word, style));
        }
        line_has_text = true;
        if !gap.is_empty() && used < max {
            used += width(&gap);
            line.push(Span::styled(gap, style));
        }
    }
    lines.push(Line::from(line));
    lines
}

/// Wrap whole lines (each keeps its spans and leading spaces) to `max` cells.
pub fn wrap_lines(lines: &[Line<'static>], max: usize) -> Vec<Line<'static>> {
    let mut out = vec![];
    for line in lines {
        if line.spans.is_empty() {
            out.push(Line::raw(""));
            continue;
        }
        let mut wrapped = wrap(&line.spans, max, &[], &[]);
        if let Some(first) = wrapped.first_mut() {
            first.style = line.style;
        }
        out.extend(wrapped);
    }
    out
}

/// Split styled text into (word, the space after it, style).
fn words(spans: &[Span<'static>]) -> Vec<(String, String, Style)> {
    let mut out = vec![];
    for span in spans {
        let mut word = String::new();
        let mut gap = String::new();
        for c in span.content.chars() {
            if c.is_whitespace() {
                gap.push(' ');
            } else {
                if !gap.is_empty() {
                    out.push((std::mem::take(&mut word), std::mem::take(&mut gap), span.style));
                }
                word.push(c);
            }
        }
        if !word.is_empty() || !gap.is_empty() {
            out.push((word, gap, span.style));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(lines: &[Line]) -> Vec<String> {
        lines
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect::<String>().trim_end().to_string())
            .collect()
    }

    #[test]
    fn wraps_with_prefixes_and_cuts_long_words() {
        let spans = vec![Span::raw("one two three four")];
        let out = wrap(&spans, 10, &[Span::raw("• ")], &[Span::raw("  ")]);
        assert_eq!(text(&out), vec!["• one two", "  three", "  four"]);
        let out = wrap(&[Span::raw("abcdefghijkl")], 5, &[], &[]);
        assert_eq!(text(&out), vec!["abcde", "fghij", "kl"]);
    }

    #[test]
    fn styles_survive_across_spans() {
        let spans = vec![Span::raw("hej "), Span::styled("då", Style::new().bold())];
        let out = wrap(&spans, 20, &[], &[]);
        assert_eq!(text(&out), vec!["hej då"]);
        assert!(out[0].spans.iter().any(|s| s.content == "då" && s.style == Style::new().bold()));
    }
}
