//! CH-04 INVENTORY: the Gremlin Index, zoomed like its web UI (home → room → furniture → place →
//! spot).
//!
//! Left: where you are (breadcrumbs), place cards to zoom into, the place drawn as a map
//! (Gridfinity grids and pegboards as boxes with their footprints; drawers and shelves as levels;
//! sorted shelves; spot lists) and the things there. Right: the item card. Writes go to tk-index
//! (undoable there).
//!
//! Keys: enter zoom/open · backspace up · / search · H home · b Mabel's housekeeping inbox ·
//! o loose ends · on an item: e edit, m move, c check out, r return, k nickname, p its place ·
//! u undo · g reload · in the inbox: a apply, x tick off, v open the note in VAULT.

/// the daily housekeeping rounds
pub mod housekeeping;
/// the Gremlin Index's places and items
pub mod tkindex;

use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use serde_json::{Value, json};

use crate::channels::inventory::housekeeping::{self as hk, Suggestion};
use crate::channels::inventory::tkindex::{Card, Item, Page, TkIndex, World, Zone, pos};
use crate::os::channel::{Channel, Ctx, OpenNote, Outcome, Request, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, PanelState, bold};
use crate::widgets::form::{Field, Form, Kind};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::picture::Picture;
use crate::widgets::popup::Answer;
use crate::widgets::wrap::{self, width};

// INVENTORY is split by job; the pieces add to `impl Inventory`.
/// actions, keys, popups
mod actions;
/// the item card
mod card;
/// layout and drawing
mod draw;
/// the grid map
mod map;
/// the page as blocks
mod page;

/// The id messages and help use.
const ID: &str = "inventory";
// The three sections, in the order `render` returns them.
const SEARCH: usize = 0;
const BODY: usize = 1;
const ITEM: usize = 2;
/// Lines per place card (the boxes you zoom into).
const CARD_H: usize = 4;

/// A colour per category, stable across runs (the web UI's trick: a hash into a few colours).
fn hue(category: Option<&str>, pal: &Palette) -> Color {
    let colours = [pal.amber, pal.amber_hi, pal.teal, pal.beige, pal.orange, pal.sand, pal.mint];
    colours[(crc32(category.unwrap_or("?").as_bytes()) % colours.len() as u32) as usize]
}

/// CRC-32 (IEEE), as Python's zlib.crc32: the same category gets the same colour in both clients.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for b in data {
        crc ^= *b as u32;
        for _ in 0..8 {
            crc = if crc & 1 != 0 { (crc >> 1) ^ 0xEDB8_8320 } else { crc >> 1 };
        }
    }
    !crc
}

/// What the page shows.
#[derive(Clone, PartialEq)]
enum Mode {
    /// The place at `route`.
    Browse,
    /// Search results for the text.
    Search(String),
    /// b: Mabel's housekeeping suggestions.
    Inbox,
    /// o: loose ends (things without a proper home).
    Loose,
}

/// What enter (or a click) on something does.
#[derive(Clone, Debug)]
enum Target {
    /// Go into the place at this route.
    Zoom(String),
    /// Show this item on the card.
    Pick(String),
    /// A housekeeping suggestion (index into `suggestions`): enter opens its item.
    Suggest(usize),
    /// Show the loose ends.
    Loose,
}

