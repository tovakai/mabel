//! A vim-flavoured editor for notes (he uses vim mode in Obsidian too).
//!
//! Modes: NORMAL (default), INSERT, VISUAL (v, characters) and V-LINE (V).
//! NORMAL: h j k l, w b e, 0 ^ $, gg G, counts (3j, 2dd), x, dd, dw, D, cc, cw, C, yy, yw, p P,
//! o O, i a I A, J (join), u, ctrl+r, ., :w :q :wq (the : line), / search, n next.
//! VISUAL: motions extend the selection; d/x delete, y yank, c change, > < indent.
//! Yanks go to an internal register and to the system clipboard (OSC 52, over SSH too).
//! Typing [[ in INSERT asks the channel for a note to link.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Position, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use unicode_width::UnicodeWidthChar;

use crate::keys;
use crate::palette::Palette;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    Normal,
    Insert,
    Visual,
    VLine,
}

/// What the editor asks of the channel after a key.
#[derive(Debug, PartialEq)]
pub enum Event {
    None,
    /// :w, :q, :wq, :x, :q!
    Command(String),
    /// esc in NORMAL: back to reading.
    Leave,
    /// [[ was typed: pick a note, then `insert` its name and ]].
    LinkWanted,
    /// A / search found nothing.
    NotFound(String),
}

#[derive(Clone)]
enum Change {
    X(usize),
    Dd(usize),
    D(char, usize),
}

type Pos = (usize, usize);

pub struct Vim {
    lines: Vec<Vec<char>>,
    row: usize,
    col: usize,
    pub mode: Mode,
    pending: String,
    register: (String, bool),
    anchor: Option<Pos>,
    last_change: Option<Change>,
    cmdline: Option<String>,
    last_search: String,
    undo: Vec<(Vec<Vec<char>>, Pos)>,
    redo: Vec<(Vec<Vec<char>>, Pos)>,
    /// First visual row on screen.
    scroll: usize,
    pub dirty: bool,
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Word spans of a line, like the regex \w+|[^\w\s]+.
fn word_spans(line: &[char]) -> Vec<(usize, usize)> {
    let mut spans = vec![];
    let mut i = 0;
    while i < line.len() {
        if line[i].is_whitespace() {
            i += 1;
            continue;
        }
        let word = is_word(line[i]);
        let start = i;
        while i < line.len() && !line[i].is_whitespace() && is_word(line[i]) == word {
            i += 1;
        }
        spans.push((start, i));
    }
    spans
}

/// Send text to the terminal's clipboard (OSC 52): works in kitty, foot, over SSH.
fn osc52(text: &str) {
    if cfg!(test) {
        return; // not into the test runner's output
    }
    const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in text.as_bytes().chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        for i in 0..4 {
            out.push(if i <= chunk.len() { B64[(n >> (18 - 6 * i) & 63) as usize] as char } else { '=' });
        }
    }
    use std::io::Write;
    let mut stdout = std::io::stdout();
    let _ = write!(stdout, "\x1b]52;c;{out}\x07");
    let _ = stdout.flush();
}

impl Vim {
    pub fn new(text: &str) -> Self {
        let mut lines: Vec<Vec<char>> = text.split('\n').map(|l| l.chars().collect()).collect();
        if lines.is_empty() {
            lines.push(vec![]);
        }
        Vim {
            lines,
            row: 0,
            col: 0,
            mode: Mode::Normal,
            pending: String::new(),
            register: (String::new(), false),
            anchor: None,
            last_change: None,
            cmdline: None,
            last_search: String::new(),
            undo: vec![],
            redo: vec![],
            scroll: 0,
            dirty: false,
        }
    }

    pub fn text(&self) -> String {
        self.lines.iter().map(|l| l.iter().collect::<String>()).collect::<Vec<_>>().join("\n")
    }

    /// "-- NORMAL --", the : line being typed, and keys pending.
    pub fn status(&self) -> String {
        if let Some(c) = &self.cmdline {
            return format!("{c}█");
        }
        let name = match self.mode {
            Mode::Normal => "NORMAL",
            Mode::Insert => "INSERT",
            Mode::Visual => "VISUAL",
            Mode::VLine => "V-LINE",
        };
        if self.pending.is_empty() { format!("-- {name} --") } else { format!("-- {name} --  {}", self.pending) }
    }

    pub fn goto_line(&mut self, row: usize) {
        self.row = row.min(self.lines.len() - 1);
        self.col = 0;
    }

    /// Text typed in from outside (the [[ picker's choice, a paste).
    pub fn insert(&mut self, text: &str) {
        for c in text.chars() {
            if c == '\n' {
                self.split_line();
            } else if c != '\r' {
                self.lines[self.row].insert(self.col, c);
                self.col += 1;
            }
        }
        self.dirty = true;
    }

    // ── edits ────────────────────────────────────────────────────────────

    fn snapshot(&mut self) {
        self.undo.push((self.lines.clone(), (self.row, self.col)));
        if self.undo.len() > 200 {
            self.undo.remove(0);
        }
        self.redo.clear();
        self.dirty = true;
    }

