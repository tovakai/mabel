//! A file/project browser for CREATIVE's apps (PIXEL, TRACKER): ctrl+o, or :e without a name.
//!
//! Lists the app's folder (pixel_dir / tracker_dir): folders first (a folder is a project), then
//! files newest first, with a preview on the right that the app provides. Keys: j/k move ·
//! enter open (or go into a folder) · h/backspace up · n new · m new folder · r rename ·
//! c duplicate · D delete (moves it to .trash, so nothing is lost) · / filter · s sort by
//! name/date · esc close. An app can list extra entries (PIXEL: Mabel's avatar).
//! With unsaved changes, opening something asks first: s save, then open · d drop them.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph};

use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::bold;
use crate::widgets::input::{Input, InputResult};
use crate::widgets::wrap::width;

/// D moves things here (inside the app's folder), so a delete can always be undone by hand.
const TRASH: &str = ".trash";

/// Which preview the right side shows (each app provides its own).
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Preview {
    Sprite,
    Song,
}

/// What an app tells the browser.
pub struct Spec {
    pub title: String,
    /// The app's folder (`pixel_dir`, `tracker_dir`).
    pub root: PathBuf,
    /// Files the app opens (extensions, no dot).
    pub suffixes: Vec<String>,
    /// Listed, dimmed (exports).
    pub also_show: Vec<String>,
    /// A folder holding this file opens as one document (PIXEL: avatar.json).
    pub document_dir: Option<String>,
    /// Extra entries outside the folder: (path, label).
    pub extras: Vec<(PathBuf, String)>,
    /// The extension n gives a new file.
    pub new_suffix: String,
    /// The app has unsaved changes.
    pub dirty: bool,
    /// The file open in the app now (the browser starts next to it, cursor on it).
    pub current: Option<PathBuf>,
    pub preview: Preview,
}

/// What the browser hands back: open `path` (or make it, if `new`), saving the current work first?
pub struct Choice {
    pub path: PathBuf,
    pub new: bool,
    pub save_first: bool,
}

/// What a key did: the browser stays open, was closed, or something was chosen.
pub enum Answer {
    Open,
    Cancel,
    Chosen(Choice),
}

/// What a line in the list is.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Kind {
    /// "..": the folder above.
    Up,
    Dir,
    File,
    /// Listed, can't be opened (exports).
    Other,
    /// Outside the folder (Mabel's avatar).
    Extra,
}

/// One line in the list.
struct Entry {
    path: PathBuf,
    label: String,
    kind: Kind,
    mtime: Option<SystemTime>,
}

/// The question in the input line: a new file's name, a new folder's, a new name, a filter.
enum Ask {
    New,
    Folder,
    Rename,
    Filter,
}

/// A yes/no question: delete this? / the app has unsaved changes: save first? (the bool: a new file)
enum Confirm {
    Delete(PathBuf),
    Dirty(PathBuf, bool),
}

/// The browser, opened by an app over its own screen.
pub struct Browser {
    spec: Spec,
    /// The folder being listed, its lines, and the cursor.
    dir: PathBuf,
    entries: Vec<Entry>,
    index: usize,
    /// s: sort by name instead of newest first.
    by_name: bool,
    filter: String,
    ask: Option<(Ask, Input)>,
    confirm: Option<Confirm>,
    /// What just happened (or went wrong).
    message: String,
    /// The open file was renamed (old, new): the app follows it.
    pub renamed: Option<(PathBuf, PathBuf)>,
    /// The preview drawn last, for which file and size (made again only when those change).
    preview: Option<(PathBuf, u16, u16, Vec<Line<'static>>)>,
}

/// "12 min ago", "3 h ago", or the date.
fn ago(t: SystemTime) -> String {
    let secs = SystemTime::now().duration_since(t).map_or(0, |d| d.as_secs());
    if secs < 3600 {
        format!("{} min ago", secs / 60)
    } else if secs < 86400 {
        format!("{} h ago", secs / 3600)
    } else {
        chrono::DateTime::<chrono::Local>::from(t).format("%Y-%m-%d").to_string()
    }
}

/// A path's extension, lowercase ("" if none).
fn ext_of(p: &Path) -> String {
    p.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default()
}

/// Copy a folder and everything in it (c on a project folder).
fn copy_dir(from: &Path, to: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(to)?;
    for e in std::fs::read_dir(from)?.flatten() {
        let target = to.join(e.file_name());
        if e.path().is_dir() {
            copy_dir(&e.path(), &target)?;
        } else {
            std::fs::copy(e.path(), target)?;
        }
    }
    Ok(())
}

impl Browser {
    /// Open the browser in the folder of the app's current file (or its root).
    pub fn new(spec: Spec) -> Self {
        let _ = std::fs::create_dir_all(&spec.root);
        let here = spec
            .current
            .as_ref()
            .and_then(|c| c.parent())
            .filter(|h| h.is_dir() && h.starts_with(&spec.root))
            .map(Path::to_path_buf);
        let dir = here.unwrap_or_else(|| spec.root.clone());
        let mut b = Browser {
            spec,
            dir,
            entries: vec![],
            index: 0,
            by_name: false,
            filter: String::new(),
            ask: None,
            confirm: None,
            message: String::new(),
            renamed: None,
            preview: None,
        };
        b.reload();
        if let Some(cur) = &b.spec.current
            && let Some(i) = b.entries.iter().position(|e| &e.path == cur)
        {
            b.index = i;
        }
        b
    }

