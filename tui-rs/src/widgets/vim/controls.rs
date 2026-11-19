//! The editor's keys in each mode (NORMAL, INSERT, VISUAL), operators (d, c, y, > <),
//! `.` repeat, the `:` line, and / search.

use super::*;

impl Vim {
    // ── keys ─────────────────────────────────────────────────────────────

    /// A key, handled by the mode the editor is in. Returns what the channel should do.
    pub fn key(&mut self, key: &KeyEvent) -> Event {
        if self.cmdline.is_some() {
            return self.cmdline_key(key);
        }
        let event = match self.mode {
            Mode::Insert => self.insert_key(key),
            Mode::Visual | Mode::VLine => {
                self.visual_key(key);
                Event::None
            }
            Mode::Normal => self.normal_key(key),
        };
        self.clamp();
        event
    }

    /// INSERT: typing, enter, backspace, arrows; esc back to NORMAL; [[ asks for a link.
    pub(super) fn insert_key(&mut self, key: &KeyEvent) -> Event {
        match (keys::ch(key), key.code) {
            (_, KeyCode::Esc) => {
                self.mode = Mode::Normal;
                self.col = self.col.saturating_sub(1);
            }
            (Some(c), _) => {
                let wants_link = c == '[' && self.col > 0 && self.lines[self.row].get(self.col - 1) == Some(&'[');
                self.insert(&c.to_string());
                if wants_link {
                    return Event::LinkWanted;
                }
            }
            (_, KeyCode::Enter) => {
                self.split_line();
                self.dirty = true;
            }
            (_, KeyCode::Tab) => self.insert("    "),
            (_, KeyCode::Backspace) => {
                if self.col > 0 {
                    self.col -= 1;
                    self.lines[self.row].remove(self.col);
                } else if self.row > 0 {
                    let line = self.lines.remove(self.row);
                    self.row -= 1;
                    self.col = self.line_len(self.row);
                    self.lines[self.row].extend(line);
                }
                self.dirty = true;
            }
            (_, KeyCode::Delete) => {
                if self.col < self.line_len(self.row) {
                    self.lines[self.row].remove(self.col);
                } else if self.row + 1 < self.lines.len() {
                    let next = self.lines.remove(self.row + 1);
                    self.lines[self.row].extend(next);
                }
                self.dirty = true;
            }
            (_, KeyCode::Left) => self.col = self.col.saturating_sub(1),
            (_, KeyCode::Right) => self.col = (self.col + 1).min(self.line_len(self.row)),
            (_, KeyCode::Up) => self.row = self.row.saturating_sub(1),
            (_, KeyCode::Down) => self.row = (self.row + 1).min(self.lines.len() - 1),
            (_, KeyCode::Home) => self.col = 0,
            (_, KeyCode::End) => self.col = self.line_len(self.row),
            _ => {}
        }
        Event::None
    }

    /// Go into INSERT (one undo step for everything typed until esc).
    pub(super) fn start_insert(&mut self) {
        self.snapshot(); // the whole insert is one undo step
        self.mode = Mode::Insert;
    }