    fn restore(&mut self, from_undo: bool) {
        let (take, put) = if from_undo { (&mut self.undo, &mut self.redo) } else { (&mut self.redo, &mut self.undo) };
        if let Some((lines, (r, c))) = take.pop() {
            put.push((std::mem::replace(&mut self.lines, lines), (self.row, self.col)));
            self.row = r.min(self.lines.len() - 1);
            self.col = c;
            self.clamp();
            self.dirty = true;
        }
    }

    fn line_len(&self, row: usize) -> usize {
        self.lines[row].len()
    }

    /// NORMAL keeps the cursor on a character (not past the end).
    fn clamp(&mut self) {
        let max =
            if self.mode == Mode::Insert { self.line_len(self.row) } else { self.line_len(self.row).saturating_sub(1) };
        self.col = self.col.min(max);
    }

    fn split_line(&mut self) {
        let rest = self.lines[self.row].split_off(self.col);
        self.lines.insert(self.row + 1, rest);
        self.row += 1;
        self.col = 0;
    }

    fn text_range(&self, a: Pos, b: Pos) -> String {
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
    fn delete_range(&mut self, a: Pos, b: Pos) {
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

    fn yank(&mut self, text: String, linewise: bool) {
        osc52(&text);
        self.register = (text, linewise);
    }

    fn put(&mut self, after: bool) {
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

    fn indent(&mut self, from: usize, to: usize, right: bool) {
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

    // ── motions ──────────────────────────────────────────────────────────

    fn word(&self, key: char, row: usize, col: usize) -> Pos {
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
    fn motion(&self, key: &KeyEvent, count: usize, from: Pos) -> Option<Pos> {
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

    fn count(&self) -> (usize, String) {
        let digits: String = self.pending.chars().take_while(|c| c.is_ascii_digit()).collect();
        let rest = self.pending[digits.len()..].to_string();
        (digits.parse().unwrap_or(1).max(1), rest)
    }

    // ── keys ─────────────────────────────────────────────────────────────

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

    fn insert_key(&mut self, key: &KeyEvent) -> Event {
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

    fn start_insert(&mut self) {
        self.snapshot(); // the whole insert is one undo step
        self.mode = Mode::Insert;
    }

    fn normal_key(&mut self, key: &KeyEvent) -> Event {
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

    fn operator(&mut self, op: char, key: &KeyEvent, count: usize) {
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

    fn repeat(&mut self) {
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

    fn selection(&self) -> Option<(Pos, Pos)> {
        let anchor = self.anchor?;
        let cur = (self.row, self.col);
        let (a, b) = if anchor <= cur { (anchor, cur) } else { (cur, anchor) };
        Some(match self.mode {
            Mode::VLine => ((a.0, 0), (b.0, self.line_len(b.0))),
            _ => (a, (b.0, b.1 + 1)),
        })
    }

    fn visual_key(&mut self, key: &KeyEvent) {
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

    fn cmdline_key(&mut self, key: &KeyEvent) -> Event {
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

    fn find(&mut self, needle: &str) -> Event {
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

    // ── drawing ──────────────────────────────────────────────────────────

    /// Soft-wrapped: (line, start column, end column) for each row on screen.
    fn rows(&self, width: usize) -> Vec<(usize, usize, usize)> {
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

#[cfg(test)]
mod tests {
    use super::*;

    fn press(v: &mut Vim, keys: &str) -> Event {
        let mut last = Event::None;
        for c in keys.chars() {
            let code = match c {
                '⎋' => KeyCode::Esc,
                '⏎' => KeyCode::Enter,
                _ => KeyCode::Char(c),
            };
            last = v.key(&KeyEvent::new(code, KeyModifiers::NONE));
        }
        last
    }

    #[test]
    fn normal_editing() {
        let mut v = Vim::new("one two three\nfour\nfive");
        press(&mut v, "wdw");
        assert_eq!(v.text(), "one three\nfour\nfive");
        press(&mut v, "jdd");
        assert_eq!(v.text(), "one three\nfive");
        press(&mut v, "u");
        assert_eq!(v.text(), "one three\nfour\nfive");
        press(&mut v, "ggcwhej⎋");
        assert_eq!(v.text(), "hej three\nfour\nfive");
        press(&mut v, "yyGp");
        assert_eq!(v.text(), "hej three\nfour\nfive\nhej three");
        press(&mut v, "2kJ");
        assert_eq!(v.text(), "hej three\nfour five\nhej three");
        press(&mut v, "0x.");
        assert_eq!(v.text(), "hej three\nur five\nhej three");
    }

    #[test]
    fn insert_visual_commands_and_links() {
        let mut v = Vim::new("a\nb\nc");
        press(&mut v, "Ax⎋oy⎋");
        assert_eq!(v.text(), "ax\ny\nb\nc");
        press(&mut v, "jVjd");
        assert_eq!(v.text(), "ax\ny");
        assert_eq!(press(&mut v, ":wq⏎"), Event::Command("wq".into()));
        assert_eq!(press(&mut v, "A[["), Event::LinkWanted);
        assert_eq!(press(&mut v, "⎋"), Event::None);
        assert_eq!(press(&mut v, "⎋"), Event::Leave);
        let mut w = Vim::new("find me here");
        press(&mut w, "/here⏎");
        assert_eq!(w.col, 8);
        assert_eq!(press(&mut w, "/nope⏎"), Event::NotFound("nope".into()));
    }
}