    /// List the folder: up, folders (projects), files newest first (or by name), the app's
    /// extras, filtered.
    fn reload(&mut self) {
        let mut entries = vec![];
        if self.dir != self.spec.root {
            entries.push(Entry {
                path: self.dir.parent().unwrap_or(&self.dir).to_path_buf(),
                label: "..".into(),
                kind: Kind::Up,
                mtime: None,
            });
        } else {
            for (p, label) in &self.spec.extras {
                entries.push(Entry {
                    path: p.clone(),
                    label: label.clone(),
                    kind: Kind::Extra,
                    mtime: std::fs::metadata(p).and_then(|m| m.modified()).ok(),
                });
            }
        }
        let (mut dirs, mut files) = (vec![], vec![]);
        for e in std::fs::read_dir(&self.dir).into_iter().flatten().flatten() {
            let p = e.path();
            let name = e.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') {
                continue;
            }
            if !self.filter.is_empty() && !name.to_lowercase().contains(&self.filter.to_lowercase()) {
                continue;
            }
            let mtime = e.metadata().and_then(|m| m.modified()).ok();
            if p.is_dir() {
                let doc = self.spec.document_dir.as_ref().is_some_and(|f| p.join(f).exists());
                if doc {
                    files.push(Entry { path: p, label: name, kind: Kind::File, mtime });
                } else {
                    dirs.push(Entry { path: p, label: format!("{name}/"), kind: Kind::Dir, mtime });
                }
            } else if self.spec.suffixes.contains(&ext_of(&p)) {
                files.push(Entry { path: p, label: name, kind: Kind::File, mtime });
            } else if self.spec.also_show.contains(&ext_of(&p)) {
                files.push(Entry { path: p, label: name, kind: Kind::Other, mtime });
            }
        }
        let by_name = self.by_name;
        let sort = |v: &mut Vec<Entry>| {
            if by_name {
                v.sort_by_key(|e| e.label.to_lowercase());
            } else {
                v.sort_by_key(|e| std::cmp::Reverse(e.mtime));
            }
        };
        sort(&mut dirs);
        sort(&mut files);
        files.sort_by_key(|e| e.kind == Kind::Other); // exports below what can be opened
        entries.extend(dirs);
        entries.extend(files);
        self.entries = entries;
        self.index = self.index.min(self.entries.len().saturating_sub(1));
    }

    /// The line under the cursor.
    fn entry(&self) -> Option<&Entry> {
        self.entries.get(self.index)
    }

    /// Put the cursor on `path`, if it's listed.
    fn select(&mut self, path: &Path) {
        if let Some(i) = self.entries.iter().position(|e| e.path == path) {
            self.index = i;
        }
    }

    // ── keys ─────────────────────────────────────────────────────────────