    /// NORMAL: counts, motions, x, p, o, J, u, `.`, operators waiting for their motion, `:` and /.
    pub(super) fn normal_key(&mut self, key: &KeyEvent) -> Event {
        let c = keys::ch(key);
        if let Some(d) = c.filter(|d| d.is_ascii_digit())
            && (d != '0' || self.pending.chars().next().is_some_and(|p| p.is_ascii_digit()))
        {
            self.pending.push(d);
            return Event::None;
        }
        let (count, op) = self.count();
        let (row, col) = (self.row, self.col);
        if matches!(op.as_str(), "d" | "c" | "y" | ">" | "<") {
            self.pending.clear();
            self.operator(op.chars().next().expect("one"), key, count);
            return Event::None;
        }
        if op == "g" {
            self.pending.clear();
            if c == Some('g') {
                (self.row, self.col) = (0, 0);
            }
            return Event::None;
        }
        if let Some(p @ ('d' | 'c' | 'y' | 'g' | '>' | '<')) = c {
            self.pending.push(p);
            return Event::None;
        }
        self.pending.clear();
        if let Some(target) = self.motion(key, count, (row, col)) {
            (self.row, self.col) = target;
            return Event::None;
        }
        if keys::ctrl(key, 'r') {
            self.restore(false);
            return Event::None;
        }
        match (c, key.code) {
            (Some('i'), _) => self.start_insert(),
            (Some('a'), _) => {
                self.start_insert();
                self.col = (col + 1).min(self.line_len(row));
            }
            (Some('I'), _) => {
                self.start_insert();
                self.col = self.lines[row].iter().take_while(|c| c.is_whitespace()).count();
            }
            (Some('A'), _) => {
                self.start_insert();
                self.col = self.line_len(row);
            }
            (Some('o'), _) => {
                self.start_insert();
                self.lines.insert(row + 1, vec![]);
                (self.row, self.col) = (row + 1, 0);
            }
            (Some('O'), _) => {
                self.start_insert();
                self.lines.insert(row, vec![]);
                (self.row, self.col) = (row, 0);
            }
            (Some('x'), _) if !self.lines[row].is_empty() => {
                self.snapshot();
                let end = (col + count).min(self.line_len(row));
                self.yank(self.text_range((row, col), (row, end)), false);
                self.delete_range((row, col), (row, end));
                self.last_change = Some(Change::X(count));
            }
            (Some('D'), _) => {
                self.snapshot();
                let end = self.line_len(row);
                self.yank(self.text_range((row, col), (row, end)), false);
                self.delete_range((row, col), (row, end));
            }
            (Some('C'), _) => {
                self.start_insert();
                let end = self.line_len(row);
                self.delete_range((row, col), (row, end));
            }
            (Some('J'), _) => {
                self.snapshot();
                for _ in 0..count {
                    if self.row + 1 < self.lines.len() {
                        let next: Vec<char> = self.lines.remove(self.row + 1);
                        let next: Vec<char> = next.into_iter().skip_while(|c| c.is_whitespace()).collect();
                        self.col = self.line_len(self.row);
                        if !next.is_empty() {
                            self.lines[self.row].push(' ');
                            self.lines[self.row].extend(next);
                        }
                    }
                }
            }
            (Some('p'), _) => self.put(true),
            (Some('P'), _) => self.put(false),
            (Some('u'), _) => self.restore(true),
            (Some('v'), _) => {
                self.anchor = Some((row, col));
                self.mode = Mode::Visual;
            }
            (Some('V'), _) => {
                self.anchor = Some((row, col));
                self.mode = Mode::VLine;
            }
            (Some('.'), _) => self.repeat(),
            (Some(':'), _) => self.cmdline = Some(":".into()),
            (Some('/'), _) => self.cmdline = Some("/".into()),
            (Some('n'), _) if !self.last_search.is_empty() => {
                let needle = self.last_search.clone();
                return self.find(&needle);
            }
            (_, KeyCode::Esc) => return Event::Leave,
            _ => {}
        }
        Event::None
    }

    /// d, c or y with a motion (`dw`, `c$`, `yy`…), `count` times.
    pub(super) fn operator(&mut self, op: char, key: &KeyEvent, count: usize) {
        let (row, col) = (self.row, self.col);
        let c = keys::ch(key);
        if c == Some(op) {
            // dd, cc, yy, >>, <<: whole lines.
            let last = (row + count - 1).min(self.lines.len() - 1);
            if op == '>' || op == '<' {
                self.snapshot();
                self.indent(row, last, op == '>');
                return;
            }
            let text: String = (row..=last).map(|r| self.lines[r].iter().collect::<String>() + "\n").collect();
            self.yank(text, true);
            if op == 'y' {
                return;
            }
            self.snapshot();
            if op == 'c' {
                self.lines.drain(row + 1..=last);
                self.lines[row].clear();
                (self.row, self.col) = (row, 0);
                self.mode = Mode::Insert;
                return;
            }
            self.lines.drain(row..=last);
            if self.lines.is_empty() {
                self.lines.push(vec![]);
            }
            (self.row, self.col) = (row.min(self.lines.len() - 1), 0);
            self.last_change = Some(Change::Dd(count));
            return;
        }
        // cw acts like ce (vim's quirk), inclusive.
        let as_e = op == 'c' && c == Some('w');
        let motion_key = if as_e { KeyEvent::new(KeyCode::Char('e'), KeyModifiers::NONE) } else { *key };
        let Some(mut target) = self.motion(&motion_key, count, (row, col)) else { return };
        let mkey = keys::ch(&motion_key);
        if mkey == Some('w') && target.0 > row {
            target = (row, self.line_len(row)); // like vim: dw on the last word stops at the line end
        }
        let (mut start, mut end) = if (row, col) <= target { ((row, col), target) } else { (target, (row, col)) };
        if matches!(mkey, Some('e' | '$' | 'l')) {
            end.1 += 1;
        }
        if start.0 != end.0 && matches!(mkey, Some('j' | 'k')) {
            start.1 = 0; // j/k with an operator: whole lines
            end = (end.0, self.line_len(end.0));
        }
        let text = self.text_range(start, end);
        self.yank(text, false);
        if op == 'y' {
            return;
        }
        self.snapshot();
        self.delete_range(start, end);
        if op == 'c' {
            self.mode = Mode::Insert;
        } else if let Some(k) = mkey {
            self.last_change = Some(Change::D(k, count));
        }
    }

