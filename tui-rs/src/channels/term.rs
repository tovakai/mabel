//! CH-06 TERM: real shells inside MABEL-OS (a pty runs the shell, vt100 keeps its screen).
//!
//! Inside a shell every key goes to the program (vim, htop, ssh work), F-keys and ctrl+q
//! included, except alt+1–8 (channels) and ctrl+] which steps back out to the sections.
//! Tabs: N new shell, [ ] or < > previous/next, X close (in nav mode). shift+PageUp/PageDown
//! scroll back. The 16 terminal colours follow the MABEL-OS palette.

use std::io::{Read, Write};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use portable_pty::{CommandBuilder, MasterPty, PtySize, native_pty_system};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;

use crate::os::channel::{Channel, Ctx, Msg, Outcome, View};
use crate::os::keys;
use crate::os::palette::{Palette, hex};
use crate::os::ui::{self, bold};

const ID: &str = "term";
const SCROLLBACK: usize = 2000;
/// vt100 sizes are never zero.
fn nz(v: u16) -> std::num::NonZeroU16 {
    std::num::NonZeroU16::new(v.max(1)).expect("at least 1")
}

/// The screen's (rows, cols).
fn size_of(parser: &vt100::Parser) -> (u16, u16) {
    let (r, c) = parser.screen().size();
    (r.get(), c.get())
}

/// Set in TERM's shells: MABEL-OS started there would be one inside another.
pub const INSIDE_ENV: &str = "MABEL_OS_TERM";

enum Got {
    Output { tab: u64, bytes: Vec<u8> },
    Ended { tab: u64 },
}

struct Shell {
    id: u64,
    title: String,
    parser: vt100::Parser,
    master: Option<Box<dyn MasterPty + Send>>,
    writer: Option<Box<dyn Write + Send>>,
    child: Option<Box<dyn portable_pty::Child + Send + Sync>>,
    alive: bool,
    /// Lines scrolled back (shift+PageUp).
    scrolled: usize,
}

impl Shell {
    fn new(id: u64, rows: u16, cols: u16) -> Self {
        Shell {
            id,
            title: format!("shell {id}"),
            parser: vt100::Parser::new(nz(rows), nz(cols), SCROLLBACK),
            master: None,
            writer: None,
            child: None,
            alive: false,
            scrolled: 0,
        }
    }

    /// Start a login shell in a pty; its output comes back as messages.
    fn spawn(&mut self, ctx: &Ctx) {
        let (rows, cols) = size_of(&self.parser);
        let size = PtySize { rows, cols, pixel_width: 0, pixel_height: 0 };
        let pair = match native_pty_system().openpty(size) {
            Ok(p) => p,
            Err(e) => {
                self.parser.process(format!("\x1b[31mno pty: {e}\x1b[0m").as_bytes());
                return;
            }
        };
        let shell = std::env::var("SHELL").unwrap_or_else(|_| "/bin/bash".into());
        let mut cmd = CommandBuilder::new(&shell);
        cmd.arg("-l");
        cmd.cwd(crate::os::config::home());
        cmd.env("TERM", "xterm-256color");
        cmd.env("COLORTERM", "truecolor");
        cmd.env("MABEL_OS", "1");
        cmd.env(INSIDE_ENV, std::process::id().to_string());
        cmd.env_remove("MABEL_OS_PAUSED");
        match pair.slave.spawn_command(cmd) {
            Ok(child) => self.child = Some(child),
            Err(e) => {
                self.parser.process(format!("\x1b[31mcouldn't start {shell}: {e}\x1b[0m").as_bytes());
                return;
            }
        }
        drop(pair.slave);
        let (Ok(mut reader), Ok(writer)) = (pair.master.try_clone_reader(), pair.master.take_writer()) else { return };
        self.writer = Some(writer);
        self.master = Some(pair.master);
        self.alive = true;
        let (tx, tab) = (ctx.tx.clone(), self.id);
        // The pty blocks on read: a thread of its own per shell.
        std::thread::spawn(move || {
            let mut buf = vec![0u8; 65536];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        if tx.send(Msg::For(ID, Box::new(Got::Output { tab, bytes: buf[..n].to_vec() }))).is_err() {
                            return;
                        }
                    }
                }
            }
            let _ = tx.send(Msg::For(ID, Box::new(Got::Ended { tab })));
        });
    }

    fn write(&mut self, bytes: &[u8]) {
        if let Some(w) = &mut self.writer {
            let _ = w.write_all(bytes);
            let _ = w.flush();
        }
    }

    fn resize(&mut self, rows: u16, cols: u16) {
        if size_of(&self.parser) == (rows, cols) || rows == 0 || cols == 0 {
            return;
        }
        self.parser.screen_mut().set_size(nz(rows), nz(cols));
        if let Some(m) = &self.master {
            let _ = m.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
        }
    }

    fn close(&mut self) {
        if let Some(child) = &mut self.child {
            let _ = child.kill();
        }
        self.alive = false;
    }

    fn scroll(&mut self, lines: isize) {
        let screen = self.parser.screen_mut();
        let want = (self.scrolled as isize + lines).max(0) as usize;
        screen.set_scrollback(want);
        self.scrolled = screen.scrollback(); // vt100 stops at what history there is
    }
}