    /// A key (the list is at the top of this file).
    pub fn key(&mut self, key: &KeyEvent) -> Answer {
        self.message.clear();
        if let Some((what, input)) = &mut self.ask {
            match key.code {
                KeyCode::Esc => self.ask = None,
                _ => {
                    if input.key(key) == InputResult::Submit {
                        let text = input.value.trim().to_string();
                        let what = std::mem::replace(what, Ask::Filter);
                        self.ask = None;
                        match self.answered(what, &text) {
                            Ok(Some(answer)) => return answer,
                            Ok(None) => {}
                            Err(e) => self.message = format!("✖ {e}"),
                        }
                    }
                }
            }
            return Answer::Open;
        }
        if let Some(confirm) = self.confirm.take() {
            match (confirm, keys::ch(key)) {
                (Confirm::Delete(p), Some('D')) => {
                    if let Err(e) = self.trash(&p) {
                        self.message = format!("✖ {e}");
                    }
                }
                (Confirm::Dirty(p, new), Some(c @ ('s' | 'd'))) => {
                    return Answer::Chosen(Choice { path: p, new, save_first: c == 's' });
                }
                _ => {}
            }
            return Answer::Open;
        }
        let n = self.entries.len();
        match (keys::ch(key), key.code) {
            (_, KeyCode::Esc) => return Answer::Cancel,
            (Some('j'), _) | (_, KeyCode::Down) => self.index = (self.index + 1).min(n.saturating_sub(1)),
            (Some('k'), _) | (_, KeyCode::Up) => self.index = self.index.saturating_sub(1),
            (_, KeyCode::PageDown) => self.index = (self.index + 10).min(n.saturating_sub(1)),
            (_, KeyCode::PageUp) => self.index = self.index.saturating_sub(10),
            (Some('g'), _) | (_, KeyCode::Home) => self.index = 0,
            (Some('G'), _) | (_, KeyCode::End) => self.index = n.saturating_sub(1),
            (Some('l'), _) | (_, KeyCode::Enter | KeyCode::Right) => {
                let go_in = key.code != KeyCode::Enter;
                if let Some(a) = self.open(go_in) {
                    return a;
                }
            }
            (Some('h'), _) | (_, KeyCode::Left | KeyCode::Backspace) => self.go_up(),
            (Some('n'), _) => self.prompt(
                Ask::New,
                &format!("new {} name", if self.spec.new_suffix.is_empty() { "file" } else { &self.spec.new_suffix }),
                "",
            ),
            (Some('m'), _) => self.prompt(Ask::Folder, "new folder name", ""),
            (Some('r'), _) if self.entry().is_some_and(|e| matches!(e.kind, Kind::File | Kind::Dir | Kind::Other)) => {
                let name = self
                    .entry()
                    .and_then(|e| e.path.file_name())
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                self.prompt(Ask::Rename, "rename to", &name);
            }
            (Some('c'), _) if self.entry().is_some_and(|e| matches!(e.kind, Kind::File | Kind::Other)) => {
                let p = self.entry().map(|e| e.path.clone()).unwrap_or_default();
                if let Err(e) = self.duplicate(&p) {
                    self.message = format!("✖ {e}");
                }
            }
            (Some('D'), _) if self.entry().is_some_and(|e| matches!(e.kind, Kind::File | Kind::Dir | Kind::Other)) => {
                self.confirm = Some(Confirm::Delete(self.entry().map(|e| e.path.clone()).unwrap_or_default()));
            }
            (Some('/'), _) => {
                let f = self.filter.clone();
                self.prompt(Ask::Filter, "filter (empty clears)", &f);
            }
            (Some('s'), _) => {
                self.by_name = !self.by_name;
                self.reload();
            }
            _ => {}
        }
        Answer::Open
    }

    /// Ask a question in the input line.
    fn prompt(&mut self, what: Ask, label: &str, value: &str) {
        let mut input = Input::new(label);
        input.set(value);
        self.ask = Some((what, input));
    }

    /// enter: go into a folder (`go_in`), or choose a file (asking first if the app has
    /// unsaved changes). A folder that is one document (an avatar) opens as a file.
    fn open(&mut self, go_in: bool) -> Option<Answer> {
        let e = self.entry()?;
        let (path, kind) = (e.path.clone(), e.kind);
        if matches!(kind, Kind::Dir | Kind::Up) || (go_in && kind == Kind::File && path.is_dir()) {
            self.dir = path;
            self.index = 0;
            self.reload();
            None
        } else if kind == Kind::Other {
            self.message = "that's an export: open the song or sprite it came from".into();
            None
        } else if self.spec.dirty && Some(&path) != self.spec.current.as_ref() {
            self.confirm = Some(Confirm::Dirty(path, false));
            None
        } else {
            Some(Answer::Chosen(Choice { path, new: false, save_first: false }))
        }
    }

