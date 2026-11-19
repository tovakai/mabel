//! The editor's edits: undo snapshots, splitting and joining lines, deleting and copying
//! ranges, yank and put, and indenting.

use super::*;

impl Vim {
    // ── edits ────────────────────────────────────────────────────────────

    /// Remember the text before a change (for u; clears redo).
    pub(super) fn snapshot(&mut self) {
        self.undo.push((self.lines.clone(), (self.row, self.col)));
        if self.undo.len() > 200 {
            self.undo.remove(0);
        }
        self.redo.clear();
        self.dirty = true;
    }

    /// u (`from_undo`) or ctrl+r: go back to a remembered text.
    pub(super) fn restore(&mut self, from_undo: bool) {
        let (take, put) = if from_undo { (&mut self.undo, &mut self.redo) } else { (&mut self.redo, &mut self.undo) };
        if let Some((lines, (r, c))) = take.pop() {
            put.push((std::mem::replace(&mut self.lines, lines), (self.row, self.col)));
            self.row = r.min(self.lines.len() - 1);
            self.col = c;
            self.clamp();
            self.dirty = true;
        }
    }

    /// How many characters line `row` has.
    pub(super) fn line_len(&self, row: usize) -> usize {
        self.lines[row].len()
    }

    /// NORMAL keeps the cursor on a character (not past the end).
    pub(super) fn clamp(&mut self) {
        let max =
            if self.mode == Mode::Insert { self.line_len(self.row) } else { self.line_len(self.row).saturating_sub(1) };
        self.col = self.col.min(max);
    }

    /// enter in INSERT: break the line at the cursor.
    pub(super) fn split_line(&mut self) {
        let rest = self.lines[self.row].split_off(self.col);
        self.lines.insert(self.row + 1, rest);
        self.row += 1;
        self.col = 0;
    }

    /// The text between two places, in either order (with line breaks when it spans lines).
    pub(super) fn text_range(&self, a: Pos, b: Pos) -> String {
        let (start, end) = if a <= b { (a, b) } else { (b, a) };
        if start.0 == end.0 {
            let l = &self.lines[start.0];
            return l[start.1.min(l.len())..end.1.min(l.len())].iter().collect();
        }
        let mut out: String = self.lines[start.0][start.1.min(self.line_len(start.0))..].iter().collect();
        for line in &self.lines[start.0 + 1..end.0] {
            out.push('\n');
            out.extend(line);
        }
        out.push('\n');
        out.extend(&self.lines[end.0][..end.1.min(self.line_len(end.0))]);
        out
    }

    /// Delete [a, b) (b may be past a line's end: then the newline goes too).
    pub(super) fn delete_range(&mut self, a: Pos, b: Pos) {
        let (start, end) = if a <= b { (a, b) } else { (b, a) };
        let end_col = end.1.min(self.line_len(end.0));
        let tail: Vec<char> = self.lines[end.0][end_col..].to_vec();
        let start_col = start.1.min(self.line_len(start.0));
        self.lines[start.0].truncate(start_col);
        self.lines[start.0].extend(tail);
        if end.0 > start.0 {
            self.lines.drain(start.0 + 1..=end.0);
        }
        self.row = start.0;
        self.col = start_col;
    }

    /// Copy into the register, and to the system clipboard.
    pub(super) fn yank(&mut self, text: String, linewise: bool) {
        osc52(&text);
        self.register = (text, linewise);
    }

    /// p (`after`) or P: put the register after or before the cursor (whole lines below/above).
    pub(super) fn put(&mut self, after: bool) {
        let (text, linewise) = self.register.clone();
        if text.is_empty() {
            return;
        }
        self.snapshot();
        if linewise {
            let new: Vec<Vec<char>> = text.trim_end_matches('\n').split('\n').map(|l| l.chars().collect()).collect();
            let at = if after { self.row + 1 } else { self.row };
            let n = new.len();
            for (i, l) in new.into_iter().enumerate() {
                self.lines.insert(at + i, l);
            }
            self.row = at.min(self.lines.len() - 1);
            self.col = 0;
            let _ = n;
        } else {
            if after && !self.lines[self.row].is_empty() {
                self.col += 1;
            }
            let saved = self.mode;
            self.mode = Mode::Insert;
            self.insert(&text);
            self.mode = saved;
            self.col = self.col.saturating_sub(1);
        }
    }

    /// > or <: indent lines `from`..=`to` by four spaces, or take four away.
    pub(super) fn indent(&mut self, from: usize, to: usize, right: bool) {
        for r in from..=to.min(self.lines.len() - 1) {
            if right {
                for _ in 0..4 {
                    self.lines[r].insert(0, ' ');
                }
            } else {
                let lead = self.lines[r].iter().take(4).take_while(|c| **c == ' ').count();
                self.lines[r].drain(..lead);
            }
        }
    }
}
