//! The index's pieces: layouts (places), items, addresses (key + position), and what a page
//! is made of (cards, blocks, zones, levels).

use super::*;

/// A place: a room's area, a piece of furniture's inside, a drawer unit, a grid, a pegboard…
/// (tk-index's layout record; `key` is its 10-character address prefix).
#[derive(Debug, Clone, Deserialize, Default)]
pub struct Layout {
    pub key: String,
    #[serde(default)]
    pub area: String,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub prefix: Option<String>,
    #[serde(default)]
    pub host_item_id: Option<String>,
    #[serde(default)]
    pub parent_address: Option<String>,
    #[serde(default)]
    pub position_format: Option<String>,
    #[serde(default)]
    pub view: Option<String>,
    #[serde(default)]
    pub columns: Option<i64>,
    #[serde(default)]
    pub rows: Option<i64>,
    #[serde(default)]
    pub levels: Option<i64>,
}

/// A thing (tk-index's item record). Its `address` says where it is.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct Item {
    pub id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub address: Option<String>,
    #[serde(default)]
    pub parent_item_id: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub spec_code: Option<String>,
    #[serde(default)]
    pub sort_key: Option<String>,
    #[serde(default)]
    pub search_words: Option<String>,
    #[serde(default)]
    pub checked_out_to: Option<String>,
    #[serde(default)]
    pub span_columns: Option<i64>,
    #[serde(default)]
    pub span_rows: Option<i64>,
    #[serde(default)]
    pub footprint_confirmed: Option<Value>,
}

impl Item {
    /// "home", "away" (checked out), "retired"… ("home" when unset).
    pub fn status(&self) -> &str {
        self.status.as_deref().unwrap_or("home")
    }
}

/// The first 10 characters of an address: the place it's in.
pub fn key(addr: &str) -> &str {
    &addr[..addr.len().min(10)]
}

/// The position part of an address.
pub fn pos(addr: &str) -> &str {
    if addr.len() > 10 { &addr[10..] } else { "" }
}

/// An area's room letters (the first 3).
pub fn letters(area: &str) -> &str {
    &area[..area.len().min(3)]
}

/// The level digit of an address (its 11th character).
pub(super) fn level_digit(addr: &str) -> Option<char> {
    addr.chars().nth(10)
}

/// "C03R05" → (3, 5).
pub fn cell(position: &str) -> Option<(i64, i64)> {
    let b = position.as_bytes();
    if b.len() != 6 || b[0] != b'C' || b[3] != b'R' {
        return None;
    }
    Some((position[1..3].parse().ok()?, position[4..6].parse().ok()?))
}

/// The order an area's layouts are shown in: drawers, shelves, grids, compartments, pegboards.
pub(super) fn view_rank(view: Option<&str>) -> u8 {
    match view {
        Some("drawers") => 0,
        Some("shelves") => 1,
        Some("gridfinity") => 2,
        Some("compartments") => 3,
        Some("pegboard") => 4,
        _ => 9,
    }
}

/// A place card: a place you can zoom into, with its title, code and how many things are in it.
#[derive(Debug, Clone)]
pub struct Card {
    pub route: String,
    pub title: String,
    pub code: String,
    pub count: usize,
}

/// An item drawn on an area's grid.
pub struct Block {
    pub item: Item,
    /// Its column, row, width and height on the grid (in cells).
    pub c: i64,
    pub r: i64,
    pub w: i64,
    pub h: i64,
    pub kids: usize,
    /// The footprint is a guess (or unknown): drawn dashed.
    pub assumed: bool,
}

/// A layout inside a layout.
pub struct Sub {
    /// A smaller layout sitting on the grid (a bin with its own spots) at column `c`, row `r`.
    pub layout: Layout,
    pub c: i64,
    pub r: i64,
    pub route: String,
}

/// A level of a drawer unit or shelf.
pub struct Level {
    /// One drawer or shelf: its route, name, what a place on it is called, what sits on it.
    pub route: String,
    pub name: String,
    pub place: &'static str,
    pub subs: Vec<Layout>,
    pub items: Vec<Item>,
}

/// One drawing in an area view.
pub enum Zone {
    Grid { layout: Layout, cols: i64, rows: i64, blocks: Vec<Block>, subs: Vec<Sub>, compartments: bool },
    Levels { layout: Layout, levels: Vec<Level> },
    Media { layout: Layout, items: Vec<Item> },
    List { layout: Layout, cards: Vec<Card>, spots: Vec<(String, Vec<Item>)> },
}

impl Zone {
    /// The layout this zone draws.
    pub fn layout(&self) -> &Layout {
        match self {
            Zone::Grid { layout, .. }
            | Zone::Levels { layout, .. }
            | Zone::Media { layout, .. }
            | Zone::List { layout, .. } => layout,
        }
    }
}

/// Everything a route's page shows (`World::page` builds it; INVENTORY's page.rs draws it).
#[derive(Default)]
pub struct Page {
    pub cards: Vec<Card>,
    pub things: Vec<Item>,
    pub things_head: String,
    pub zones: Vec<Zone>,
    /// An item's one levelled area, drawn in place: (title, route).
    pub inline: Option<(String, String)>,
}