    /// h/backspace: the folder above (never above the app's root).
    fn go_up(&mut self) {
        if self.dir != self.spec.root {
            let came_from = self.dir.clone();
            self.dir = self.dir.parent().unwrap_or(&self.spec.root).to_path_buf();
            self.reload();
            self.select(&came_from);
        }
    }

    /// A typed name made safe as a file name: slashes become dashes; empty, . and .. are refused.
    fn safe(name: &str) -> anyhow::Result<String> {
        let n = name.replace('/', "-").trim().to_string();
        if n.is_empty() || n == "." || n == ".." {
            anyhow::bail!("give it a name");
        }
        Ok(n)
    }

    /// enter on a question: make the new file (chosen at once) or folder, rename (telling the
    /// app if its open file moved), or filter.
    fn answered(&mut self, what: Ask, text: &str) -> anyhow::Result<Option<Answer>> {
        match what {
            Ask::Filter => {
                self.filter = text.to_string();
                self.index = 0;
                self.reload();
            }
            Ask::New => {
                let mut path = self.dir.join(Self::safe(text)?);
                if !self.spec.new_suffix.is_empty() && path.extension().is_none() {
                    path.set_extension(&self.spec.new_suffix);
                }
                if path.exists() {
                    anyhow::bail!("{} already exists", path.file_name().unwrap_or_default().to_string_lossy());
                }
                if self.spec.dirty {
                    self.confirm = Some(Confirm::Dirty(path, true));
                    return Ok(None);
                }
                return Ok(Some(Answer::Chosen(Choice { path, new: true, save_first: false })));
            }
            Ask::Folder => {
                let path = self.dir.join(Self::safe(text)?);
                std::fs::create_dir(&path)?;
                self.reload();
                self.select(&path);
                self.message = format!("folder {}", path.file_name().unwrap_or_default().to_string_lossy());
            }
            Ask::Rename => {
                let Some(old) = self.entry().map(|e| e.path.clone()) else { return Ok(None) };
                let mut new = old.with_file_name(Self::safe(text)?);
                if old.is_file()
                    && new.extension().is_none()
                    && let Some(ext) = old.extension()
                {
                    new.set_extension(ext);
                }
                if new.exists() {
                    anyhow::bail!("{} already exists", new.file_name().unwrap_or_default().to_string_lossy());
                }
                std::fs::rename(&old, &new)?;
                // The open file keeps saving to the right place (also through a renamed folder).
                if let Some(cur) = self.spec.current.clone() {
                    let moved = if cur == old {
                        Some(new.clone())
                    } else if old.is_dir() || cur.starts_with(&old) {
                        cur.strip_prefix(&old).ok().map(|rest| new.join(rest))
                    } else {
                        None
                    };
                    if let Some(m) = moved {
                        self.renamed = Some((cur, m.clone()));
                        self.spec.current = Some(m);
                    }
                }
                self.reload();
                self.select(&new);
                self.message = format!("renamed to {}", new.file_name().unwrap_or_default().to_string_lossy());
            }
        }
        Ok(None)
    }

    /// c: copy a file or folder next to itself ("name 2", then "name 3"…).
    fn duplicate(&mut self, path: &Path) -> anyhow::Result<()> {
        let stem = path.file_stem().unwrap_or_default().to_string_lossy().into_owned();
        let ext = path.extension().map(|e| format!(".{}", e.to_string_lossy())).unwrap_or_default();
        let mut n = 2;
        let mut copy = path.with_file_name(format!("{stem} {n}{ext}"));
        while copy.exists() {
            n += 1;
            copy = path.with_file_name(format!("{stem} {n}{ext}"));
        }
        if path.is_dir() {
            copy_dir(path, &copy)?;
        } else {
            std::fs::copy(path, &copy)?;
        }
        self.reload();
        self.select(&copy);
        self.message = format!("copied to {}", copy.file_name().unwrap_or_default().to_string_lossy());
        Ok(())
    }

    /// D (after a yes): move it into .trash.
    fn trash(&mut self, path: &Path) -> anyhow::Result<()> {
        let trash = self.spec.root.join(TRASH);
        std::fs::create_dir_all(&trash)?;
        let name = path.file_name().unwrap_or_default().to_string_lossy();
        let target = trash.join(format!("{} {name}", chrono::Local::now().format("%Y%m%d-%H%M%S")));
        std::fs::rename(path, &target)?;
        self.reload();
        self.message = format!("{name} moved to {TRASH}/");
        Ok(())
    }