impl Drop for Shell {
    fn drop(&mut self) {
        self.close();
    }
}

/// A key as the bytes a terminal sends (xterm style). None: not for the program.
pub fn key_bytes(key: &KeyEvent, app_cursor: bool) -> Option<Vec<u8>> {
    let mods = key.modifiers;
    let ctrl = mods.contains(KeyModifiers::CONTROL);
    let alt = mods.contains(KeyModifiers::ALT) && !ctrl; // ctrl+alt is AltGr: the character counts
    let csi_mod = |base: &str, end: char| {
        let m = 1
            + u8::from(mods.contains(KeyModifiers::SHIFT))
            + 2 * u8::from(mods.contains(KeyModifiers::ALT))
            + 4 * u8::from(ctrl);
        if m > 1 { format!("\x1b[1;{m}{end}").into_bytes() } else { format!("{base}{end}").into_bytes() }
    };
    let arrow = |end: char| {
        if app_cursor && mods.is_empty() { format!("\x1bO{end}").into_bytes() } else { csi_mod("\x1b[", end) }
    };
    let bytes = match key.code {
        KeyCode::Char(c) if ctrl && !mods.contains(KeyModifiers::ALT) => match c {
            'a'..='z' | 'A'..='Z' => vec![(c.to_ascii_lowercase() as u8) & 0x1f],
            ' ' | '@' | '2' => vec![0],
            '[' | '3' => vec![0x1b],
            '\\' | '4' => vec![0x1c],
            ']' | '5' => vec![0x1d],
            '^' | '6' => vec![0x1e],
            '_' | '7' | '-' => vec![0x1f],
            _ => c.to_string().into_bytes(),
        },
        KeyCode::Char(c) => {
            let mut b = if alt { vec![0x1b] } else { vec![] };
            b.extend(c.to_string().into_bytes());
            b
        }
        KeyCode::Enter => vec![b'\r'],
        KeyCode::Tab => vec![b'\t'],
        KeyCode::BackTab => b"\x1b[Z".to_vec(),
        KeyCode::Backspace if alt => vec![0x1b, 0x7f],
        KeyCode::Backspace => vec![0x7f],
        KeyCode::Esc => vec![0x1b],
        KeyCode::Up => arrow('A'),
        KeyCode::Down => arrow('B'),
        KeyCode::Right => arrow('C'),
        KeyCode::Left => arrow('D'),
        KeyCode::Home => csi_mod("\x1b[", 'H'),
        KeyCode::End => csi_mod("\x1b[", 'F'),
        KeyCode::PageUp => b"\x1b[5~".to_vec(),
        KeyCode::PageDown => b"\x1b[6~".to_vec(),
        KeyCode::Delete => b"\x1b[3~".to_vec(),
        KeyCode::Insert => b"\x1b[2~".to_vec(),
        KeyCode::F(n @ 1..=4) => format!("\x1bO{}", (b'P' + n - 1) as char).into_bytes(),
        KeyCode::F(n @ 5..=12) => {
            let code = [15, 17, 18, 19, 20, 21, 23, 24][(n - 5) as usize];
            format!("\x1b[{code}~").into_bytes()
        }
        _ => return None,
    };
    Some(bytes)
}

/// The terminal's 256 colours: the first 16 in MABEL-OS's palette, the rest xterm's cube and greys.
fn colour(c: vt100::Color, pal: &Palette, fallback: Color) -> Color {
    match c {
        vt100::Color::Default => fallback,
        vt100::Color::Rgb(r, g, b) => Color::Rgb(r, g, b),
        vt100::Color::Idx(i) => {
            let named = |h: &str| hex(h).unwrap_or(fallback);
            match i {
                0 => pal.bg,
                1 => pal.red,
                2 => named("#a8c66c"),
                3 => pal.amber,
                4 => named("#6aa6c8"),
                5 => named("#d98bb5"),
                6 => pal.teal,
                7 => pal.beige,
                8 => pal.beige_dark,
                9 => pal.orange,
                10 => named("#c8e08a"),
                11 => pal.amber_hi,
                12 => named("#8cc4e0"),
                13 => named("#f0a8cc"),
                14 => named("#7fe8dc"),
                15 => named("#f5ecd2"),
                16..=231 => {
                    let n = i - 16;
                    let level = |v: u8| if v == 0 { 0 } else { 55 + v * 40 };
                    Color::Rgb(level(n / 36), level(n / 6 % 6), level(n % 6))
                }
                _ => {
                    let v = 8 + (i - 232) * 10;
                    Color::Rgb(v, v, v)
                }
            }
        }
    }
}

