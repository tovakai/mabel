//! CH-05 VAULT: his Obsidian vault in MABEL-OS.
//!
//! Left: the explorer (folders drill down; / searches contents; # tags; r recent). Centre: the
//! note, rendered (wiki links, callouts, tags, properties) or in the vim editor (e, or i inside).
//! Right: outline, backlinks, links, the note's tags. Keys: o quick open · f follow a link ·
//! D today's daily note · n new note · R rename · V switch vault (core of truth / Mabel's
//! journal) · M ask Mabel about this note · O outline on/off · backspace back.

pub mod notes; // reading the vault: notes, links, tags, frontmatter

use std::path::PathBuf;
use std::sync::LazyLock;

use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use regex::Regex;

use crate::channels::vault::notes::{INLINE_TAG, Note, Vault, WIKILINK, split_frontmatter};
use crate::os::channel::{AskAbout, Channel, Ctx, OpenNote, Outcome, Request, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::markdown;
use crate::widgets::popup::{Answer, Ask, frame_box};
use crate::widgets::vim::{self, Vim};

const ID: &str = "vault";
const EXPLORER: usize = 0;
const NOTE: usize = 1;
const SIDE: usize = 2;

static CALLOUT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(>\s*)\[!(\w+)\]([+-]?)\s*(.*)$").expect("regex"));
static HIGHLIGHT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"==([^=]+)==").expect("regex"));

fn icon(kind: &str) -> &'static str {
    match kind {
        "note" => "✎",
        "info" => "ℹ",
        "tip" => "✦",
        "todo" => "☐",
        "warning" => "⚠",
        "danger" => "⚡",
        "bug" | "failure" => "✖",
        "example" => "▤",
        "quote" => "❝",
        "success" => "✓",
        "question" => "?",
        "abstract" => "≡",
        _ => "◆",
    }
}

/// Obsidian's extras as plain Markdown: properties as a quote, callouts with an icon, [[links]]
/// by their label, #tags as code, ==highlights== bold.
pub fn obsidian_to_markdown(text: &str) -> String {
    let (fm, body, _) = split_frontmatter(text);
    let link = |m: &regex::Captures| {
        let head = m.get(3).map_or("", |h| h.as_str());
        let label = m.get(4).map(|a| a.as_str().to_string()).unwrap_or_else(|| {
            if m[2].is_empty() { head.trim_start_matches('#').to_string() } else { format!("{}{head}", &m[2]) }
        });
        if &m[1] == "!" { format!("🖼 [{label}](wiki:)") } else { format!("[{label}](wiki:)") }
    };
    let mut out = vec![];
    if !fm.is_empty() {
        let props: Vec<String> = fm
            .iter()
            .take(14)
            .map(|(k, v)| {
                let value = v.join(", ");
                let value = if value.chars().count() > 140 {
                    format!("{}…", value.chars().take(139).collect::<String>())
                } else {
                    value
                };
                WIKILINK.replace_all(&format!("**{k}**: {value}"), link).into_owned()
            })
            .collect();
        out.push(format!("> {}\n", props.join("  \n> ")));
    }
    let mut fenced = false;
    for line in body.lines() {
        let t = line.trim_start();
        if t.starts_with("```") || t.starts_with("~~~") {
            fenced = !fenced;
            out.push(line.to_string());
            continue;
        }
        if fenced {
            out.push(line.to_string());
            continue;
        }
        let mut line = line.to_string();
        if let Some(m) = CALLOUT.captures(&line) {
            let kind = m[2].to_lowercase();
            let title = if m[4].is_empty() { kind.to_uppercase() } else { m[4].to_string() };
            line = format!("{}**{} {title}**", &m[1], icon(&kind));
        }
        line = WIKILINK.replace_all(&line, link).into_owned();
        line = INLINE_TAG.replace_all(&line, "$1`#$2`").into_owned();
        line = HIGHLIGHT.replace_all(&line, "**$1**").into_owned();
        out.push(line);
    }
    out.join("\n")
}

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