/// The body, top to bottom, before it's laid out at a width.
enum Block {
    /// A plain line.
    Text(Line<'static>),
    /// A row of place cards.
    Cards(Vec<Card>),
    /// A selectable entry (one or more lines).
    Select(Vec<Line<'static>>, Target),
    /// A grid zone of the page, drawn as a map.
    Map(usize),
}

/// A form over the page, for the item with this id: edit, move, check out, nickname.
enum Popup {
    Edit(Form, String),
    Move(Form, String),
    Checkout(Form, String),
    Nickname(Form, String),
}

/// Background work reporting back.
enum Got {
    /// The whole index loaded (and an item to show once it has).
    World(Result<Box<World>, String>, Option<String>),
    /// One item's details.
    Item(Result<Value, String>),
    /// An item's photo (by key).
    Photo(String, Option<Vec<u8>>),
    /// A write to tk-index finished (its message).
    Wrote(Result<String, String>),
    /// A housekeeping suggestion was applied (and which one, to tick off).
    Applied(Result<String, String>, Option<usize>),
}

/// The edit form's fields: (tk-index's field, label, kind: "line", "text", or `|`-separated choices).
const EDIT_FIELDS: [(&str, &str, &str); 13] = [
    ("name", "Name", "line"),
    ("spec_code", "Spec ID", "line"),
    ("category", "Category", "line"),
    ("quantity", "Quantity", "line"),
    ("quantity_note", "Quantity note", "line"),
    ("stage", "Stage", "|Placed|Review|Bagged|Needs ID"),
    ("confidence", "Confidence", "|Certain|Likely|Unknown"), // blank allowed, as in Python
    ("decision", "Decision", "|Keep|Recycle|Donate|Unsure"),
    ("search_words", "Search words (other words that find it)", "line"),
    ("sort_key", "Sort title", "line"),
    ("owner", "Owner", "line"),
    ("specs", "Specs", "text"),
    ("notes", "Notes", "text"),
];

/// A JSON value as the text a form field shows (whole numbers without ".0").
fn value_text(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Number(n) => {
            n.as_f64().map_or(n.to_string(), |f| if f.fract() == 0.0 { format!("{}", f as i64) } else { f.to_string() })
        }
        other => other.to_string(),
    }
}

/// The INVENTORY screen.
pub struct Inventory {
    /// tk-index's HTTP API (`index_url` in config.toml).
    idx: TkIndex,
    /// The vault, where housekeeping notes live.
    vault: String,
    /// Every place and item, loaded at once (the web UI's model, ported).
    world: Option<World>,
    /// Why loading failed, if it did.
    error: String,
    /// Where you are ("home/kontoret/…"), what's shown, and the page for that place.
    route: String,
    mode: Mode,
    page: Page,
    /// The page as blocks, the selectable things in it, and which is selected.
    blocks: Vec<Block>,
    targets: Vec<Target>,
    sel: usize,
    /// The first line of the page on screen, and its title.
    scroll: usize,
    title: String,
    /// Mabel's housekeeping suggestions (the inbox).
    suggestions: Vec<Suggestion>,
    search: Input,
    /// The item on the card (tk-index's JSON) and its id.
    item: Option<Value>,
    selected: Option<String>,
    /// Its photo, and whose it is.
    photo: Option<Picture>,
    photo_key: Option<String>,
    /// How far the card is scrolled.
    card_scroll: usize,
    popup: Option<Popup>,
    /// Where each target was drawn (for clicks), and the crumbs.
    hits: Vec<(Rect, usize)>,
    crumb_hits: Vec<(Rect, String)>,
    /// Where the three sections were drawn (mouse clicks use them).
    rects: Vec<Rect>,
    /// How many lines the page showed last time (PgUp/PgDn move by half of it).
    page_h: usize,
}

impl Inventory {
    /// An empty INVENTORY; `start` loads the index.
    pub fn new(index_url: &str, vault: &str) -> Self {
        Inventory {
            idx: TkIndex::new(index_url),
            vault: vault.into(),
            world: None,
            error: String::new(),
            route: String::new(),
            mode: Mode::Browse,
            page: Page::default(),
            blocks: vec![],
            targets: vec![],
            sel: 0,
            scroll: 0,
            title: "INVENTORY".into(),
            suggestions: vec![],
            search: Input::new("/ search the Gremlin Index (names, nicknames, spec IDs, svenska)"),
            item: None,
            selected: None,
            photo: None,
            photo_key: None,
            card_scroll: 0,
            popup: None,
            hits: vec![],
            crumb_hits: vec![],
            rects: vec![Rect::default(); 3],
            page_h: 10,
        }
    }

    // ── loading ──────────────────────────────────────────────────────────

    /// Load the whole index in the background (`then_item`: show this item once it's in).
    fn load_world(&self, then_item: Option<String>, ctx: &Ctx) {
        let idx = self.idx.clone();
        ctx.run(ID, async move { Got::World(idx.world().await.map(Box::new).map_err(|e| e.to_string()), then_item) });
    }

    /// Put an item on the card (its details arrive in the background).
    fn show_item(&mut self, id: &str, ctx: &Ctx) {
        self.selected = Some(id.to_string());
        let (idx, id) = (self.idx.clone(), id.to_string());
        ctx.run(ID, async move { Got::Item(idx.item(&id).await.map_err(|e| e.to_string())) });
    }
}

impl Channel for Inventory {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "INVENTORY"
    }

    fn start(&mut self, ctx: &mut Ctx) {
        self.load_world(None, ctx);
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let (main, item) = if view.narrow {
            let [m, i] = Layout::vertical([Constraint::Fill(3), Constraint::Fill(2)]).areas(area);
            (m, i)
        } else {
            let [m, i] = Layout::horizontal([Constraint::Fill(3), Constraint::Fill(2)]).areas(area);
            (m, i)
        };
        let (search, body) = self.render_main(frame, main, view);
        self.render_item(frame, item, view);
        if let Some(popup) = &mut self.popup {
            let (Popup::Edit(f, _) | Popup::Move(f, _) | Popup::Checkout(f, _) | Popup::Nickname(f, _)) = popup;
            f.render(frame, area, view.pal);
        }
        self.rects = vec![search, body, item];
        self.rects.clone()
    }