pub struct Term {
    shells: Vec<Shell>,
    current: usize,
    next_id: u64,
    /// The last drawn size, for new shells.
    size: (u16, u16),
    started: bool,
}

impl Term {
    pub fn new() -> Self {
        Term { shells: vec![], current: 0, next_id: 1, size: (24, 80), started: false }
    }

    fn new_tab(&mut self, ctx: &Ctx) {
        let mut shell = Shell::new(self.next_id, self.size.0, self.size.1);
        self.next_id += 1;
        shell.spawn(ctx);
        self.shells.push(shell);
        self.current = self.shells.len() - 1;
    }

    fn close_tab(&mut self, ctx: &Ctx) {
        if self.shells.is_empty() {
            return;
        }
        self.shells.remove(self.current); // dropping it ends the shell
        if self.shells.is_empty() {
            self.new_tab(ctx);
        }
        self.current = self.current.min(self.shells.len() - 1);
    }

    fn step(&mut self, by: isize) {
        let n = self.shells.len() as isize;
        if n > 0 {
            self.current = (self.current as isize + by).rem_euclid(n) as usize;
        }
    }
}

impl Channel for Term {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "TERM"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        let [tabs, body] = Layout::vertical([Constraint::Length(1), Constraint::Fill(1)]).areas(area);
        let mut spans = vec![Span::raw(" ")];
        for (i, sh) in self.shells.iter().enumerate() {
            let label = format!(" {} {}", sh.title, if sh.alive { "" } else { "✖ " });
            let style = if i == self.current { bold(pal.bg).bg(pal.amber) } else { Style::new().fg(pal.amber_dim) };
            spans.push(Span::styled(label, style));
            spans.push(Span::raw(" "));
        }
        spans.push(Span::styled(
            "   N new · [ ] or < > switch · X close (in nav mode)",
            Style::new().fg(pal.amber_faint),
        ));
        frame.render_widget(Paragraph::new(Line::from(spans)), tabs);

        let block = ui::panel(pal, "TERM", view.panel_state(0)).padding(ratatui::widgets::Padding::ZERO).title_bottom(
            Line::styled(" ctrl+] back to the sections · shift+PgUp scroll back ", Style::new().fg(pal.amber_dim))
                .right_aligned(),
        );
        let inner = block.inner(body);
        frame.render_widget(block, body);
        self.size = (inner.height.max(5), inner.width.max(20));
        let inside = view.inside(0);
        let Some(sh) = self.shells.get_mut(self.current) else { return vec![body] };
        sh.resize(inner.height, inner.width);

