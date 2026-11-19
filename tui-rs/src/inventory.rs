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

use crossterm::event::{KeyCode, KeyEvent, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use serde_json::{Value, json};

use crate::channel::{Channel, Ctx, OpenNote, Outcome, Request, View};
use crate::housekeeping::{self as hk, Suggestion};
use crate::keys;
use crate::palette::Palette;
use crate::tkindex::{Card, Item, Page, TkIndex, World, Zone, pos};
use crate::ui::{self, PanelState, bold};
use crate::widgets::form::{Field, Form, Kind};
use crate::widgets::input::{Input, InputResult};
use crate::widgets::picture::Picture;
use crate::widgets::popup::Answer;
use crate::widgets::wrap::{self, width};

const ID: &str = "inventory";
const SEARCH: usize = 0;
const BODY: usize = 1;
const ITEM: usize = 2;
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

#[derive(Clone, PartialEq)]
enum Mode {
    Browse,
    Search(String),
    Inbox,
    Loose,
}

/// What enter (or a click) on something does.
#[derive(Clone, Debug)]
enum Target {
    Zoom(String),
    Pick(String),
    /// A housekeeping suggestion (index into `suggestions`): enter opens its item.
    Suggest(usize),
    Loose,
}

/// The body, top to bottom, before it's laid out at a width.
enum Block {
    Text(Line<'static>),
    Cards(Vec<Card>),
    /// A selectable entry (one or more lines).
    Select(Vec<Line<'static>>, Target),
    /// A grid zone of the page, drawn as a map.
    Map(usize),
}

enum Popup {
    Edit(Form, String),
    Move(Form, String),
    Checkout(Form, String),
    Nickname(Form, String),
}

enum Got {
    World(Result<Box<World>, String>, Option<String>),
    Item(Result<Value, String>),
    Photo(String, Option<Vec<u8>>),
    Wrote(Result<String, String>),
    Applied(Result<String, String>, Option<usize>),
}

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

pub struct Inventory {
    idx: TkIndex,
    vault: String,
    world: Option<World>,
    error: String,
    route: String,
    mode: Mode,
    page: Page,
    blocks: Vec<Block>,
    targets: Vec<Target>,
    sel: usize,
    scroll: usize,
    title: String,
    suggestions: Vec<Suggestion>,
    search: Input,
    item: Option<Value>,
    selected: Option<String>,
    photo: Option<Picture>,
    photo_key: Option<String>,
    card_scroll: usize,
    popup: Option<Popup>,
    /// Where each target was drawn (for clicks), and the crumbs.
    hits: Vec<(Rect, usize)>,
    crumb_hits: Vec<(Rect, String)>,
    rects: Vec<Rect>,
    page_h: usize,
}

impl Inventory {
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

    fn load_world(&self, then_item: Option<String>, ctx: &Ctx) {
        let idx = self.idx.clone();
        ctx.run(ID, async move { Got::World(idx.world().await.map(Box::new).map_err(|e| e.to_string()), then_item) });
    }

    fn show_item(&mut self, id: &str, ctx: &Ctx) {
        self.selected = Some(id.to_string());
        let (idx, id) = (self.idx.clone(), id.to_string());
        ctx.run(ID, async move { Got::Item(idx.item(&id).await.map_err(|e| e.to_string())) });
    }

    // ── the page, as blocks ──────────────────────────────────────────────

    fn thing_line(&self, item: &Item, prefix: &str, pal: &Palette) -> Line<'static> {
        let w = self.world.as_ref();
        let away = item.status() == "away";
        let kids = w.map_or(0, |w| w.kid_count(&item.id));
        let mut spans = vec![
            Span::styled("▪ ", Style::new().fg(if away { pal.red } else { hue(item.category.as_deref(), pal) })),
            Span::styled(prefix.to_string(), Style::new().fg(pal.beige_dark)),
            Span::styled(item.name.clone(), Style::new().fg(pal.amber)),
            Span::styled(
                format!("  #{}{}", item.id, if kids > 0 { format!(" · {kids} inside") } else { String::new() }),
                Style::new().fg(pal.beige_dark),
            ),
        ];
        if away {
            spans.push(Span::styled("  OUT", bold(pal.red)));
        } else if !matches!(item.status(), "home") {
            spans.push(Span::styled(format!("  {}", item.status()), Style::new().fg(pal.amber_dim)));
        }
        Line::from(spans)
    }

    fn head(text: String, pal: &Palette) -> Block {
        Block::Text(Line::styled(text, bold(pal.beige)))
    }

    fn rebuild(&mut self, pal: &Palette) {
        let Some(w) = &self.world else { return };
        let mut blocks: Vec<Block> = vec![];
        match &self.mode {
            Mode::Search(q) => {
                let hits = w.search(q, None);
                self.title = format!("INVENTORY ░ SEARCH “{}”", q.to_uppercase().chars().take(30).collect::<String>());
                blocks.push(Block::Text(Line::styled(
                    format!("{} found · esc back to browsing", hits.len()),
                    Style::new().fg(pal.beige_dark),
                )));
                for i in &hits {
                    let place: String = w.place_title(i.address.as_deref()).chars().take(28).collect();
                    blocks.push(Block::Select(
                        vec![self.thing_line(i, &format!("{place} · "), pal)],
                        Target::Pick(i.id.clone()),
                    ));
                }
            }
            Mode::Loose => {
                let counts = w.status_counts();
                self.title = "INVENTORY ░ LOOSE ENDS".into();
                blocks.push(Block::Text(Line::styled(
                    format!(
                        "{} away · {} without a home",
                        counts.get("away").unwrap_or(&0),
                        counts.get("unplaced").unwrap_or(&0)
                    ),
                    Style::new().fg(pal.beige_dark),
                )));
                blocks.push(Self::head("AWAY FROM HOME".into(), pal));
                for i in w.search("", Some("away")) {
                    let to = i.checked_out_to.clone().unwrap_or_else(|| "?".into());
                    blocks.push(Block::Select(
                        vec![self.thing_line(&i, &format!("→ {to} · "), pal)],
                        Target::Pick(i.id.clone()),
                    ));
                }
                blocks.push(Block::Text(Line::raw("")));
                blocks.push(Self::head("WITHOUT A HOME".into(), pal));
                for i in w.search("", Some("unplaced")) {
                    blocks.push(Block::Select(vec![self.thing_line(&i, "", pal)], Target::Pick(i.id.clone())));
                }
            }
            Mode::Inbox => {
                self.suggestions = hk::load(&self.vault);
                let open: Vec<usize> = (0..self.suggestions.len()).filter(|n| !self.suggestions[*n].done).collect();
                self.title = "INVENTORY ░ MABEL'S HOUSEKEEPING".into();
                let applicable = open.iter().filter(|n| self.suggestions[**n].applicable()).count();
                blocks.push(Block::Text(Line::styled(
                    format!(
                        "{} open · {applicable} can be applied with a · x ticks one off · enter opens the item",
                        open.len()
                    ),
                    Style::new().fg(pal.beige_dark),
                )));
                let mut section = String::new();
                for n in &open {
                    let s = &self.suggestions[*n];
                    if s.section != section {
                        section = s.section.clone();
                        blocks.push(Block::Text(Line::raw("")));
                        blocks.push(Self::head(section.to_uppercase(), pal));
                    }
                    let first = Line::from(vec![
                        Span::styled(
                            if s.applicable() { "◆ " } else { "◇ " },
                            Style::new().fg(if s.applicable() { pal.teal } else { pal.amber_dim }),
                        ),
                        Span::styled(s.name.chars().take(56).collect::<String>(), Style::new().fg(pal.amber_hi)),
                        Span::styled(format!("  {}", s.ids.join(", ")), Style::new().fg(pal.beige_dark)),
                    ]);
                    let mut second = vec![Span::styled(format!("   → {}", s.change), Style::new().fg(pal.amber))];
                    if !s.why.is_empty() {
                        second.push(Span::styled(format!("   ({})", s.why), Style::new().fg(pal.beige_dark)));
                    }
                    blocks.push(Block::Select(vec![first, Line::from(second), Line::raw("")], Target::Suggest(*n)));
                }
                if open.is_empty() {
                    blocks.push(Block::Text(Line::styled(
                        "Nothing waiting. Mabel does a round when you're away.",
                        Style::new().fg(pal.amber_dim),
                    )));
                }
            }
            Mode::Browse => {
                let r = self.route.clone();
                self.title = format!("INVENTORY ░ {}", w.title(&r).to_uppercase().chars().take(40).collect::<String>());
                self.page = w.page(&r);
                let counts = w.status_counts();
                if r.is_empty() {
                    let live: usize = counts.iter().filter(|(k, _)| k.as_str() != "retired").map(|(_, v)| v).sum();
                    blocks.push(Self::head(
                        format!("WHERE IS EVERYTHING?   {live} things · {} rooms", self.page.cards.len()),
                        pal,
                    ));
                }
                if !self.page.cards.is_empty() {
                    blocks.push(Block::Cards(self.page.cards.clone()));
                }
                if let Some((title, route)) = &self.page.inline {
                    blocks.push(Block::Select(
                        vec![Line::styled(title.to_uppercase(), bold(pal.beige))],
                        Target::Zoom(route.clone()),
                    ));
                }
                let labelled = self.page.zones.len() > 1;
                for zi in 0..self.page.zones.len() {
                    if labelled {
                        let l = self.page.zones[zi].layout();
                        blocks.push(Block::Text(Line::raw("")));
                        blocks.push(Self::head(
                            format!(
                                "{}  {}",
                                l.label.clone().unwrap_or_default(),
                                l.prefix.clone().unwrap_or_default()
                            ),
                            pal,
                        ));
                    }
                    self.zone_blocks(zi, &mut blocks, pal);
                }
                if !self.page.things.is_empty() {
                    blocks.push(Block::Text(Line::raw("")));
                    blocks.push(Self::head(self.page.things_head.to_uppercase(), pal));
                    for i in self.page.things.clone() {
                        blocks.push(Block::Select(vec![self.thing_line(&i, "", pal)], Target::Pick(i.id.clone())));
                    }
                }
                let (away, unplaced) = (*counts.get("away").unwrap_or(&0), *counts.get("unplaced").unwrap_or(&0));
                if r.is_empty() && (away > 0 || unplaced > 0) {
                    blocks.push(Block::Text(Line::raw("")));
                    blocks.push(Block::Select(
                        vec![Line::styled(
                            format!("◆ LOOSE ENDS   {away} away from home · {unplaced} without a home"),
                            bold(pal.orange),
                        )],
                        Target::Loose,
                    ));
                }
            }
        }
        self.targets = blocks
            .iter()
            .flat_map(|b| match b {
                Block::Cards(cards) => cards.iter().map(|c| Target::Zoom(c.route.clone())).collect(),
                Block::Select(_, t) => vec![t.clone()],
                _ => vec![],
            })
            .collect();
        self.blocks = blocks;
        self.sel = self.sel.min(self.targets.len().saturating_sub(1));
    }

    fn zone_blocks(&self, zi: usize, blocks: &mut Vec<Block>, pal: &Palette) {
        let Some(w) = &self.world else { return };
        match &self.page.zones[zi] {
            Zone::Grid { cols, rows, blocks: bins, subs, .. } => {
                blocks.push(Block::Map(zi));
                blocks.push(Block::Text(Line::styled(
                    format!("{cols}×{rows} · ╌ size assumed or unknown · +n things inside"),
                    Style::new().fg(pal.beige_dark),
                )));
                for s in subs {
                    let label = format!(
                        "▦ {}  ({})",
                        s.layout.label.clone().unwrap_or_default(),
                        pos(s.layout.parent_address.as_deref().unwrap_or(""))
                    );
                    blocks.push(Block::Select(
                        vec![Line::styled(label, Style::new().fg(pal.teal))],
                        Target::Zoom(s.route.clone()),
                    ));
                }
                let mut sorted: Vec<&crate::tkindex::Block> = bins.iter().collect();
                sorted.sort_by_key(|b| (b.r, b.c));
                for b in sorted {
                    blocks.push(Block::Select(
                        vec![self.thing_line(&b.item, &format!("C{:02}R{:02} ", b.c, b.r), pal)],
                        Target::Pick(b.item.id.clone()),
                    ));
                }
            }
            Zone::Levels { levels, .. } => {
                for lv in levels {
                    let bits: Vec<String> = lv
                        .subs
                        .iter()
                        .map(|s| format!("▦ {}", s.label.clone().unwrap_or_default()))
                        .chain(lv.items.iter().map(|i| i.name.clone()))
                        .collect();
                    let joined: String = bits.join(" · ").chars().take(160).collect();
                    blocks.push(Block::Select(
                        vec![Line::from(vec![
                            Span::styled(format!("{:<18}", lv.name), bold(pal.amber_hi)),
                            Span::styled(format!("{:<7}", lv.place), Style::new().fg(pal.beige_dark)),
                            Span::styled(
                                if joined.is_empty() { "empty".into() } else { joined },
                                Style::new().fg(if bits.is_empty() { pal.amber_faint } else { pal.amber }),
                            ),
                        ])],
                        Target::Zoom(lv.route.clone()),
                    ));
                }
            }
            Zone::Media { items, .. } => {
                blocks.push(Block::Text(Line::styled(
                    format!("{} titles in sort order", items.len()),
                    Style::new().fg(pal.beige_dark),
                )));
                let mut letter = String::new();
                for i in items {
                    let first =
                        World::sort_title(i).chars().next().map(|c| c.to_uppercase().to_string()).unwrap_or_default();
                    let first =
                        if first.chars().all(char::is_alphabetic) && !first.is_empty() { first } else { "#".into() };
                    if first != letter {
                        letter = first;
                        blocks.push(Block::Text(Line::raw("")));
                        blocks.push(Block::Text(Line::styled(letter.clone(), bold(pal.teal))));
                    }
                    blocks.push(Block::Select(vec![self.thing_line(i, "", pal)], Target::Pick(i.id.clone())));
                }
            }
            Zone::List { cards, spots, .. } => {
                if !cards.is_empty() {
                    blocks.push(Block::Cards(cards.clone()));
                }
                for (spot, items) in spots {
                    let label = if spot == "000000" {
                        "ANYWHERE".to_string()
                    } else {
                        format!("SPOT {}", spot.parse::<u64>().unwrap_or(0))
                    };
                    blocks.push(Block::Text(Line::raw("")));
                    blocks.push(Block::Text(Line::styled(label, bold(pal.beige_dark))));
                    for i in items {
                        blocks.push(Block::Select(vec![self.thing_line(i, "", pal)], Target::Pick(i.id.clone())));
                    }
                }
            }
        }
        let _ = w;
    }

    // ── the map ──────────────────────────────────────────────────────────

    /// A Gridfinity grid / pegboard / compartment box as box-drawing, bins at their footprints.
    fn draw_grid(&self, zone: &Zone, width: usize, pal: &Palette) -> Vec<Line<'static>> {
        let Zone::Grid { cols, rows, blocks, subs, compartments, .. } = zone else { return vec![] };
        let (c_n, r_n) = (*cols as usize, *rows as usize);
        let cw = ((width.saturating_sub(6)) / c_n.max(1)).clamp(5, 14);
        let chh = if cw >= 8 { 3 } else { 2 };
        let (w, h) = (c_n * cw + 1, r_n * chh + 1);
        let mut chars = vec![vec![' '; w]; h];
        let mut style = vec![vec![Style::new().fg(pal.amber_faint); w]; h];
        let put = |chars: &mut Vec<Vec<char>>, style: &mut Vec<Vec<Style>>, x: usize, y: usize, ch: char, st: Style| {
            if x < w && y < h {
                chars[y][x] = ch;
                style[y][x] = st;
            }
        };
        for r in 0..r_n {
            for c in 0..c_n {
                put(&mut chars, &mut style, c * cw + cw / 2, r * chh + chh / 2, '·', Style::new().fg(pal.amber_faint));
            }
        }
        let draw_box = |chars: &mut Vec<Vec<char>>,
                        style: &mut Vec<Vec<Style>>,
                        c: i64,
                        r: i64,
                        bw: i64,
                        bh: i64,
                        label: &str,
                        colour: Color,
                        dashed: bool,
                        extra: &str| {
            let (x0, y0) = ((c - 1).max(0) as usize * cw, (r - 1).max(0) as usize * chh);
            let (x1, y1) = (((c - 1 + bw).max(1)) as usize * cw, ((r - 1 + bh).max(1)) as usize * chh);
            let (hz, vt) = if dashed { ('╌', '╎') } else { ('─', '│') };
            let st = Style::new().fg(colour);
            for x in x0 + 1..x1 {
                put(chars, style, x, y0, hz, st);
                put(chars, style, x, y1, hz, st);
            }
            for y in y0 + 1..y1 {
                put(chars, style, x0, y, vt, st);
                put(chars, style, x1, y, vt, st);
                for x in x0 + 1..x1 {
                    put(chars, style, x, y, ' ', st);
                }
            }
            for (x, y, ch) in [(x0, y0, '┌'), (x1, y0, '┐'), (x0, y1, '└'), (x1, y1, '┘')] {
                put(chars, style, x, y, ch, st);
            }
            let inner_w = (x1 - x0).saturating_sub(1).max(1);
            let inner_h = (y1 - y0).saturating_sub(1).max(1);
            let mut lines: Vec<String> = vec![];
            let mut line = String::new();
            for word in label.split_whitespace() {
                if line.chars().count() + word.chars().count() + usize::from(!line.is_empty()) <= inner_w {
                    if !line.is_empty() {
                        line.push(' ');
                    }
                    line.push_str(word);
                } else {
                    if !line.is_empty() {
                        lines.push(std::mem::take(&mut line));
                    }
                    line = word.chars().take(inner_w).collect();
                }
            }
            if !line.is_empty() {
                lines.push(line);
            }
            lines.truncate(inner_h);
            let extra_n = extra.chars().count();
            if extra_n > 0
                && lines.len() == inner_h
                && let Some(last) = lines.last_mut()
                && last.chars().count() + extra_n + 1 > inner_w
            {
                *last = last.chars().take(inner_w.saturating_sub(extra_n + 1)).collect();
            }
            for (i, text) in lines.iter().enumerate() {
                for (j, ch) in text.chars().enumerate() {
                    put(chars, style, x0 + 1 + j, y0 + 1 + i, ch, st);
                }
            }
            for (j, ch) in extra.chars().enumerate() {
                put(
                    chars,
                    style,
                    (x1 + j).saturating_sub(extra_n),
                    y1.saturating_sub(1),
                    ch,
                    Style::new().fg(pal.teal),
                );
            }
        };
        for b in blocks {
            let colour = if b.item.status() == "away" { pal.red } else { hue(b.item.category.as_deref(), pal) };
            if *compartments {
                draw_box(&mut chars, &mut style, b.c, b.r, 1, 1, &b.item.name, colour, false, "");
            } else {
                let extra = if b.kids > 0 { format!("+{}", b.kids) } else { String::new() };
                draw_box(&mut chars, &mut style, b.c, b.r, b.w, b.h, &b.item.name, colour, b.assumed, &extra);
            }
        }
        for s in subs {
            draw_box(
                &mut chars,
                &mut style,
                s.c,
                s.r,
                1,
                1,
                &format!("▦ {}", s.layout.label.clone().unwrap_or_default()),
                pal.teal,
                false,
                "",
            );
        }
        if let Some(sel) = &self.selected
            && let Some(b) = blocks.iter().find(|b| &b.item.id == sel)
        {
            let (x0, y0) = ((b.c - 1).max(0) as usize * cw, (b.r - 1).max(0) as usize * chh);
            let (x1, y1) = ((b.c - 1 + b.w) as usize * cw, (b.r - 1 + b.h) as usize * chh);
            for row in style.iter_mut().take((y1 + 1).min(h)).skip(y0) {
                for st in row.iter_mut().take((x1 + 1).min(w)).skip(x0) {
                    *st = st.add_modifier(Modifier::REVERSED | Modifier::BOLD);
                }
            }
        }
        let mut out = vec![];
        let header: String = (1..=c_n).map(|c| format!("{:<cw$}", format!("C{c:02}"))).collect();
        out.push(Line::styled(
            format!("    {}", header.chars().take(w).collect::<String>()),
            Style::new().fg(pal.beige_dark),
        ));
        for y in 0..h {
            let label =
                if y % chh == chh / 2 && y / chh < r_n { format!("R{:02} ", y / chh + 1) } else { "    ".into() };
            let mut spans = vec![Span::styled(label, Style::new().fg(pal.beige_dark))];
            let mut run = String::new();
            let mut run_style = style[y][0];
            for x in 0..w {
                if style[y][x] != run_style && !run.is_empty() {
                    spans.push(Span::styled(std::mem::take(&mut run), run_style));
                }
                run_style = style[y][x];
                run.push(chars[y][x]);
            }
            spans.push(Span::styled(run, run_style));
            out.push(Line::from(spans));
        }
        out
    }

    // ── the item card ────────────────────────────────────────────────────

    fn card_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
        let Some(d) = &self.item else {
            return vec![Line::styled("Pick something on the left.", Style::new().fg(pal.amber_dim))];
        };
        let s = |k: &str| value_text(&d[k]);
        let w = self.world.as_ref();
        let label = |t: &str| Span::styled(format!("{t:<6}"), bold(pal.beige));
        let mut out = vec![Line::styled(s("name"), bold(pal.amber_hi))];
        let mut idline = vec![Span::styled(format!("#{}", s("id")), Style::new().fg(pal.beige_dark))];
        if !s("spec_code").is_empty() {
            idline.push(Span::styled(format!("  ·  {}", s("spec_code")), Style::new().fg(pal.teal)));
        }
        idline.push(Span::styled(
            format!("  ·  {}", if s("category").is_empty() { "—".into() } else { s("category") }),
            Style::new().fg(pal.amber_dim),
        ));
        out.push(Line::from(idline));
        out.push(Line::raw(""));
        if s("status") == "away" {
            let mut text = format!(
                "to {} since {}",
                if s("checked_out_to").is_empty() { "?".into() } else { s("checked_out_to") },
                s("checked_out_at").chars().take(16).collect::<String>()
            );
            if !s("checkout_reason").is_empty() {
                text += &format!(" · {}", s("checkout_reason"));
            }
            out.push(Line::from(vec![
                Span::styled("● OUT ", bold(pal.red)),
                Span::styled(text, Style::new().fg(pal.red)),
            ]));
        } else {
            let status = if s("status").is_empty() { "HOME".into() } else { s("status").to_uppercase() };
            out.push(Line::styled(format!("● {status}"), bold(pal.teal)));
        }
        let addr = d["address"].as_str();
        out.push(Line::from(vec![
            label("HOME"),
            Span::styled(w.map_or(s("address"), |w| w.place_title(addr)), Style::new().fg(pal.amber)),
        ]));
        out.push(Line::styled(format!("      {}", addr.unwrap_or("no address")), Style::new().fg(pal.beige_dark)));
        if let Some(p) = d["parent_item_id"].as_str() {
            let name = w.and_then(|w| w.item(p)).map_or(p.to_string(), |i| i.name.clone());
            out.push(Line::from(vec![label("IN"), Span::styled(name, Style::new().fg(pal.amber))]));
        }
        let qty = if d["quantity"].is_number() { format!("{} ", value_text(&d["quantity"])) } else { "? ".into() };
        out.push(Line::from(vec![
            label("QTY"),
            Span::styled(qty, Style::new().fg(pal.amber)),
            Span::styled(s("quantity_note"), Style::new().fg(pal.amber_dim)),
        ]));
        let decision = if s("decision").is_empty() { String::new() } else { format!(" · {}", s("decision")) };
        let stage = if s("stage").is_empty() { "—".into() } else { s("stage") };
        out.push(Line::from(vec![
            label("STAGE"),
            Span::styled(format!("{stage} · {}{decision}", s("confidence")), Style::new().fg(pal.amber)),
        ]));
        if !d["nominal_size"].is_null() {
            let size = match &d["nominal_size"] {
                Value::Array(a) => a.iter().map(value_text).collect::<Vec<_>>().join("×"),
                other => value_text(other),
            };
            out.push(Line::from(vec![label("SIZE"), Span::styled(size, Style::new().fg(pal.amber))]));
        }
        if let Some(nicks) = d["nicknames"].as_array().filter(|n| !n.is_empty()) {
            let names: Vec<String> =
                nicks.iter().map(|n| n.as_str().map_or_else(|| value_text(&n["alias"]), String::from)).collect();
            out.push(Line::from(vec![label("AKA"), Span::styled(names.join(", "), Style::new().fg(pal.amber))]));
        }
        for (title, k) in [("SPECS", "specs"), ("NOTES", "notes")] {
            if !s(k).is_empty() {
                out.push(Line::raw(""));
                out.push(Line::styled(title, bold(pal.beige)));
                for l in s(k).chars().take(900).collect::<String>().lines() {
                    out.push(Line::styled(l.to_string(), Style::new().fg(pal.amber)));
                }
            }
        }
        if let Some(children) = d["children"].as_array().filter(|c| !c.is_empty()) {
            out.push(Line::raw(""));
            out.push(Line::styled(format!("INSIDE ({})", children.len()), bold(pal.beige)));
            for c in children.iter().take(20) {
                out.push(Line::styled(
                    format!(" ▪ {}  #{}", value_text(&c["name"]), value_text(&c["id"])),
                    Style::new().fg(pal.amber),
                ));
            }
        }
        if let Some(moves) = d["movements"].as_array().filter(|m| !m.is_empty()) {
            out.push(Line::raw(""));
            out.push(Line::styled("LATELY", bold(pal.beige)));
            for mv in moves.iter().rev().take(4) {
                let text = format!(
                    " {}  {}  {}",
                    value_text(&mv["happened_at"]).chars().take(16).collect::<String>(),
                    value_text(&mv["kind"]),
                    value_text(&mv["reason"])
                );
                out.push(Line::styled(text.chars().take(90).collect::<String>(), Style::new().fg(pal.beige_dark)));
            }
        }
        out.push(Line::raw(""));
        out.push(Line::styled(
            "e edit · m move · c check out · r return · k nickname · p its place",
            Style::new().fg(pal.amber_faint),
        ));
        out
    }

    // ── actions ──────────────────────────────────────────────────────────

    fn set_mode(&mut self, mode: Mode, ctx: &Ctx) {
        self.mode = mode;
        self.sel = 0;
        self.scroll = 0;
        self.rebuild(&ctx.pal);
    }

    fn zoom(&mut self, route: &str, ctx: &Ctx) {
        self.mode = Mode::Browse;
        self.route = self.world.as_ref().map_or(route.to_string(), |w| w.resolve(route));
        self.sel = 0;
        self.scroll = 0;
        self.rebuild(&ctx.pal);
    }

    fn activate(&mut self, ctx: &mut Ctx) {
        match self.targets.get(self.sel).cloned() {
            Some(Target::Zoom(r)) => self.zoom(&r, ctx),
            Some(Target::Pick(id)) => self.pick(&id, ctx),
            Some(Target::Suggest(n)) => {
                if let Some(id) = self.suggestions.get(n).and_then(|s| s.ids.first()).cloned() {
                    self.show_item(&id, ctx);
                }
            }
            Some(Target::Loose) => self.set_mode(Mode::Loose, ctx),
            None => {}
        }
    }

    fn pick(&mut self, id: &str, ctx: &Ctx) {
        self.show_item(id, ctx);
        // Redraw a map with this bin highlighted.
        if self.mode == Mode::Browse {
            self.rebuild(&ctx.pal);
        }
    }

    fn up(&mut self, ctx: &Ctx) {
        if self.mode != Mode::Browse {
            return self.set_mode(Mode::Browse, ctx);
        }
        if let (Some(w), false) = (&self.world, self.route.is_empty()) {
            let chain = w.chain(&self.route);
            self.route = if chain.len() > 1 { chain[chain.len() - 2].clone() } else { String::new() };
            self.sel = 0;
            self.scroll = 0;
            self.rebuild(&ctx.pal);
        }
    }

    /// A write through tk-index; the world and the card reload after it.
    fn write(&self, path: String, data: Value, done: String, ctx: &Ctx) {
        let idx = self.idx.clone();
        ctx.run(ID, async move { Got::Wrote(idx.post(&path, &data).await.map(|_| done).map_err(|e| e.to_string())) });
    }

    fn need_item(&self, ctx: &mut Ctx) -> Option<Value> {
        if self.item.is_none() {
            ctx.toast("INVENTORY", "Pick an item first");
        }
        self.item.clone()
    }

    fn focused_suggestion(&self) -> Option<usize> {
        match self.targets.get(self.sel) {
            Some(Target::Suggest(n)) => Some(*n),
            _ => None,
        }
    }

    fn common(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.world.is_none() && !matches!(keys::ch(key), Some('g')) {
            return Outcome::Ignored;
        }
        let id = |d: &Value| value_text(&d["id"]);
        let name = |d: &Value| value_text(&d["name"]);
        match (keys::ch(key), key.code) {
            (_, KeyCode::Backspace) => self.up(ctx),
            (Some('/'), _) => ctx.focus(SEARCH),
            (Some('H'), _) => {
                self.route.clear();
                self.set_mode(Mode::Browse, ctx);
            }
            (Some('b'), _) => self.set_mode(Mode::Inbox, ctx),
            (Some('o'), _) => self.set_mode(Mode::Loose, ctx),
            (Some('g'), _) => self.load_world(self.selected.clone(), ctx),
            (Some('e'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let fields = EDIT_FIELDS
                        .iter()
                        .map(|(k, label, kind)| {
                            let kind = match *kind {
                                "line" => Kind::Line,
                                "text" => Kind::Text,
                                opts => Kind::Choice(opts.split('|').map(String::from).collect()),
                            };
                            Field::new(k, label, &value_text(&d[*k]), kind)
                        })
                        .collect();
                    self.popup = Some(Popup::Edit(Form::new(&format!("EDIT #{}", id(&d)), fields), id(&d)));
                }
            }
            (Some('m'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let f = Field::new(
                        "destination",
                        "New home address (16 characters, e.g. GRIDES0001C03R05)",
                        &value_text(&d["address"]),
                        Kind::Line,
                    );
                    self.popup =
                        Some(Popup::Move(Form::new(&format!("MOVE {}", name(&d).to_uppercase()), vec![f]), id(&d)));
                }
            }
            (Some('c'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let fields = vec![
                        Field::new("destination", "Where is it going? (optional)", "", Kind::Line),
                        Field::new("reason", "What for? (optional)", "", Kind::Line),
                    ];
                    self.popup = Some(Popup::Checkout(
                        Form::new(&format!("CHECK OUT {}", name(&d).to_uppercase()), fields),
                        id(&d),
                    ));
                }
            }
            (Some('r'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    self.write(
                        format!("items/{}/return", id(&d)),
                        json!({}),
                        format!("{} is back home", name(&d)),
                        ctx,
                    );
                }
            }
            (Some('k'), _) => {
                if let Some(d) = self.need_item(ctx) {
                    let f = Field::new("add", "Also called", "", Kind::Line);
                    self.popup = Some(Popup::Nickname(
                        Form::new(&format!("NICKNAME FOR {}", name(&d).to_uppercase()), vec![f]),
                        id(&d),
                    ));
                }
            }
            (Some('p'), _) => {
                if let (Some(d), Some(w)) = (&self.item, &self.world)
                    && let Some(addr) = d["address"].as_str()
                {
                    let r = w.resolve(&w.place_route(Some(addr)));
                    self.zoom(&r, ctx);
                }
            }
            (Some('u'), _) => {
                let idx = self.idx.clone();
                ctx.run(ID, async move {
                    let last =
                        idx.get("undo").await.ok().map(|v| value_text(&v["last"]["label"])).filter(|l| !l.is_empty());
                    let done = idx.post("undo", &json!({})).await;
                    Got::Wrote(
                        done.map(|_| format!("↶ undid: {}", last.unwrap_or_else(|| "the last change".into())))
                            .map_err(|e| e.to_string()),
                    )
                });
            }
            (Some('a'), _) if self.mode == Mode::Inbox => {
                let Some(n) = self.focused_suggestion() else { return Outcome::Used };
                let s = self.suggestions[n].clone();
                let Some(edit) = s.edit.clone() else {
                    ctx.toast("INVENTORY", "That one is a to-do for you (x ticks it off once done)");
                    return Outcome::Used;
                };
                let idx = self.idx.clone();
                ctx.run(ID, async move {
                    for id in &s.ids {
                        if let Err(e) = idx.post(&format!("items/{id}/edit"), &edit).await {
                            return Got::Applied(Err(format!("#{id}: {e}")), None);
                        }
                    }
                    Got::Applied(Ok(format!("✓ applied to {} item(s): {} (undo: u)", s.ids.len(), s.change)), Some(n))
                });
            }
            (Some('x'), _) if self.mode == Mode::Inbox => {
                if let Some(n) = self.focused_suggestion()
                    && hk::tick(&self.vault, &self.suggestions[n], true)
                {
                    ctx.toast("INVENTORY", "ticked off");
                    self.rebuild(&ctx.pal);
                }
            }
            (Some('v'), _) if self.mode == Mode::Inbox => {
                if let Some(n) = self.focused_suggestion() {
                    let open = OpenNote {
                        vault: "core of truth".into(),
                        rel: hk::NOTE.into(),
                        line: Some(self.suggestions[n].line_no),
                    };
                    ctx.requests.push(Request::Deliver { to: "vault", msg: Box::new(open) });
                }
            }
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn popup_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) {
        let Some(mut popup) = self.popup.take() else { return };
        let (form, _) = match &mut popup {
            Popup::Edit(f, id) | Popup::Move(f, id) | Popup::Checkout(f, id) | Popup::Nickname(f, id) => {
                (f, id.clone())
            }
        };
        let answer = form.key(key);
        let values = match answer {
            Answer::Open => {
                self.popup = Some(popup);
                return;
            }
            Answer::Cancel => return,
            Answer::Chosen(v) => v,
        };
        let get =
            |k: &str| values.iter().find(|(key, _)| key == k).map(|(_, v)| v.trim().to_string()).unwrap_or_default();
        let name = self.item.as_ref().map(|d| value_text(&d["name"])).unwrap_or_default();
        match popup {
            Popup::Edit(_, id) => {
                let Some(d) = &self.item else { return };
                let mut changed = serde_json::Map::new();
                for (k, v) in &values {
                    let old = value_text(&d[k.as_str()]);
                    if old.trim() != v.trim() {
                        let value = if k == "quantity" {
                            if v.trim().is_empty() {
                                Value::Null
                            } else {
                                v.trim().replace(',', ".").parse::<f64>().map_or(Value::Null, |f| json!(f))
                            }
                        } else {
                            json!(v)
                        };
                        changed.insert(k.clone(), value);
                    }
                }
                if !changed.is_empty() {
                    self.write(format!("items/{id}/edit"), Value::Object(changed), format!("Edited {name}"), ctx);
                }
            }
            Popup::Move(_, id) => {
                let dest = get("destination").to_uppercase();
                if !dest.is_empty() {
                    self.write(
                        format!("items/{id}/move"),
                        json!({"destination": dest, "reason": "moved in the TUI"}),
                        format!("Moved {name}"),
                        ctx,
                    );
                }
            }
            Popup::Checkout(_, id) => {
                let dest = get("destination");
                let dest = if dest.is_empty() { Value::Null } else { json!(dest) };
                self.write(
                    format!("items/{id}/checkout"),
                    json!({"destination": dest, "reason": get("reason")}),
                    format!("{name} is out"),
                    ctx,
                );
            }
            Popup::Nickname(_, id) => {
                let add = get("add");
                if !add.is_empty() {
                    self.write(format!("items/{id}/nickname"), json!({ "add": add }), "Nickname added".into(), ctx);
                }
            }
        }
    }

    // ── drawing ──────────────────────────────────────────────────────────

    /// Lay the blocks out at `width`: lines, and which target sits on which lines and columns.
    fn layout(&self, width: usize, pal: &Palette) -> (Vec<Line<'static>>, Vec<Place>) {
        let mut lines: Vec<Line<'static>> = vec![];
        let mut places = vec![];
        let mut t = 0;
        let per_row = if width >= 120 { 4 } else { 3 };
        for block in &self.blocks {
            match block {
                Block::Text(l) => lines.extend(wrap::wrap_lines(std::slice::from_ref(l), width.max(1))),
                Block::Map(zi) => lines.extend(self.draw_grid(&self.page.zones[*zi], width, pal)),
                Block::Select(entry, _) => {
                    let selected = t == self.sel;
                    let wrapped = wrap::wrap_lines(entry, width.max(1));
                    places.push(Place {
                        target: t,
                        line: lines.len(),
                        x: 0,
                        width: width as u16,
                        height: wrapped.len().max(1),
                    });
                    for l in wrapped {
                        lines.push(if selected { l.style(Style::new().bg(pal.amber_faint)) } else { l });
                    }
                    t += 1;
                }
                Block::Cards(cards) => {
                    let gap = 2;
                    let cw = ((width + gap) / per_row).saturating_sub(gap).max(12);
                    for row in cards.chunks(per_row) {
                        let top = lines.len();
                        let mut rows: Vec<Vec<Span<'static>>> = vec![vec![]; CARD_H];
                        for (n, card) in row.iter().enumerate() {
                            let selected = t == self.sel;
                            let (edge, bg) =
                                if selected { (pal.amber, pal.amber_faint) } else { (pal.amber_dim, pal.panel) };
                            let fit = |s: &str, w: usize| {
                                let mut out = String::new();
                                for c in s.chars() {
                                    if width_of(&out) + width_of(&c.to_string()) > w {
                                        break;
                                    }
                                    out.push(c);
                                }
                                let pad = w.saturating_sub(width_of(&out));
                                out + &" ".repeat(pad)
                            };
                            let inner = cw.saturating_sub(4);
                            let count = if card.count > 0 { format!("  · {}", card.count) } else { String::new() };
                            let code: String = card.code.chars().take(34).collect();
                            let code_w = inner.saturating_sub(width_of(&count));
                            rows[0].push(Span::styled(format!("╭{}╮", "─".repeat(cw - 2)), Style::new().fg(edge)));
                            rows[1].push(Span::styled("│ ", Style::new().fg(edge).bg(bg)));
                            rows[1].push(Span::styled(fit(&card.title, inner), bold(pal.amber_hi).bg(bg)));
                            rows[1].push(Span::styled(" │", Style::new().fg(edge).bg(bg)));
                            rows[2].push(Span::styled("│ ", Style::new().fg(edge).bg(bg)));
                            rows[2].push(Span::styled(fit(&code, code_w), Style::new().fg(pal.beige_dark).bg(bg)));
                            rows[2].push(Span::styled(
                                fit(&count, inner - code_w.min(inner)),
                                Style::new().fg(pal.teal).bg(bg),
                            ));
                            rows[2].push(Span::styled(" │", Style::new().fg(edge).bg(bg)));
                            rows[3].push(Span::styled(format!("╰{}╯", "─".repeat(cw - 2)), Style::new().fg(edge)));
                            if n + 1 < row.len() {
                                for r in rows.iter_mut() {
                                    r.push(Span::raw(" ".repeat(gap)));
                                }
                            }
                            places.push(Place {
                                target: t,
                                line: top,
                                x: (n * (cw + gap)) as u16,
                                width: cw as u16,
                                height: CARD_H,
                            });
                            t += 1;
                        }
                        lines.extend(rows.into_iter().map(Line::from));
                    }
                    lines.push(Line::raw(""));
                }
            }
        }
        (lines, places)
    }

    /// The main panel; returns where the search box and the body were drawn (its sections).
    fn render_main(&mut self, frame: &mut Frame, area: Rect, view: &View) -> (Rect, Rect) {
        let pal = view.pal;
        let lit = view.panel_state(BODY) == PanelState::Lit;
        let inside = view.inside(SEARCH) || view.inside(BODY);
        let state = if lit {
            PanelState::Lit
        } else if inside {
            PanelState::Inside
        } else {
            PanelState::Idle
        };
        let block = ui::panel(pal, &self.title, state);
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let [search, crumbs, _, body] = Layout::vertical([
            Constraint::Length(3),
            Constraint::Length(1),
            Constraint::Length(1),
            Constraint::Fill(1),
        ])
        .areas(inner);
        let sblock = ui::panel(pal, "", view.panel_state(SEARCH));
        let field = sblock.inner(search);
        frame.render_widget(sblock, search);
        let ph = if view.panel_state(SEARCH) == PanelState::Lit { pal.amber_dim } else { pal.amber_faint };
        self.search.render(frame, field, Style::new().fg(pal.amber_hi), Style::new().fg(ph), view.inside(SEARCH));

        // Breadcrumbs: each step clickable.
        self.crumb_hits.clear();
        if let Some(w) = &self.world {
            if self.mode == Mode::Browse {
                let mut spans = vec![];
                let mut x = crumbs.x;
                for (n, step) in w.chain(&self.route).iter().enumerate() {
                    if n > 0 {
                        spans.push(Span::styled("  ›  ", Style::new().fg(pal.amber_faint)));
                        x += 5;
                    }
                    let title = w.title(step);
                    let tw = width(&title) as u16;
                    if *step == self.route {
                        spans.push(Span::styled(title, bold(pal.amber_hi)));
                    } else {
                        spans.push(Span::styled(
                            title,
                            Style::new().fg(pal.amber_dim).add_modifier(Modifier::UNDERLINED),
                        ));
                        self.crumb_hits.push((Rect::new(x, crumbs.y, tw, 1), step.clone()));
                    }
                    x += tw;
                }
                spans.push(Span::styled(format!("     {}", w.code(&self.route)), Style::new().fg(pal.beige_dark)));
                frame.render_widget(Paragraph::new(Line::from(spans)), crumbs);
            }
        } else {
            let text = if self.error.is_empty() {
                "loading the Gremlin Index…".to_string()
            } else {
                format!("✖ can't reach the Gremlin Index: {}", self.error)
            };
            frame.render_widget(
                Paragraph::new(Line::styled(
                    text,
                    Style::new().fg(if self.error.is_empty() { pal.amber_dim } else { pal.red }),
                )),
                crumbs,
            );
        }

        let (lines, places) = self.layout(body.width.saturating_sub(1) as usize, pal);
        let h = body.height as usize;
        self.page_h = h;
        // Keep the selection in view.
        if let Some(p) = places.iter().find(|p| p.target == self.sel) {
            let last = p.line + p.height - 1;
            if p.line < self.scroll {
                self.scroll = p.line;
            } else if last >= self.scroll + h {
                self.scroll = last + 1 - h;
            }
        }
        self.scroll = self.scroll.min(lines.len().saturating_sub(h));
        self.hits = places
            .iter()
            .filter(|p| p.line >= self.scroll && p.line < self.scroll + h)
            .map(|p| {
                (Rect::new(body.x + p.x, body.y + (p.line - self.scroll) as u16, p.width, p.height as u16), p.target)
            })
            .collect();
        let shown: Vec<Line> = lines.into_iter().skip(self.scroll).take(h).collect();
        frame.render_widget(Paragraph::new(shown), body);
        (search, body)
    }

    fn render_item(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let title = self.item.as_ref().map_or("ITEM".to_string(), |d| format!("ITEM ░ #{}", value_text(&d["id"])));
        let block = ui::panel(pal, &title, view.panel_state(ITEM));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let mut y = inner.y;
        if let Some(p) = &mut self.photo {
            let max_h = if view.narrow { 11 } else { 14 };
            let max_w = if view.narrow { 22 } else { 28 };
            let spot = Rect { width: max_w.min(inner.width), height: inner.height, ..inner };
            y += p.render(spot, frame.buffer_mut(), max_h, 0) + 1;
        }
        let rest = Rect { y, height: inner.bottom().saturating_sub(y), ..inner };
        let lines = wrap::wrap_lines(&self.card_lines(pal), rest.width.max(1) as usize);
        self.card_scroll = self.card_scroll.min(lines.len().saturating_sub(rest.height as usize));
        frame.render_widget(Paragraph::new(lines).scroll((self.card_scroll as u16, 0)), rest);
    }
}

/// Where a target was laid out: its first line, column, width and height in lines.
struct Place {
    target: usize,
    line: usize,
    x: u16,
    width: u16,
    height: usize,
}

fn width_of(s: &str) -> usize {
    width(s)
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