    /// `.`: do the last change again.
    pub(super) fn repeat(&mut self) {
        match self.last_change.clone() {
            Some(Change::X(n)) => {
                self.snapshot();
                let end = (self.col + n).min(self.line_len(self.row));
                self.delete_range((self.row, self.col), (self.row, end));
            }
            Some(Change::Dd(n)) => self.operator('d', &KeyEvent::new(KeyCode::Char('d'), KeyModifiers::NONE), n),
            Some(Change::D(k, n)) => self.operator('d', &KeyEvent::new(KeyCode::Char(k), KeyModifiers::NONE), n),
            None => {}
        }
    }

    /// The VISUAL selection as (start, end), in order.
    pub(super) fn selection(&self) -> Option<(Pos, Pos)> {
        let anchor = self.anchor?;
        let cur = (self.row, self.col);
        let (a, b) = if anchor <= cur { (anchor, cur) } else { (cur, anchor) };
        Some(match self.mode {
            Mode::VLine => ((a.0, 0), (b.0, self.line_len(b.0))),
            _ => (a, (b.0, b.1 + 1)),
        })
    }

    /// VISUAL / V-LINE: motions grow the selection; d x y c > < act on it; esc ends it.
    pub(super) fn visual_key(&mut self, key: &KeyEvent) {
        let c = keys::ch(key);
        let leave = key.code == KeyCode::Esc
            || (c == Some('v') && self.mode == Mode::Visual)
            || (c == Some('V') && self.mode == Mode::VLine);
        if leave {
            self.mode = Mode::Normal;
            self.anchor = None;
            return;
        }
        match c {
            Some('g') => {
                (self.row, self.col) = (0, 0);
                return;
            }
            Some('G') => {
                (self.row, self.col) = (self.lines.len() - 1, 0);
                return;
            }
            _ => {}
        }
        if let Some(target) = self.motion(key, 1, (self.row, self.col)) {
            (self.row, self.col) = target;
            return;
        }
        let Some((start, end)) = self.selection() else { return };
        let linewise = self.mode == Mode::VLine;
        match c {
            Some('y') => {
                let text = self.text_range(start, end) + if linewise { "\n" } else { "" };
                self.yank(text, linewise);
                (self.row, self.col) = start;
            }
            Some(k @ ('d' | 'x' | 'c')) => {
                let text = self.text_range(start, end) + if linewise { "\n" } else { "" };
                self.yank(text, linewise);
                self.snapshot();
                let mut end = end;
                if linewise && k != 'c' && end.0 + 1 < self.lines.len() {
                    end = (end.0 + 1, 0);
                } else if linewise && k != 'c' && start.0 > 0 {
                    // the last lines of the note: take the newline before them
                    let prev = (start.0 - 1, self.line_len(start.0 - 1));
                    self.delete_range(prev, end);
                    self.row = (start.0).min(self.lines.len() - 1);
                    self.col = 0;
                    self.mode = Mode::Normal;
                    self.anchor = None;
                    return;
                }
                self.delete_range(start, end);
                self.mode = if k == 'c' { Mode::Insert } else { Mode::Normal };
                self.anchor = None;
                return;
            }
            Some(k @ ('>' | '<')) => {
                self.snapshot();
                self.indent(start.0, end.0, k == '>');
            }
            _ => return,
        }
        self.mode = Mode::Normal;
        self.anchor = None;
    }

    /// The `:` line: typing; enter hands the command to the channel (:w, :q, :wq…).
    pub(super) fn cmdline_key(&mut self, key: &KeyEvent) -> Event {
        let Some(line) = self.cmdline.as_mut() else { return Event::None };
        match (keys::ch(key), key.code) {
            (_, KeyCode::Esc) => self.cmdline = None,
            (_, KeyCode::Enter) => {
                let line = self.cmdline.take().unwrap_or_default();
                if let Some(needle) = line.strip_prefix('/') {
                    self.last_search = needle.to_string();
                    return self.find(needle);
                }
                return Event::Command(line.trim_start_matches(':').trim().to_string());
            }
            (_, KeyCode::Backspace) => {
                line.pop();
                if line.is_empty() {
                    self.cmdline = None;
                }
            }
            (Some(c), _) => line.push(c),
            _ => {}
        }
        Event::None
    }

    /// / search: the next match after the cursor (upper/lower case alike), wrapping round to the top.
    pub(super) fn find(&mut self, needle: &str) -> Event {
        if needle.is_empty() {
            return Event::None;
        }
        let low: Vec<char> = needle.to_lowercase().chars().collect();
        let n = self.lines.len();
        for step in 0..=n {
            let r = (self.row + step) % n;
            let line: Vec<char> = self.lines[r].iter().flat_map(|c| c.to_lowercase()).collect();
            let from = if step == 0 { self.col + 1 } else { 0 };
            if let Some(i) = (from..line.len()).find(|i| line[*i..].starts_with(&low)) {
                (self.row, self.col) = (r, i);
                return Event::None;
            }
        }
        Event::NotFound(needle.to_string())
    }
}