#[derive(Clone)]
struct Entry {
    line: Line<'static>,
    route: Option<Route>,
}

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
    Open,
    Link,
    Follow,
}

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

enum Got {
    Loaded { name: String, vault: Option<Vault>, then: Option<(String, Option<usize>)> },
}

pub struct VaultView {
    vaults: Vec<(String, PathBuf)>,
    vault: Option<Vault>,
    vault_name: String,
    loading: bool,
    message: String,
    note: Option<String>,
    text: String,
    rendered: Option<(u16, Vec<Line<'static>>)>,
    scroll: usize,
    /// Scroll to about this file line once the note is drawn (search hits, outline).
    jump_to: Option<usize>,
    page: usize,
    editor: Option<Vim>,
    folder: String,
    explorer: Explorer,
    entries: Vec<Entry>,
    entry_at: usize,
    side: Vec<Entry>,
    side_at: usize,
    side_shown: Option<bool>,
    history: Vec<String>,
    popup: Option<Popup>,
    rects: Vec<Rect>,
    started: bool,
}

impl VaultView {
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

    // ── the explorer and the side panel ──────────────────────────────────

    fn note_entry(&self, note: &Note, with_path: bool, pal: &Palette) -> Entry {
        let open = self.note.as_deref() == Some(note.rel.as_str());
        let mut spans = vec![
            Span::styled(if open { "▶ " } else { "• " }, Style::new().fg(pal.amber_dim)),
            Span::styled(note.name.clone(), Style::new().fg(pal.amber)),
        ];
        if with_path && !note.folder().is_empty() {
            spans.push(Span::styled(format!("  {}", note.folder()), Style::new().fg(pal.beige_dark)));
        }
        Entry { line: Line::from(spans), route: Some(Route::Note(note.rel.clone(), None)) }
    }

    fn heading(text: String, pal: &Palette) -> Entry {
        Entry { line: Line::styled(text, bold(pal.beige)), route: None }
    }