    // ── drawing ──────────────────────────────────────────────────────────

    /// The preview for the line under the cursor (the app's sprite or song card), cached.
    fn preview_lines(&mut self, w: u16, h: u16, pal: &Palette) -> Vec<Line<'static>> {
        let Some(e) = self.entry() else { return vec![] };
        let (path, kind) = (e.path.clone(), e.kind);
        match kind {
            Kind::Up => vec![],
            Kind::Dir => {
                let n = std::fs::read_dir(&path)
                    .map(|d| d.flatten().filter(|e| !e.file_name().to_string_lossy().starts_with('.')).count())
                    .unwrap_or(0);
                [
                    String::new(),
                    " project folder".into(),
                    format!(" {n} item{}", if n == 1 { "" } else { "s" }),
                    String::new(),
                    " enter opens it".into(),
                ]
                .into_iter()
                .map(|t| Line::styled(t, Style::new().fg(pal.amber)))
                .collect()
            }
            Kind::Other => {
                let size = std::fs::metadata(&path).map_or(0, |m| m.len());
                [
                    String::new(),
                    format!(" an export (.{})", ext_of(&path)),
                    format!(" {:.0} KB", size as f64 / 1024.0),
                    String::new(),
                    " not opened here".into(),
                ]
                .into_iter()
                .map(|t| Line::styled(t, Style::new().fg(pal.amber_dim)))
                .collect()
            }
            Kind::File | Kind::Extra => {
                if let Some((p, pw, ph, lines)) = &self.preview
                    && *p == path
                    && (*pw, *ph) == (w, h)
                {
                    return lines.clone();
                }
                let lines = match self.spec.preview {
                    Preview::Sprite => crate::channels::creative::pixelview::thumbnail(&path, w, h, pal),
                    Preview::Song => crate::channels::creative::trackerview::song_card(&path, w, h, pal),
                };
                self.preview = Some((path, w, h, lines.clone()));
                lines
            }
        }
    }

    /// Draw the browser over the app: the list, the preview, and the question or message line.
    pub fn render(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        let w = area.width * 9 / 10;
        let h = area.height * 85 / 100;
        let r = Rect::new(area.x + (area.width - w) / 2, area.y + (area.height - h) / 2, w, h);
        let block = Block::new()
            .borders(Borders::ALL)
            .border_type(BorderType::Thick)
            .border_style(Style::new().fg(pal.amber))
            .title(Span::styled(format!(" {} ", self.spec.title), bold(pal.beige)))
            .style(Style::new().bg(pal.panel).fg(pal.amber));
        let inner = block.inner(r);
        frame.render_widget(Clear, r);
        frame.render_widget(block, r);
        let ask_h = if self.ask.is_some() { 1 } else { 0 };
        let [main, help, ask] =
            Layout::vertical([Constraint::Fill(1), Constraint::Length(1), Constraint::Length(ask_h)]).areas(inner);
        let [list, preview] = Layout::horizontal([Constraint::Fill(58), Constraint::Fill(42)]).areas(main);

        // The list.
        let rel = self.dir.strip_prefix(&self.spec.root).map(|p| p.display().to_string()).unwrap_or_default();
        let root_name = self.spec.root.file_name().unwrap_or_default().to_string_lossy();
        let mut head = vec![Span::styled(
            format!(" {root_name}/{}", if rel.is_empty() { String::new() } else { format!("{rel}/") }),
            bold(pal.beige),
        )];
        if !self.filter.is_empty() {
            head.push(Span::styled(format!("  filter: {}", self.filter), Style::new().fg(pal.sand)));
        }
        let mut lines = vec![Line::from(head)];
        let rows = (list.height as usize).saturating_sub(1).max(1);
        let top = (self.index + 1).saturating_sub(rows);
        if self.entries.is_empty() {
            lines.push(Line::styled("  (empty: n makes a new one)", Style::new().fg(pal.amber_dim)));
        }
        let lw = list.width as usize;
        for (i, e) in self.entries.iter().enumerate().skip(top).take(rows) {
            let (icon, colour) = match e.kind {
                Kind::Dir => ("▸ ", pal.amber_hi),
                Kind::Up => ("◂ ", pal.amber_dim),
                Kind::File => ("◆ ", pal.amber),
                Kind::Other => ("· ", pal.amber_dim),
                Kind::Extra => ("★ ", pal.teal),
            };
            let when = e.mtime.map(ago).unwrap_or_default();
            let mut style = Style::new().fg(colour);
            if i == self.index {
                style = bold(pal.bg).bg(pal.amber);
            } else if self.spec.current.as_ref() == Some(&e.path) {
                style = style.add_modifier(Modifier::UNDERLINED);
            }
            let room = lw.saturating_sub(width(&when) + 1);
            let mut name: String = format!("{icon}{}", e.label);
            while width(&name) > room && name.pop().is_some() {}
            let pad = room.saturating_sub(width(&name));
            lines.push(Line::from(vec![
                Span::styled(format!("{name}{}", " ".repeat(pad)), style),
                Span::styled(when, if i == self.index { style } else { Style::new().fg(pal.amber_dim) }),
            ]));
        }
        frame.render_widget(Paragraph::new(lines), list);

        // The preview, behind a faint line.
        let sep = Rect { width: 1, ..preview };
        frame.render_widget(
            Paragraph::new(vec![Line::styled("▏", Style::new().fg(pal.amber_faint)); preview.height as usize]),
            sep,
        );
        let pv = Rect { x: preview.x + 2, width: preview.width.saturating_sub(2), ..preview };
        let lines = self.preview_lines(pv.width, pv.height, pal);
        frame.render_widget(Paragraph::new(lines), pv);

        // Help, a confirmation, or what just happened.
        let help_line = match &self.confirm {
            Some(Confirm::Delete(p)) => Line::styled(
                format!(
                    " delete {}? D again moves it to .trash · esc keeps it",
                    p.file_name().unwrap_or_default().to_string_lossy()
                ),
                bold(pal.red),
            ),
            Some(Confirm::Dirty(..)) => {
                Line::styled(" unsaved changes! s save them, then open · d drop them · esc stay", bold(pal.red))
            }
            None if !self.message.is_empty() => Line::styled(format!(" {}", self.message), bold(pal.sand)),
            None => Line::styled(
                format!(
                    " enter open · h up · n new · m folder · r rename · c duplicate · D delete · / filter · s sort ({}) · esc",
                    if self.by_name { "name" } else { "newest" }
                ),
                Style::new().fg(pal.amber_dim),
            ),
        };
        frame.render_widget(Paragraph::new(help_line), help);
        if let Some((_, input)) = &mut self.ask {
            input.render(
                frame,
                ask,
                Style::new().fg(pal.amber_hi).bg(pal.amber_faint),
                Style::new().fg(pal.amber_dim).bg(pal.amber_faint),
                true,
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    fn press(b: &mut Browser, c: char) -> Answer {
        b.key(&KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE))
    }

    #[test]
    fn list_rename_follows_and_trash() {
        let root = std::env::temp_dir().join(format!("mabel-browser-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(root.join("project")).unwrap();
        std::fs::write(root.join("a.mpx"), "{}").unwrap();
        std::fs::write(root.join("a.wav"), "x").unwrap();
        std::fs::write(root.join("notes.txt"), "x").unwrap();
        let spec = Spec {
            title: "T".into(),
            root: root.clone(),
            suffixes: vec!["mpx".into()],
            also_show: vec!["wav".into()],
            document_dir: None,
            extras: vec![],
            new_suffix: "mpx".into(),
            dirty: false,
            current: Some(root.join("a.mpx")),
            preview: Preview::Sprite,
        };
        let mut b = Browser::new(spec);
        let labels: Vec<&str> = b.entries.iter().map(|e| e.label.as_str()).collect();
        assert_eq!(labels, vec!["project/", "a.mpx", "a.wav"], "folders first, exports last, other files hidden");
        b.index = 1;
        press(&mut b, 'r');
        b.ask.as_mut().unwrap().1.set("b");
        let _ = b.key(&KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(b.renamed, Some((root.join("a.mpx"), root.join("b.mpx"))));
        press(&mut b, 'D');
        press(&mut b, 'D');
        assert!(!root.join("b.mpx").exists() && std::fs::read_dir(root.join(TRASH)).unwrap().count() == 1);
        let _ = std::fs::remove_dir_all(&root);
    }
}
