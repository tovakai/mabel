//! Moving the cursor: word jumps, every motion (h j k l, w b e, 0 ^ $, gg G…), and counts.

use super::*;

impl Vim {
    // ── motions ──────────────────────────────────────────────────────────

    /// Where w, b or e lands from (row, col), crossing lines.
    pub(super) fn word(&self, key: char, row: usize, col: usize) -> Pos {
        let line = &self.lines[row];
        let spans = word_spans(line);
        let last = self.lines.len() - 1;
        match key {
            'w' => spans.iter().find(|(s, _)| *s > col).map(|(s, _)| (row, *s)).unwrap_or(if row < last {
                (row + 1, 0)
            } else {
                (row, line.len().saturating_sub(1))
            }),
            'e' => spans.iter().find(|(_, e)| e - 1 > col).map(|(_, e)| (row, e - 1)).unwrap_or(if row < last {
                (row + 1, 0)
            } else {
                (row, col)
            }),
            _ => spans.iter().rev().find(|(s, _)| *s < col).map(|(s, _)| (row, *s)).unwrap_or(if row > 0 {
                (row - 1, self.line_len(row - 1).saturating_sub(1))
            } else {
                (row, 0)
            }),
        }
    }

    /// Where a motion key goes from `from` (None: not a motion).
    pub(super) fn motion(&self, key: &KeyEvent, count: usize, from: Pos) -> Option<Pos> {
        let (row, col) = from;
        let last = self.lines.len() - 1;
        let fit = |r: usize, c: usize| (r, c.min(self.line_len(r).saturating_sub(1)));
        let c = keys::ch(key);
        Some(match (c, key.code) {
            (Some('h'), _) | (_, KeyCode::Left) => (row, col.saturating_sub(count)),
            (Some('l'), _) | (_, KeyCode::Right) => fit(row, col + count),
            (Some('j'), _) | (_, KeyCode::Down) => fit((row + count).min(last), col),
            (Some('k'), _) | (_, KeyCode::Up) => fit(row.saturating_sub(count), col),
            (Some('0'), _) | (_, KeyCode::Home) => (row, 0),
            (Some('^'), _) => (row, self.lines[row].iter().take_while(|c| c.is_whitespace()).count()),
            (Some('$'), _) | (_, KeyCode::End) => (row, self.line_len(row).saturating_sub(1)),
            (Some('G'), _) => ((if count > 1 { count - 1 } else { last }).min(last), 0),
            (Some(k @ ('w' | 'b' | 'e')), _) => {
                let mut p = from;
                for _ in 0..count {
                    p = self.word(k, p.0, p.1);
                }
                p
            }
            _ => return None,
        })
    }

    /// The count typed before a command ("3" in "3dd"; 1 if none) and the rest of the keys.
    pub(super) fn count(&self) -> (usize, String) {
        let digits: String = self.pending.chars().take_while(|c| c.is_ascii_digit()).collect();
        let rest = self.pending[digits.len()..].to_string();
        (digits.parse().unwrap_or(1).max(1), rest)
    }
}