    fn build_explorer(&mut self, pal: &Palette) {
        let Some(v) = &self.vault else { return };
        let mut entries = vec![];
        match &self.explorer {
            Explorer::Files => {
                let prefix = if self.folder.is_empty() { String::new() } else { format!("{}/", self.folder) };
                let mut folders: Vec<(String, usize)> = vec![];
                let mut notes: Vec<&Note> = vec![];
                for (rel, note) in &v.notes {
                    let Some(rest) = rel.strip_prefix(&prefix) else { continue };
                    match rest.split_once('/') {
                        Some((f, _)) => match folders.iter_mut().find(|(n, _)| n == f) {
                            Some((_, c)) => *c += 1,
                            None => folders.push((f.to_string(), 1)),
                        },
                        None => notes.push(note),
                    }
                }
                entries.push(Self::heading(
                    if self.folder.is_empty() { "▸ /".into() } else { format!("▸ {}", self.folder) },
                    pal,
                ));
                if !self.folder.is_empty() {
                    let up = self.folder.rsplit_once('/').map_or(String::new(), |(f, _)| f.to_string());
                    entries.push(Entry {
                        line: Line::styled("◂ ..", Style::new().fg(pal.amber_dim)),
                        route: Some(Route::Dir(up)),
                    });
                }
                folders.sort_by_key(|(f, _)| f.to_lowercase());
                for (f, count) in folders {
                    entries.push(Entry {
                        line: Line::from(vec![
                            Span::styled("▤ ", Style::new().fg(pal.amber_dim)),
                            Span::styled(f.clone(), Style::new().fg(pal.amber_hi)),
                            Span::styled(format!("  {count}"), Style::new().fg(pal.beige_dark)),
                        ]),
                        route: Some(Route::Dir(format!("{prefix}{f}"))),
                    });
                }
                notes.sort_by_key(|n| n.name.to_lowercase());
                for n in notes {
                    entries.push(self.note_entry(n, false, pal));
                }
            }
            Explorer::Search(q) => {
                let hits = v.search(q, 100);
                entries.push(Self::heading(format!("“{q}” · {} lines", hits.len()), pal));
                for (rel, line, text) in hits {
                    let name = v.notes.get(&rel).map_or(rel.clone(), |n| n.name.clone());
                    entries.push(Entry {
                        line: Line::from(vec![
                            Span::styled(name, Style::new().fg(pal.amber_hi)),
                            Span::styled(format!(" :{}  ", line + 1), Style::new().fg(pal.beige_dark)),
                            Span::styled(text.chars().take(80).collect::<String>(), Style::new().fg(pal.amber)),
                        ]),
                        route: Some(Route::Note(rel, Some(line))),
                    });
                }
            }
            Explorer::Tags => {
                entries.push(Self::heading("TAGS".into(), pal));
                for (tag, notes) in v.tags() {
                    entries.push(Entry {
                        line: Line::from(vec![
                            Span::styled(format!("#{tag}"), Style::new().fg(pal.teal)),
                            Span::styled(format!("  {}", notes.len()), Style::new().fg(pal.beige_dark)),
                        ]),
                        route: Some(Route::Tag(tag)),
                    });
                }
            }
            Explorer::Tag(tag) => {
                let mut notes: Vec<&Note> = v.notes.values().filter(|n| n.tags.contains(tag)).collect();
                notes.sort_by_key(|n| n.name.to_lowercase());
                entries.push(Entry {
                    line: Line::styled(format!("#{tag} · {}", notes.len()), bold(pal.teal)),
                    route: None,
                });
                for n in notes {
                    entries.push(self.note_entry(n, true, pal));
                }
            }
            Explorer::Recent => {
                entries.push(Self::heading("RECENT".into(), pal));
                for n in v.find_notes("", 40) {
                    entries.push(self.note_entry(n, true, pal));
                }
            }
        }
        self.entries = entries;
        self.entry_at = self.entries.iter().position(|e| e.route.is_some()).unwrap_or(0);
    }

    fn build_side(&mut self, pal: &Palette) {
        let (Some(v), Some(rel)) = (&self.vault, &self.note) else {
            self.side.clear();
            return;
        };
        let Some(note) = v.notes.get(rel) else { return };
        let mut side = vec![Self::heading("OUTLINE".into(), pal)];
        for (level, text, line) in &note.headings {
            let colour = if *level <= 2 { pal.amber_hi } else { pal.amber };
            side.push(Entry {
                line: Line::styled(format!("{}{text}", "  ".repeat(level - 1)), Style::new().fg(colour)),
                route: Some(Route::Heading(*line)),
            });
        }
        if note.headings.is_empty() {
            side.push(Entry { line: Line::styled("no headings", Style::new().fg(pal.amber_faint)), route: None });
        }
        let back = v.backlinks(note);
        side.push(Entry { line: Line::raw(""), route: None });
        side.push(Self::heading(format!("BACKLINKS · {}", back.len()), pal));
        let back_entries: Vec<Entry> = back.iter().take(60).map(|n| self.note_entry(n, true, pal)).collect();
        side.extend(back_entries);
        let mut links: Vec<&String> = vec![];
        for l in &note.links {
            if !links.contains(&l) {
                links.push(l);
            }
        }
        side.push(Entry { line: Line::raw(""), route: None });
        side.push(Self::heading(format!("LINKS · {}", links.len()), pal));
        for target in links.into_iter().take(60) {
            let found = v.resolve(target, Some(note));
            let colour = if found.is_some() { pal.amber } else { pal.amber_faint };
            let route = match found {
                Some(rel) => Route::Note(rel, None),
                None => Route::Missing(target.clone()),
            };
            side.push(Entry { line: Line::styled(target.clone(), Style::new().fg(colour)), route: Some(route) });
        }
        if !note.tags.is_empty() {
            side.push(Entry { line: Line::raw(""), route: None });
            side.push(Self::heading("TAGS".into(), pal));
            let mut tags = note.tags.clone();
            tags.sort();
            for t in tags {
                side.push(Entry {
                    line: Line::styled(format!("#{t}"), Style::new().fg(pal.teal)),
                    route: Some(Route::Tag(t)),
                });
            }
        }
        self.side = side;
        self.side_at = self.side.iter().position(|e| e.route.is_some()).unwrap_or(0);
    }

