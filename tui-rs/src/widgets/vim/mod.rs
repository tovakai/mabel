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

use crate::os::keys;
use crate::os::palette::Palette;

// The editor is split by job; the pieces add to `impl Vim`.
/// keys in each mode
mod controls;
/// drawing
mod draw;
/// edits
mod edits;
/// cursor motions
mod motions;

/// Which mode the editor is in.
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

/// The last change `.` repeats: x (n characters), dd (n lines), d with a motion.
#[derive(Clone)]
enum Change {
    X(usize),
    Dd(usize),
    D(char, usize),
}

/// A place in the text: (line, column), counted in characters.
type Pos = (usize, usize);

/// The editor: the text as lines of characters, the cursor, the mode, and its memory.
pub struct Vim {
    lines: Vec<Vec<char>>,
    /// The cursor.
    row: usize,
    col: usize,
    pub mode: Mode,
    /// Keys typed so far of a command that isn't finished ("3d" waiting for its motion).
    pending: String,
    /// What y/d copied, and whether it was whole lines (then p puts lines).
    register: (String, bool),
    /// Where VISUAL started.
    anchor: Option<Pos>,
    last_change: Option<Change>,
    /// The `:` line, while it's open.
    cmdline: Option<String>,
    /// What / looked for (n looks again).
    last_search: String,
    /// The text (and cursor) before each change, for u; ctrl+r redoes.
    undo: Vec<(Vec<Vec<char>>, Pos)>,
    redo: Vec<(Vec<Vec<char>>, Pos)>,
    /// First visual row on screen.
    scroll: usize,
    /// Changed since it was opened or saved.
    pub dirty: bool,
}

/// Letters, digits and _ make words (what w and b jump between).
fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Word spans of a line, like the regex `\w+|[^\w\s]+`.
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
    /// An editor holding `text`, in NORMAL, at the top.
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

    /// The text as one string (what gets saved).
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

    /// Put the cursor on line `row` (the outline's headings use this).
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
