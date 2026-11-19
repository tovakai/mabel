//! CH-05 VAULT: his Obsidian vault in MABEL-OS.
//!
//! Left: the explorer (folders drill down; / searches contents; # tags; r recent). Centre: the
//! note, rendered (wiki links, callouts, tags, properties) or in the vim editor (e, or i inside).
//! Right: outline, backlinks, links, the note's tags. Keys: o quick open · f follow a link ·
//! D today's daily note · n new note · R rename · V switch vault (core of truth / Mabel's
//! journal) · M ask Mabel about this note · O outline on/off · backspace back.

/// reading the vault: notes, links, tags, frontmatter
pub mod notes;

use std::path::PathBuf;

use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;

use crate::channels::vault::notes::{INLINE_TAG, Note, Vault, WIKILINK, split_frontmatter};
use crate::os::channel::{AskAbout, Channel, Ctx, OpenNote, Outcome, Request, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::markdown;
use crate::widgets::popup::{Answer, Ask, frame_box};
use crate::widgets::vim::{self, Vim};

/// The id messages and help use.
const ID: &str = "vault";
// The three sections, in the order `render` returns them.
const EXPLORER: usize = 0;
const NOTE: usize = 1;
const SIDE: usize = 2;

// VAULT's code is split by job; every piece adds to `impl VaultView`.
/// EXPLORER and the side panel
mod lists;
/// Obsidian's extras as plain Markdown
mod obsidian;
/// the note page: opening, editing, saving notes (notes.rs reads the vault's files)
mod page;
/// pickers and questions
mod popups;

pub use obsidian::obsidian_to_markdown;

/// Where an entry in the explorer or the side panel leads.
#[derive(Clone, Debug)]
enum Route {
    Dir(String),
    Note(String, Option<usize>),
    Tag(String),
    Heading(usize),
    /// A link to a note that doesn't exist yet.
    Missing(String),
}

/// One line in EXPLORER or the side panel, and where enter takes you (None: a heading).
#[derive(Clone)]
struct Entry {
    line: Line<'static>,
    route: Option<Route>,
}

/// What EXPLORER lists.
#[derive(Clone, PartialEq)]
enum Explorer {
    Files,
    Search(String),
    Tags,
    Tag(String),
    Recent,
}

/// What a picker is choosing.
enum PickFor {
    /// o: a note to open.
    Open,
    /// a note to link to (inserted as `[[name]]` in the editor).
    Link,
    /// f: one of this note's links to follow.
    Follow,
}

/// A box over the page that takes the keys while it's open.
enum Popup {
    /// Type to filter, j/k or arrows, enter picks.
    Picker {
        title: String,
        input: Input,
        options: Vec<(String, String)>,
        at: usize,
        purpose: PickFor,
    },
    Search(Ask),
    NewNote(Ask),
    Rename(Ask),
}

/// Background work reporting back: a vault finished scanning.
enum Got {
    Loaded { name: String, vault: Option<Vault>, then: Option<(String, Option<usize>)> },
}

/// The VAULT screen.
pub struct VaultView {
    /// The vaults V switches between: (name, folder). His vault, and Mabel's journal if it exists.
    vaults: Vec<(String, PathBuf)>,
    /// The open vault, scanned (every note's links, tags, headings), and its name.
    vault: Option<Vault>,
    vault_name: String,
    /// A scan is running.
    loading: bool,
    /// The status line's message.
    message: String,
    /// The open note (its path inside the vault, with .md) and its text.
    note: Option<String>,
    text: String,
    /// The note rendered for the screen, and the width it was rendered for.
    rendered: Option<(u16, Vec<Line<'static>>)>,
    /// The first line of the note on screen.
    scroll: usize,
    /// Scroll to about this file line once the note is drawn (search hits, outline).
    jump_to: Option<usize>,
    /// How many lines the note's panel showed last time (PgUp/PgDn move by it).
    page: usize,
    /// The vim editor, while editing.
    editor: Option<Vim>,
    /// The folder EXPLORER is in ("" = the vault's top).
    folder: String,
    explorer: Explorer,
    /// EXPLORER's lines and cursor.
    entries: Vec<Entry>,
    entry_at: usize,
    /// The side panel's lines (outline, links, backlinks, tags) and cursor.
    side: Vec<Entry>,
    side_at: usize,
    /// O: the side panel on or off. None = the default: on, except on the deck.
    side_shown: Option<bool>,
    /// Notes opened before this one, for backspace.
    history: Vec<String>,
    popup: Option<Popup>,
    /// Where the three sections were drawn (mouse clicks use them).
    rects: Vec<Rect>,
    /// The first vault has been loaded (on the first visit, not at start-up).
    started: bool,
}

impl VaultView {
    /// `core` is his vault, `journal` Mabel's (offered only if the folder exists).
    pub fn new(core: &str, journal: &str) -> Self {
        let mut vaults = vec![("core of truth".to_string(), crate::os::config::expand(core))];
        let journal = crate::os::config::expand(journal);
        if journal.is_dir() {
            vaults.push(("Mabel's journal".to_string(), journal));
        }
        VaultView {
            vaults,
            vault: None,
            vault_name: String::new(),
            loading: false,
            message: String::new(),
            note: None,
            text: String::new(),
            rendered: None,
            scroll: 0,
            jump_to: None,
            page: 10,
            editor: None,
            folder: String::new(),
            explorer: Explorer::Files,
            entries: vec![],
            entry_at: 0,
            side: vec![],
            side_at: 0,
            side_shown: None,
            history: vec![],
            popup: None,
            rects: vec![Rect::default(); 3],
            started: false,
        }
    }

    /// Scan vault `index` in the background (a big vault takes a moment); `then` opens a note
    /// (and line) when it's done.
    fn load_vault(&mut self, index: usize, then: Option<(String, Option<usize>)>, ctx: &Ctx) {
        let (name, root) = self.vaults[index % self.vaults.len()].clone();
        self.loading = true;
        ctx.run(ID, async move {
            let vault = tokio::task::spawn_blocking(move || {
                let v = Vault::new(root);
                v.exists().then(|| v.scan())
            })
            .await
            .ok()
            .flatten();
            Got::Loaded { name, vault, then }
        });
    }
}

impl VaultView {
    // ── keys ─────────────────────────────────────────────────────────────

    /// VAULT's own keys, in nav mode and inside the explorer, note (reading) and side panel.
    fn common(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.vault.is_none() {
            return Outcome::Ignored;
        }
        if keys::ctrl(key, 's') {
            self.save(ctx);
            return Outcome::Used;
        }
        let pal = ctx.pal;
        match (keys::ch(key), key.code) {
            (Some('o'), _) => self.picker("OPEN NOTE", PickFor::Open),
            (Some('/'), _) => {
                self.popup = Some(Popup::Search(Ask::new("SEARCH THE VAULT", "words (all must be on the line)")))
            }
            (Some('#'), _) => {
                self.explorer = Explorer::Tags;
                self.build_explorer(&pal);
                ctx.focus(EXPLORER);
            }
            (Some('r'), _) => {
                self.explorer = Explorer::Recent;
                self.build_explorer(&pal);
                ctx.focus(EXPLORER);
            }
            (Some('f'), _) if self.note.is_some() => self.picker("FOLLOW A LINK", PickFor::Follow),
            (Some('e'), _) if self.note.is_some() => self.edit(ctx),
            (Some('O'), _) => self.side_shown = Some(!self.side_shown.unwrap_or(true)),
            (Some('D'), _) => {
                if self.vault_name != "core of truth" {
                    ctx.toast("VAULT", "Daily notes live in core of truth (V switches vault)");
                } else {
                    let today = chrono::Local::now().date_naive();
                    match self.vault.as_mut().map(|v| v.daily(today)) {
                        Some(Ok(rel)) => self.open_note(&rel, None, true, ctx),
                        Some(Err(e)) => ctx.error("VAULT", &e.to_string()),
                        None => {}
                    }
                }
            }
            (Some('n'), _) => {
                let where_ = if self.explorer == Explorer::Files && !self.folder.is_empty() {
                    self.folder.clone()
                } else {
                    "the vault root".into()
                };
                self.popup = Some(Popup::NewNote(Ask::new(&format!("NEW NOTE IN {}", where_.to_uppercase()), "name")));
            }
            (Some('R'), _) if self.note.is_some() => {
                let name = self
                    .note
                    .as_ref()
                    .and_then(|r| self.vault.as_ref()?.notes.get(r))
                    .map(|n| n.name.clone())
                    .unwrap_or_default();
                let mut ask = Ask::new(&format!("RENAME {}", name.to_uppercase()), "new name (links are updated)");
                ask.input.set(&name);
                self.popup = Some(Popup::Rename(ask));
            }
            (Some('V'), _) => {
                if self.vaults.len() < 2 {
                    ctx.toast("VAULT", "Only one vault here");
                } else {
                    let at = self.vaults.iter().position(|(n, _)| *n == self.vault_name).unwrap_or(0);
                    self.note = None;
                    self.load_vault(at + 1, None, ctx);
                }
            }
            (Some('M'), _) if self.note.is_some() => {
                let rel = self.note.clone().unwrap_or_default();
                let name =
                    self.vault.as_ref().and_then(|v| v.notes.get(&rel)).map(|n| n.name.clone()).unwrap_or_default();
                let content = self.text.clone();
                ctx.requests.push(Request::Deliver { to: "mabel", msg: Box::new(AskAbout { name, rel, content }) });
            }
            (_, KeyCode::Backspace) => self.back(ctx),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }
}

impl Channel for VaultView {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "VAULT"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        let side_on = self.side_shown.unwrap_or(!view.narrow);
        let (ew, sw) =
            if view.narrow { (28, if side_on { 32 } else { 0 }) } else { (32, if side_on { 36 } else { 0 }) };
        let [explorer, note, side] =
            Layout::horizontal([Constraint::Length(ew), Constraint::Fill(1), Constraint::Length(sw)]).areas(area);

        let title = if self.vault_name.is_empty() {
            "VAULT".to_string()
        } else {
            format!("VAULT ░ {}", self.vault_name.to_uppercase())
        };
        let block = ui::panel(pal, &title, view.panel_state(EXPLORER));
        let inner = block.inner(explorer);
        frame.render_widget(block, explorer);
        Self::draw_list(frame, inner, &self.entries, self.entry_at, view.inside(EXPLORER), pal);

        self.render_note(frame, note, view);

        if sw > 0 {
            let block = ui::panel(pal, "OUTLINE · LINKS", view.panel_state(SIDE));
            let inner = block.inner(side);
            frame.render_widget(block, side);
            Self::draw_list(frame, inner, &self.side, self.side_at, view.inside(SIDE), pal);
        }
        self.render_popup(frame, area, pal);
        self.rects = vec![explorer, note, if sw > 0 { side } else { Rect::default() }];
        self.rects.clone()
    }

    fn captures_keys(&self) -> bool {
        self.popup.is_some()
    }

    fn raw_keys(&self, section: usize) -> bool {
        // The editor takes every key but the channel keys (alt+1–9) and ctrl+].
        section == NOTE && self.editor.is_some() && self.popup.is_none()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.popup.is_some() {
            self.popup_key(&key, ctx);
            return Outcome::Used;
        }
        if section == NOTE && self.editor.is_some() {
            if keys::ctrl(&key, 's') {
                self.save(ctx);
                return Outcome::Used;
            }
            let event = self.editor.as_mut().map_or(vim::Event::None, |ed| ed.key(&key));
            match event {
                vim::Event::None => {}
                vim::Event::Command(cmd) => {
                    if matches!(cmd.as_str(), "w" | "wq" | "x") {
                        self.save(ctx);
                    }
                    if matches!(cmd.as_str(), "q" | "wq" | "x" | "q!") {
                        self.leave_editor(cmd != "q!", ctx);
                    }
                }
                vim::Event::Leave => self.leave_editor(true, ctx),
                vim::Event::LinkWanted => self.picker("LINK TO", PickFor::Link),
                vim::Event::NotFound(n) => ctx.toast("VAULT", &format!("not found: {n}")),
            }
            return Outcome::Used;
        }
        match section {
            EXPLORER => {
                let page = self.rects[EXPLORER].height as usize;
                match Self::list_key(&self.entries, &mut self.entry_at, &key, page) {
                    Some(Some(route)) => self.follow_route(route, ctx),
                    Some(None) => {}
                    None => return self.common(&key, ctx),
                }
                Outcome::Used
            }
            SIDE => {
                let page = self.rects[SIDE].height as usize;
                match Self::list_key(&self.side, &mut self.side_at, &key, page) {
                    Some(Some(route)) => self.follow_route(route, ctx),
                    Some(None) => {}
                    None => return self.common(&key, ctx),
                }
                Outcome::Used
            }
            NOTE => {
                match (keys::ch(&key), key.code) {
                    (Some('i'), _) => self.edit(ctx),
                    (_, KeyCode::Down) => self.scroll += 1,
                    (_, KeyCode::Up) => self.scroll = self.scroll.saturating_sub(1),
                    (_, KeyCode::PageDown) | (Some(' '), _) => self.scroll += self.page.saturating_sub(1),
                    (_, KeyCode::PageUp) => self.scroll = self.scroll.saturating_sub(self.page.saturating_sub(1)),
                    (Some('g'), _) | (_, KeyCode::Home) => self.scroll = 0,
                    (Some('G'), _) | (_, KeyCode::End) => self.scroll = usize::MAX, // clamped when drawn
                    _ => return self.common(&key, ctx),
                }
                Outcome::Used
            }
            _ => Outcome::Ignored,
        }
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.common(&key, ctx)
    }

    fn paste(&mut self, section: usize, text: &str, _ctx: &mut Ctx) {
        match &mut self.popup {
            Some(Popup::Picker { input, .. }) => input.insert(text),
            Some(Popup::Search(a) | Popup::NewNote(a) | Popup::Rename(a)) => a.input.insert(text),
            None if section == NOTE => {
                if let Some(ed) = &mut self.editor {
                    ed.insert(text);
                }
            }
            None => {}
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        match (event.kind, section) {
            (MouseEventKind::Down(MouseButton::Left), Some(s @ (EXPLORER | SIDE))) => {
                let r = self.rects[s];
                let (list, at) =
                    if s == EXPLORER { (&self.entries, self.entry_at) } else { (&self.side, self.side_at) };
                let top = (at + 1).saturating_sub(r.height.saturating_sub(2) as usize);
                let i = top + event.row.saturating_sub(r.y + 1) as usize;
                if let Some(route) = list.get(i).and_then(|e| e.route.clone()) {
                    if s == EXPLORER {
                        self.entry_at = i;
                    } else {
                        self.side_at = i;
                    }
                    self.follow_route(route, ctx);
                }
            }
            (MouseEventKind::ScrollDown, Some(NOTE)) if self.editor.is_none() => self.scroll += 3,
            (MouseEventKind::ScrollUp, Some(NOTE)) if self.editor.is_none() => {
                self.scroll = self.scroll.saturating_sub(3)
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn tick(&mut self, _now: std::time::Instant, visible: bool, ctx: &mut Ctx) {
        // The vault is read when VAULT is first shown (a 3,000-note scan nobody asked for at boot).
        if visible && !self.started {
            self.started = true;
            self.load_vault(0, None, ctx);
        }
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let msg = match msg.downcast::<OpenNote>() {
            Ok(open) => {
                // From another channel (LIFE's v / p): maybe the other vault first.
                let at = self.vaults.iter().position(|(n, _)| *n == open.vault);
                match at {
                    Some(i) if self.vault_name != open.vault || self.vault.is_none() => {
                        self.started = true;
                        self.load_vault(i, Some((open.rel, open.line)), ctx);
                    }
                    _ => self.open_note(&open.rel, open.line, true, ctx),
                }
                ctx.focus(NOTE);
                return;
            }
            Err(other) => other,
        };
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Loaded { name, vault, then } => {
                self.loading = false;
                match vault {
                    None => {
                        let root = self
                            .vaults
                            .iter()
                            .find(|(n, _)| *n == name)
                            .map(|(_, r)| r.display().to_string())
                            .unwrap_or_default();
                        self.message = format!("✖ {name} isn't at {root}");
                    }
                    Some(v) => {
                        self.vault = Some(v);
                        self.vault_name = name;
                        self.folder.clear();
                        self.explorer = Explorer::Files;
                        self.history.clear();
                        let pal = ctx.pal;
                        self.build_explorer(&pal);
                        if let Some((rel, line)) = then {
                            self.open_note(&rel, line, false, ctx);
                        }
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn obsidian_extras_become_markdown() {
        let md = obsidian_to_markdown(
            "---\ntags: [a]\n---\n> [!warning] Careful\nsee [[Note#Part|that]] and ![[pic.png]] #todo ==big==\n```\n[[kept]]\n```",
        );
        assert!(md.starts_with("> **tags**: a"));
        assert!(md.contains("> **⚠ Careful**"));
        assert!(md.contains("[that](wiki:)") && md.contains("🖼 [pic.png](wiki:)"));
        assert!(md.contains("`#todo`") && md.contains("**big**"));
        assert!(md.contains("[[kept]]"), "code stays as written");
    }
}