    // ── the note ─────────────────────────────────────────────────────────

    fn open_note(&mut self, target: &str, line: Option<usize>, remember: bool, ctx: &mut Ctx) {
        let Some(v) = &self.vault else { return };
        let found = if v.notes.contains_key(target) { Some(target.to_string()) } else { v.resolve(target, None) };
        let Some(rel) = found else {
            ctx.toast("VAULT", &format!("No note “{target}” in {}", self.vault_name));
            return;
        };
        if self.editor.is_some() {
            self.leave_editor(true, ctx);
        }
        if remember && let Some(cur) = self.note.clone().filter(|c| *c != rel) {
            self.history.push(cur);
        }
        self.text = self.vault.as_ref().map(|v| v.read(&rel)).unwrap_or_default();
        self.note = Some(rel);
        self.rendered = None;
        self.scroll = 0;
        self.jump_to = line;
        let pal = ctx.pal;
        self.build_side(&pal);
        if self.explorer == Explorer::Files || matches!(self.explorer, Explorer::Recent | Explorer::Tag(_)) {
            let at = self.entry_at;
            self.build_explorer(&pal);
            self.entry_at = at.min(self.entries.len().saturating_sub(1));
        }
    }

    fn edit(&mut self, ctx: &mut Ctx) {
        let (Some(v), Some(rel)) = (&self.vault, &self.note) else { return };
        self.text = v.read(rel);
        self.editor = Some(Vim::new(&self.text));
        ctx.focus(NOTE);
    }

    fn save(&mut self, ctx: &mut Ctx) {
        let (Some(v), Some(rel), Some(ed)) = (&mut self.vault, &self.note, &mut self.editor) else { return };
        let text = ed.text();
        match v.write(rel, &text) {
            Ok(()) => {
                ed.dirty = false;
                self.text = text;
                self.rendered = None;
                let name = v.notes.get(rel).map_or(rel.clone(), |n| n.name.clone());
                ctx.toast("VAULT", &format!("✓ saved {name}"));
            }
            Err(e) => ctx.error("VAULT", &format!("couldn't save: {e}")),
        }
        let pal = ctx.pal;
        self.build_side(&pal);
    }

    /// Back to reading. Like Obsidian, typing is never lost: unsaved text is saved (unless :q!).
    fn leave_editor(&mut self, save: bool, ctx: &mut Ctx) {
        if save && self.editor.as_ref().is_some_and(|e| e.dirty) {
            self.save(ctx);
        }
        self.editor = None;
        self.rendered = None;
    }

    fn follow_route(&mut self, route: Route, ctx: &mut Ctx) {
        let pal = ctx.pal;
        match route {
            Route::Dir(d) => {
                self.explorer = Explorer::Files;
                self.folder = d;
                self.build_explorer(&pal);
            }
            Route::Tag(t) => {
                self.explorer = Explorer::Tag(t);
                self.build_explorer(&pal);
                ctx.focus(EXPLORER);
            }
            Route::Note(rel, line) => self.open_note(&rel, line, true, ctx),
            Route::Missing(t) => ctx.toast("VAULT", &format!("“{t}” isn't a note yet (n makes one)")),
            Route::Heading(line) => match &mut self.editor {
                Some(ed) => ed.goto_line(line),
                None => self.jump_to = Some(line),
            },
        }
    }