        let screen = sh.parser.screen();
        let buf = frame.buffer_mut();
        for row in 0..inner.height {
            for col in 0..inner.width {
                let Some(cell) = screen.cell(row, col) else { continue };
                if cell.is_wide_continuation() {
                    continue;
                }
                let (mut fg, mut bg) = (colour(cell.fgcolor(), pal, pal.amber), colour(cell.bgcolor(), pal, pal.bg));
                if cell.inverse() {
                    std::mem::swap(&mut fg, &mut bg);
                }
                let mut style = Style::new().fg(fg).bg(bg);
                if cell.bold() {
                    style = style.add_modifier(Modifier::BOLD);
                }
                if cell.italic() {
                    style = style.add_modifier(Modifier::ITALIC);
                }
                if cell.underline() {
                    style = style.add_modifier(Modifier::UNDERLINED);
                }
                if cell.dim() {
                    style = style.add_modifier(Modifier::DIM);
                }
                let text = if cell.has_contents() { cell.contents() } else { " " };
                buf[(inner.x + col, inner.y + row)].set_symbol(text).set_style(style);
            }
        }
        if inside && sh.scrolled == 0 && !screen.hide_cursor() {
            let (r, c) = screen.cursor_position();
            frame.set_cursor_position(Position::new(
                inner.x + c.min(inner.width - 1),
                inner.y + r.min(inner.height - 1),
            ));
        }
        vec![body]
    }

    fn raw_keys(&self, _section: usize) -> bool {
        true
    }

    fn key(&mut self, _section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.shells.is_empty() {
            self.new_tab(ctx);
        }
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        let Some(sh) = self.shells.get_mut(self.current) else { return Outcome::Ignored };
        match key.code {
            KeyCode::PageUp if shift => {
                let page = size_of(&sh.parser).0 as isize / 2;
                sh.scroll(page);
                return Outcome::Used;
            }
            KeyCode::PageDown if shift => {
                let page = size_of(&sh.parser).0 as isize / 2;
                sh.scroll(-page);
                return Outcome::Used;
            }
            _ => {}
        }
        if !sh.alive {
            if key.code == KeyCode::Enter {
                sh.parser.process(b"\r\n");
                sh.spawn(ctx);
            }
            return Outcome::Used;
        }
        if sh.scrolled > 0 {
            sh.scroll(-(sh.scrolled as isize)); // typing comes back to the live screen
        }
        if let Some(bytes) = key_bytes(&key, sh.parser.screen().application_cursor()) {
            sh.write(&bytes);
        }
        Outcome::Used
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        match keys::ch(&key) {
            Some('N') => self.new_tab(ctx),
            Some('X') => self.close_tab(ctx),
            Some(']' | '>') => self.step(1),
            Some('[' | '<') => self.step(-1),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn paste(&mut self, _section: usize, text: &str, _ctx: &mut Ctx) {
        let Some(sh) = self.shells.get_mut(self.current) else { return };
        let mut data = text.replace("\r\n", "\n").replace('\n', "\r").into_bytes();
        if sh.parser.screen().bracketed_paste() {
            let mut wrapped = b"\x1b[200~".to_vec();
            wrapped.append(&mut data);
            wrapped.extend_from_slice(b"\x1b[201~");
            data = wrapped;
        }
        sh.write(&data);
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        Some(0) // a terminal is for typing
    }

    fn tick(&mut self, _now: std::time::Instant, visible: bool, ctx: &mut Ctx) {
        // The first shell starts when TERM is first shown (not at boot: no shell nobody asked for).
        if visible && !self.started {
            self.started = true;
            self.new_tab(ctx);
        }
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, _ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Output { tab, bytes } => {
                if let Some(sh) = self.shells.iter_mut().find(|s| s.id == tab) {
                    sh.parser.process(&bytes);
                    if sh.scrolled > 0 {
                        sh.scroll(-(sh.scrolled as isize)); // new output: back to the live screen
                    }
                }
            }
            Got::Ended { tab } => {
                if let Some(sh) = self.shells.iter_mut().find(|s| s.id == tab) {
                    sh.alive = false;
                    sh.writer = None;
                    sh.master = None;
                    if let Some(mut c) = sh.child.take() {
                        let _ = c.wait();
                    }
                    sh.parser
                        .process(b"\r\n\x1b[33m[the shell ended: enter starts a new one, X closes the tab]\x1b[0m");
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(code: KeyCode, mods: KeyModifiers) -> Option<Vec<u8>> {
        key_bytes(&KeyEvent::new(code, mods), false)
    }

    #[test]
    fn keys_as_terminal_bytes() {
        assert_eq!(k(KeyCode::Char('c'), KeyModifiers::CONTROL), Some(vec![3]));
        assert_eq!(k(KeyCode::Char('å'), KeyModifiers::NONE), Some("å".as_bytes().to_vec()));
        assert_eq!(k(KeyCode::Char('x'), KeyModifiers::ALT), Some(b"\x1bx".to_vec()));
        assert_eq!(k(KeyCode::Char('['), KeyModifiers::CONTROL | KeyModifiers::ALT), Some(b"[".to_vec()), "AltGr");
        assert_eq!(k(KeyCode::Up, KeyModifiers::NONE), Some(b"\x1b[A".to_vec()));
        assert_eq!(key_bytes(&KeyEvent::new(KeyCode::Up, KeyModifiers::NONE), true), Some(b"\x1bOA".to_vec()));
        assert_eq!(k(KeyCode::Right, KeyModifiers::CONTROL), Some(b"\x1b[1;5C".to_vec()));
        assert_eq!(k(KeyCode::F(5), KeyModifiers::NONE), Some(b"\x1b[15~".to_vec()));
        assert_eq!(k(KeyCode::F(1), KeyModifiers::NONE), Some(b"\x1bOP".to_vec()));
    }

    #[test]
    fn palette_colours() {
        let pal = Palette::base();
        assert_eq!(colour(vt100::Color::Idx(3), &pal, Color::Reset), pal.amber);
        assert_eq!(colour(vt100::Color::Idx(16), &pal, Color::Reset), Color::Rgb(0, 0, 0));
        assert_eq!(colour(vt100::Color::Idx(231), &pal, Color::Reset), Color::Rgb(255, 255, 255));
        assert_eq!(colour(vt100::Color::Idx(232), &pal, Color::Reset), Color::Rgb(8, 8, 8));
    }
}