    fn captures_keys(&self) -> bool {
        self.popup.is_some()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.popup.is_some() {
            self.popup_key(&key, ctx);
            return Outcome::Used;
        }
        match section {
            SEARCH => match self.search.key(&key) {
                InputResult::Submit => {
                    let q = self.search.value.trim().to_string();
                    if !q.is_empty() {
                        self.set_mode(Mode::Search(q), ctx);
                        ctx.focus(BODY);
                    }
                    Outcome::Used
                }
                InputResult::Used => Outcome::Used,
                InputResult::Ignored => Outcome::Ignored,
            },
            BODY => {
                let n = self.targets.len();
                match key.code {
                    KeyCode::Down | KeyCode::Right if n > 0 => self.sel = (self.sel + 1).min(n - 1),
                    KeyCode::Up | KeyCode::Left => self.sel = self.sel.saturating_sub(1),
                    KeyCode::PageDown if n > 0 => self.sel = (self.sel + self.page_h / 2).min(n - 1),
                    KeyCode::PageUp => self.sel = self.sel.saturating_sub(self.page_h / 2),
                    KeyCode::Home => self.sel = 0,
                    KeyCode::End => self.sel = n.saturating_sub(1),
                    KeyCode::Enter => self.activate(ctx),
                    KeyCode::Esc if self.mode != Mode::Browse => self.set_mode(Mode::Browse, ctx),
                    _ => return self.common(&key, ctx),
                }
                Outcome::Used
            }
            ITEM => {
                match key.code {
                    KeyCode::Down => self.card_scroll += 1,
                    KeyCode::Up => self.card_scroll = self.card_scroll.saturating_sub(1),
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
            Some(Popup::Edit(f, _) | Popup::Move(f, _) | Popup::Checkout(f, _) | Popup::Nickname(f, _)) => {
                f.paste(text)
            }
            None if section == SEARCH => self.search.insert(text),
            None => {}
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        let at = (event.column, event.row);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                if let Some((_, route)) = self.crumb_hits.iter().find(|(r, _)| r.contains(at.into())).cloned() {
                    self.zoom(&route, ctx);
                } else if let Some((_, t)) = self.hits.iter().find(|(r, _)| r.contains(at.into())).copied() {
                    self.sel = t;
                    self.activate(ctx);
                }
            }
            MouseEventKind::ScrollDown if section == Some(BODY) => self.scroll += 3,
            MouseEventKind::ScrollUp if section == Some(BODY) => self.scroll = self.scroll.saturating_sub(3),
            MouseEventKind::ScrollDown if section == Some(ITEM) => self.card_scroll += 3,
            MouseEventKind::ScrollUp if section == Some(ITEM) => self.card_scroll = self.card_scroll.saturating_sub(3),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::World(Ok(world), then) => {
                self.error.clear();
                if !world.valid(&self.route) {
                    self.route.clear();
                }
                self.world = Some(*world);
                self.rebuild(&ctx.pal);
                if let Some(id) = then.or_else(|| self.selected.clone()) {
                    self.show_item(&id, ctx);
                }
            }
            Got::World(Err(e), _) => self.error = e,
            Got::Item(Ok(d)) => {
                // A photo, or tk-index's icon for it.
                let path = d["images"][0]["path"].as_str().map(String::from).or_else(|| {
                    let id = value_text(&d["id"]);
                    self.world.as_ref().and_then(|w| w.icons.get(&id).cloned())
                });
                if path != self.photo_key {
                    self.photo = None;
                    self.photo_key = path.clone();
                    if let Some(p) = path {
                        let idx = self.idx.clone();
                        ctx.run(ID, async move { Got::Photo(p.clone(), idx.image(&p).await) });
                    }
                }
                self.card_scroll = 0;
                self.item = Some(d);
                if self.mode == Mode::Browse {
                    self.rebuild(&ctx.pal);
                }
            }
            Got::Item(Err(e)) => ctx.error("INVENTORY", &format!("Couldn't open the item: {e}")),
            Got::Photo(path, bytes) => {
                if self.photo_key.as_deref() == Some(path.as_str()) {
                    self.photo = bytes.as_deref().and_then(Picture::decode);
                }
            }
            Got::Wrote(Ok(done)) => {
                ctx.toast("INVENTORY", &if done.starts_with('↶') { done } else { format!("✓ {done}") });
                self.load_world(self.selected.clone(), ctx);
            }
            Got::Wrote(Err(e)) => ctx.error("INVENTORY", &e),
            Got::Applied(Ok(done), n) => {
                if let Some(s) = n.and_then(|n| self.suggestions.get(n)) {
                    hk::tick(&self.vault, s, true);
                }
                ctx.toast("INVENTORY", &done);
                self.load_world(None, ctx);
            }
            Got::Applied(Err(e), _) => ctx.error("INVENTORY", &e),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn category_colours_match_python() {
        // The standard check value: the same hash as Python's zlib.crc32, so the same colours.
        assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    }
}