    fn back(&mut self, ctx: &mut Ctx) {
        if self.editor.is_some() {
            return;
        }
        if let Some(prev) = self.history.pop() {
            self.open_note(&prev, None, false, ctx);
        } else if self.explorer != Explorer::Files || !self.folder.is_empty() {
            self.explorer = Explorer::Files;
            self.folder = self.folder.rsplit_once('/').map_or(String::new(), |(f, _)| f.to_string());
            let pal = ctx.pal;
            self.build_explorer(&pal);
        }
    }

    // ── pickers and questions ────────────────────────────────────────────

    fn choices(&self, purpose: &PickFor, query: &str) -> Vec<(String, String)> {
        let Some(v) = &self.vault else { return vec![] };
        match purpose {
            PickFor::Follow => {
                let note = self.note.as_ref().and_then(|r| v.notes.get(r));
                let mut out: Vec<(String, String)> = vec![];
                for t in note.map(|n| n.links.clone()).unwrap_or_default() {
                    if t.to_lowercase().contains(&query.to_lowercase()) && !out.iter().any(|(k, _)| *k == t) {
                        out.push((t.clone(), t));
                    }
                }
                out
            }
            _ => v
                .find_notes(query, 60)
                .into_iter()
                .map(|n| (n.rel.clone(), format!("{}   {}", n.name, n.folder())))
                .collect(),
        }
    }

    fn picker(&mut self, title: &str, purpose: PickFor) {
        let options = self.choices(&purpose, "");
        self.popup =
            Some(Popup::Picker { title: title.into(), input: Input::new("type to filter"), options, at: 0, purpose });
    }

    fn picked(&mut self, purpose: PickFor, choice: Option<String>, ctx: &mut Ctx) {
        match (purpose, choice) {
            (PickFor::Open, Some(rel)) => self.open_note(&rel, None, true, ctx),
            (PickFor::Follow, Some(target)) => {
                let note = self.note.as_ref().and_then(|r| self.vault.as_ref()?.notes.get(r)).cloned();
                let found = self.vault.as_ref().and_then(|v| v.resolve(&target, note.as_ref()));
                self.open_note(found.as_deref().unwrap_or(&target), None, true, ctx);
            }
            (PickFor::Link, choice) => {
                let name = choice.and_then(|rel| self.vault.as_ref()?.notes.get(&rel).map(|n| n.name.clone()));
                if let (Some(ed), Some(name)) = (&mut self.editor, name) {
                    ed.insert(&format!("{name}]]"));
                }
                ctx.focus(NOTE);
            }
            _ => {}
        }
    }

    fn popup_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) {
        let Some(popup) = self.popup.take() else { return };
        match popup {
            Popup::Picker { title, mut input, mut options, mut at, purpose } => {
                match key.code {
                    KeyCode::Esc => return self.picked(purpose, None, ctx),
                    KeyCode::Enter => {
                        let choice = options.get(at).map(|(k, _)| k.clone());
                        return self.picked(purpose, choice, ctx);
                    }
                    KeyCode::Down => at = (at + 1).min(options.len().saturating_sub(1)),
                    KeyCode::Up => at = at.saturating_sub(1),
                    _ => {
                        if input.key(key) == InputResult::Used {
                            options = self.choices(&purpose, &input.value);
                            at = 0;
                        }
                    }
                }
                self.popup = Some(Popup::Picker { title, input, options, at, purpose });
            }
            Popup::Search(mut ask) => match ask.key(key) {
                Answer::Open => self.popup = Some(Popup::Search(ask)),
                Answer::Cancel => {}
                Answer::Chosen(q) if !q.is_empty() => {
                    self.explorer = Explorer::Search(q);
                    let pal = ctx.pal;
                    self.build_explorer(&pal);
                    ctx.focus(EXPLORER);
                }
                Answer::Chosen(_) => {}
            },
            Popup::NewNote(mut ask) => match ask.key(key) {
                Answer::Open => self.popup = Some(Popup::NewNote(ask)),
                Answer::Chosen(name) if !name.is_empty() => {
                    let folder = if self.explorer == Explorer::Files && !self.folder.is_empty() {
                        format!("{}/", self.folder)
                    } else {
                        String::new()
                    };
                    let title = name.rsplit('/').next().unwrap_or(&name).to_string();
                    let created =
                        self.vault.as_mut().map(|v| v.create(&format!("{folder}{name}"), &format!("# {title}\n\n")));
                    match created {
                        Some(Ok(rel)) => {
                            self.open_note(&rel, None, true, ctx);
                            self.edit(ctx);
                        }
                        Some(Err(e)) => ctx.error("VAULT", &e.to_string()),
                        None => {}
                    }
                }
                _ => {}
            },
            Popup::Rename(mut ask) => match ask.key(key) {
                Answer::Open => self.popup = Some(Popup::Rename(ask)),
                Answer::Chosen(name) if !name.is_empty() => {
                    let Some(rel) = self.note.clone() else { return };
                    let renamed = self.vault.as_mut().map(|v| v.rename(&rel, &name));
                    match renamed {
                        Some(Ok((new_rel, changed))) => {
                            ctx.toast("VAULT", &format!("✓ renamed; links updated in {changed} note(s)"));
                            self.open_note(&new_rel, None, false, ctx);
                        }
                        Some(Err(e)) => ctx.error("VAULT", &e.to_string()),
                        None => {}
                    }
                }
                _ => {}
            },
        }
    }

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

    fn list_key(list: &[Entry], at: &mut usize, key: &KeyEvent, page: usize) -> Option<Option<Route>> {
        let selectable: Vec<usize> =
            list.iter().enumerate().filter(|(_, e)| e.route.is_some()).map(|(i, _)| i).collect();
        if selectable.is_empty() {
            return None;
        }
        let pos = selectable.iter().position(|i| i == at).unwrap_or(0);
        let go = |p: isize| selectable[p.clamp(0, selectable.len() as isize - 1) as usize];
        match key.code {
            KeyCode::Down => *at = go(pos as isize + 1),
            KeyCode::Up => *at = go(pos as isize - 1),
            KeyCode::PageDown => *at = go(pos as isize + page as isize),
            KeyCode::PageUp => *at = go(pos as isize - page as isize),
            KeyCode::Home => *at = selectable[0],
            KeyCode::End => *at = *selectable.last().unwrap_or(&0),
            KeyCode::Enter => return Some(list.get(*at).and_then(|e| e.route.clone())),
            _ => return None,
        }
        Some(None)
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn draw_list(frame: &mut Frame, area: Rect, list: &[Entry], at: usize, focused: bool, pal: &Palette) {
        let h = area.height as usize;
        let top = (at + 1).saturating_sub(h);
        let lines: Vec<Line> = list
            .iter()
            .enumerate()
            .skip(top)
            .take(h)
            .map(|(i, e)| {
                let mut line = e.line.clone();
                if i == at && e.route.is_some() {
                    line = line.style(if focused {
                        Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
                    } else {
                        Style::new().bg(pal.panel)
                    });
                }
                line
            })
            .collect();
        frame.render_widget(Paragraph::new(lines), area);
    }

    fn render_note(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let v = self.vault.as_ref();
        let note = self.note.as_ref().and_then(|r| v?.notes.get(r));
        let title = note.map_or("NOTE".to_string(), |n| {
            format!("NOTE ░ {}", n.name.to_uppercase().chars().take(44).collect::<String>())
        });
        let block = ui::panel(pal, &title, view.panel_state(NOTE));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [meta, body, status] =
            Layout::vertical([Constraint::Length(2), Constraint::Fill(1), Constraint::Length(1)]).areas(inner);
        let Some(note) = note.cloned() else {
            let msg = if self.loading {
                "loading the vault…".to_string()
            } else if !self.message.is_empty() {
                self.message.clone()
            } else {
                let n = v.map_or(0, |v| v.notes.len());
                format!("{n} notes · o open · / search · # tags · D today's note")
            };
            frame.render_widget(Paragraph::new(Line::styled(msg, Style::new().fg(pal.beige_dark))), meta);
            return;
        };
        let mut meta_spans = vec![Span::styled(note.rel.clone(), Style::new().fg(pal.beige_dark))];
        if !note.tags.is_empty() {
            let mut tags = note.tags.clone();
            tags.sort();
            meta_spans.push(Span::styled(
                format!("   {}", tags.iter().map(|t| format!("#{t}")).collect::<Vec<_>>().join(" ")),
                Style::new().fg(pal.teal),
            ));
        }
        frame.render_widget(Paragraph::new(Line::from(meta_spans)), meta);

        if let Some(ed) = &mut self.editor {
            ed.render(frame, body, pal, view.inside(NOTE));
            let dirty = if ed.dirty {
                Span::styled("   ● unsaved", Style::new().fg(pal.orange))
            } else {
                Span::styled("   saved", Style::new().fg(pal.beige_dark))
            };
            frame.render_widget(
                Paragraph::new(Line::from(vec![
                    Span::styled(ed.status(), bold(pal.amber_hi)),
                    dirty,
                    Span::styled("   ctrl+s save · esc esc back to reading", Style::new().fg(pal.amber_faint)),
                ])),
                status,
            );
            return;
        }
        let width = body.width.saturating_sub(1).max(10);
        if self.rendered.as_ref().is_none_or(|(w, _)| *w != width) {
            let md = obsidian_to_markdown(&self.text);
            self.rendered = Some((width, markdown::render(&md, width, pal, Style::new().fg(pal.amber))));
        }
        let lines = &self.rendered.as_ref().expect("just rendered").1;
        self.page = body.height as usize;
        if let Some(line) = self.jump_to.take() {
            // A file line: about that far through the rendered note.
            let total = self.text.lines().count().max(1);
            self.scroll = lines.len() * line / total;
        }
        self.scroll = self.scroll.min(lines.len().saturating_sub(body.height as usize));
        let shown: Vec<Line> = lines.iter().skip(self.scroll).take(body.height as usize).cloned().collect();
        frame.render_widget(Paragraph::new(shown), body);
        frame.render_widget(
            Paragraph::new(Line::styled(
                "read · e edit · f follow a link · M ask Mabel",
                Style::new().fg(pal.amber_faint),
            )),
            status,
        );
    }

    fn render_popup(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        match &mut self.popup {
            Some(Popup::Picker { title, input, options, at, .. }) => {
                let h = (options.len() as u16 + 4).clamp(8, area.height * 7 / 10);
                let inner = frame_box(frame, area, pal, title, "enter picks · esc cancels", 70, h);
                let [q, _, list] =
                    Layout::vertical([Constraint::Length(1), Constraint::Length(1), Constraint::Fill(1)]).areas(inner);
                input.render(frame, q, Style::new().fg(pal.amber_hi), Style::new().fg(pal.amber_dim), true);
                let rows = list.height as usize;
                let top = (*at + 1).saturating_sub(rows);
                let lines: Vec<Line> = options
                    .iter()
                    .enumerate()
                    .skip(top)
                    .take(rows)
                    .map(|(i, (_, label))| {
                        let style = if i == *at {
                            Style::new().bg(pal.amber_faint).fg(pal.amber_hi)
                        } else {
                            Style::new().fg(pal.amber)
                        };
                        Line::styled(label.clone(), style)
                    })
                    .collect();
                frame.render_widget(Paragraph::new(lines), list);
            }
            Some(Popup::Search(ask) | Popup::NewNote(ask) | Popup::Rename(ask)) => ask.render(frame, area, pal),
            None => {}
        }
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
        // The editor takes every key but the channel keys (alt+1–8) and ctrl+].
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
