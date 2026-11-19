//! The Gremlin Index (tk-index) for INVENTORY: its HTTP API and a port of its place model.
//!
//! The model mirrors tk-index's web UI (static/js/core.js and views.js) so the TUI zooms the same
//! way: routes "" (home), room/KON, item/00000002, code/DES, area/DES0001, level/DRWLHE0001/3,
//! with the same redirects that skip pages holding a single thing, the same titles, codes and
//! counts. Writes go through tk-index's own endpoints, so every change is in its undo history.
//!
//! Addresses are 16 characters: a 10-character place key (prefix + area + number), then the
//! position (a grid cell C03R05, a level digit and spot, or a spot number).

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use anyhow::{Result, anyhow};
use reqwest::Client;
use serde::Deserialize;
use serde_json::Value;

// ── the API ──────────────────────────────────────────────────────────────

#[derive(Clone)]
pub struct TkIndex {
    url: String,
    http: Client,
}

impl TkIndex {
    /// No auth: loopback, or the tailnet through tailscale serve.
    pub fn new(url: &str) -> Self {
        let http = Client::builder().timeout(Duration::from_secs(20)).build().unwrap_or_default();
        TkIndex { url: url.trim_end_matches('/').to_string(), http }
    }

    async fn checked(resp: reqwest::Response) -> Result<Value> {
        let status = resp.status().as_u16();
        let body = resp.bytes().await?;
        if status >= 300 {
            let msg = serde_json::from_slice::<Value>(&body)
                .ok()
                .and_then(|v| v["error"].as_str().map(String::from))
                .unwrap_or_else(|| {
                    format!("{status}: {}", String::from_utf8_lossy(&body).chars().take(200).collect::<String>())
                });
            return Err(anyhow!(msg));
        }
        if body.is_empty() {
            return Ok(Value::Null);
        }
        Ok(serde_json::from_slice(&body)?)
    }

    pub async fn get(&self, path: &str) -> Result<Value> {
        Self::checked(self.http.get(format!("{}/api/{path}", self.url)).send().await?).await
    }

    pub async fn post(&self, path: &str, data: &Value) -> Result<Value> {
        Self::checked(self.http.post(format!("{}/api/{path}", self.url)).json(data).send().await?).await
    }

    pub async fn world(&self) -> Result<World> {
        Ok(World::new(self.get("world").await?))
    }

    pub async fn item(&self, id: &str) -> Result<Value> {
        self.get(&format!("items/{id}")).await
    }

    pub async fn image(&self, path: &str) -> Option<Vec<u8>> {
        let resp = self.http.get(format!("{}/images/{path}", self.url)).send().await.ok()?;
        if resp.status().as_u16() != 200 {
            return None;
        }
        resp.bytes().await.ok().map(|b| b.to_vec())
    }
}

// ── the model ────────────────────────────────────────────────────────────

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
fn level_digit(addr: &str) -> Option<char> {
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

fn view_rank(view: Option<&str>) -> u8 {
    match view {
        Some("drawers") => 0,
        Some("shelves") => 1,
        Some("gridfinity") => 2,
        Some("compartments") => 3,
        Some("pegboard") => 4,
        _ => 9,
    }
}

#[derive(Debug, Clone)]
pub struct Card {
    pub route: String,
    pub title: String,
    pub code: String,
    pub count: usize,
}

pub struct Block {
    pub item: Item,
    pub c: i64,
    pub r: i64,
    pub w: i64,
    pub h: i64,
    pub kids: usize,
    /// The footprint is a guess (or unknown): drawn dashed.
    pub assumed: bool,
}

pub struct Sub {
    pub layout: Layout,
    pub c: i64,
    pub r: i64,
    pub route: String,
}

pub struct Level {
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
    pub fn layout(&self) -> &Layout {
        match self {
            Zone::Grid { layout, .. }
            | Zone::Levels { layout, .. }
            | Zone::Media { layout, .. }
            | Zone::List { layout, .. } => layout,
        }
    }
}

#[derive(Default)]
pub struct Page {
    pub cards: Vec<Card>,
    pub things: Vec<Item>,
    pub things_head: String,
    pub zones: Vec<Zone>,
    /// An item's one levelled area, drawn in place: (title, route).
    pub inline: Option<(String, String)>,
}

const ARTICLE_WORDS: [&str; 7] = ["the", "a", "an", "den", "det", "de", "en"];
const ENDINGS: [&str; 12] = ["erna", "arna", "orna", "na", "en", "et", "er", "ar", "or", "n", "t", "s"];

pub struct World {
    pub layouts: Vec<Layout>,
    pub items: Vec<Item>,
    synonyms: HashMap<String, Vec<String>>,
    pub icons: HashMap<String, String>,
    l: HashMap<String, usize>,
    i: HashMap<String, usize>,
    on: HashMap<String, Vec<usize>>,
    kids: HashMap<String, Vec<usize>>,
    hosted: HashMap<String, Vec<usize>>,
    area: HashMap<String, Vec<usize>>,
    sit: HashMap<String, Vec<usize>>,
    names: HashMap<String, String>,
    nick: HashMap<String, Vec<String>>,
    count_l: HashMap<String, usize>,
    count_i: HashMap<String, usize>,
}

fn split_route(r: &str) -> (&str, &str, &str) {
    let mut parts = r.splitn(3, '/');
    (parts.next().unwrap_or(""), parts.next().unwrap_or(""), parts.next().unwrap_or(""))
}

impl World {
    pub fn new(w: Value) -> Self {
        let layouts: Vec<Layout> = serde_json::from_value(w["layouts"].clone()).unwrap_or_default();
        let items: Vec<Item> = serde_json::from_value(w["items"].clone()).unwrap_or_default();
        let synonyms: HashMap<String, Vec<String>> = serde_json::from_value(w["synonyms"].clone()).unwrap_or_default();
        let icons: HashMap<String, String> = serde_json::from_value(w["icons"].clone()).unwrap_or_default();
        let mut world = World {
            layouts,
            items,
            synonyms,
            icons,
            l: HashMap::new(),
            i: HashMap::new(),
            on: HashMap::new(),
            kids: HashMap::new(),
            hosted: HashMap::new(),
            area: HashMap::new(),
            sit: HashMap::new(),
            names: HashMap::new(),
            nick: HashMap::new(),
            count_l: HashMap::new(),
            count_i: HashMap::new(),
        };
        world.index(&w["aliases"]);
        world
    }

    fn index(&mut self, aliases: &Value) {
        for (n, l) in self.layouts.iter().enumerate() {
            self.l.insert(l.key.clone(), n);
            self.area.entry(l.area.clone()).or_default().push(n);
            if let Some(h) = &l.host_item_id {
                self.hosted.entry(h.clone()).or_default().push(n);
            }
            if let Some(p) = &l.parent_address {
                self.sit.entry(key(p).to_string()).or_default().push(n);
            }
        }
        for (n, i) in self.items.iter().enumerate() {
            self.i.insert(i.id.clone(), n);
            if let Some(a) = &i.address {
                self.on.entry(key(a).to_string()).or_default().push(n);
            }
            if let Some(p) = &i.parent_item_id {
                self.kids.entry(p.clone()).or_default().push(n);
            }
        }
        for a in aliases.as_array().into_iter().flatten() {
            let (target, alias) = (a["target"].as_str().unwrap_or(""), a["alias"].as_str().unwrap_or(""));
            if a["display"].as_bool().unwrap_or(false) || a["display"].as_i64().unwrap_or(0) != 0 {
                self.names.insert(target.into(), alias.into());
            } else if target.len() == 8 && target.chars().all(|c| c.is_ascii_digit()) {
                self.nick.entry(target.into()).or_default().push(alias.into());
            }
        }
        let layouts = &self.layouts;
        for ls in self.area.values_mut() {
            ls.sort_by_key(|n| view_rank(layouts[*n].view.as_deref()));
        }
        // How many things are under each place and each hosting item (walking up the chain).
        for i in &self.items {
            let Some(addr) = &i.address else { continue };
            if i.status() == "retired" {
                continue;
            }
            let mut seen = HashSet::new();
            let mut a = addr.clone();
            while !a.is_empty() {
                let Some(l) = self.l.get(key(&a)).map(|n| &self.layouts[*n]) else { break };
                if !seen.insert(l.key.clone()) {
                    break;
                }
                *self.count_l.entry(l.key.clone()).or_default() += 1;
                if let Some(h) = &l.host_item_id {
                    *self.count_i.entry(h.clone()).or_default() += 1;
                    a = self.i.get(h).and_then(|n| self.items[*n].address.clone()).unwrap_or_default();
                } else {
                    a = l.parent_address.clone().unwrap_or_default();
                }
            }
        }
    }

    // ── helpers ──────────────────────────────────────────────────────────

    pub fn layout(&self, k: &str) -> Option<&Layout> {
        self.l.get(k).map(|n| &self.layouts[*n])
    }

    pub fn item(&self, id: &str) -> Option<&Item> {
        self.i.get(id).map(|n| &self.items[*n])
    }

    fn many(&self, map: &HashMap<String, Vec<usize>>, k: &str) -> Vec<&Layout> {
        map.get(k).map(|v| v.iter().map(|n| &self.layouts[*n]).collect()).unwrap_or_default()
    }

    fn items_on(&self, k: &str) -> Vec<&Item> {
        self.on.get(k).map(|v| v.iter().map(|n| &self.items[*n]).collect()).unwrap_or_default()
    }

    fn area_layouts(&self, a: &str) -> Vec<&Layout> {
        self.many(&self.area, a)
    }

    fn hosted_by(&self, id: &str) -> Vec<&Layout> {
        self.many(&self.hosted, id)
    }

    fn sitting_on(&self, k: &str) -> Vec<&Layout> {
        self.many(&self.sit, k)
    }

    pub fn kid_count(&self, id: &str) -> usize {
        self.kids.get(id).map_or(0, |v| v.len())
    }

    pub fn is_hosting(&self, id: &str) -> bool {
        self.hosted.get(id).is_some_and(|v| !v.is_empty())
    }

    fn is_root(l: &Layout) -> bool {
        l.host_item_id.is_none() && l.parent_address.is_none()
    }

    fn roots_of(&self, room: &str) -> Vec<&Layout> {
        self.layouts.iter().filter(|l| Self::is_root(l) && letters(&l.area) == room).collect()
    }

    fn room_codes(&self) -> Vec<String> {
        let mut out: Vec<String> = vec![];
        for l in self.layouts.iter().filter(|l| Self::is_root(l)) {
            let c = letters(&l.area).to_string();
            if !out.contains(&c) {
                out.push(c);
            }
        }
        out
    }

    fn code_layouts(&self, x: &str) -> Vec<&Layout> {
        self.layouts
            .iter()
            .filter(|l| letters(&l.area) == x && l.host_item_id.is_none() && l.parent_address.is_some())
            .collect()
    }

    fn areas_of(ls: &[&Layout]) -> Vec<String> {
        let mut out: Vec<String> = vec![];
        for l in ls {
            if !out.contains(&l.area) {
                out.push(l.area.clone());
            }
        }
        out
    }

    /// Items not sitting inside another item at the same address.
    fn top_only(&self, items: Vec<&Item>) -> Vec<Item> {
        let by: HashMap<&str, &Item> = items.iter().map(|i| (i.id.as_str(), *i)).collect();
        items
            .iter()
            .filter(|i| match i.parent_item_id.as_deref().and_then(|p| by.get(p)) {
                Some(parent) => parent.address != i.address,
                None => true,
            })
            .map(|i| (*i).clone())
            .collect()
    }

    pub fn kids_of(&self, id: &str) -> Vec<Item> {
        let own = self.item(id).and_then(|i| i.address.clone());
        self.kids
            .get(id)
            .map(|v| {
                v.iter().map(|n| &self.items[*n]).filter(|k| k.address.is_some() && k.address == own).cloned().collect()
            })
            .unwrap_or_default()
    }

    fn host_code(&self, id: &str) -> Option<String> {
        let ls = self.hosted_by(id);
        let mut codes: Vec<&str> = vec![];
        for l in &ls {
            let c = letters(&l.area);
            if !codes.contains(&c) {
                codes.push(c);
            }
        }
        if codes.len() != 1 {
            return None;
        }
        let x = codes[0];
        let own = self.item(id).and_then(|i| i.address.as_deref());
        if own.and_then(|a| self.layout(key(a))).is_some_and(|l| letters(&l.area) == x) {
            return None;
        }
        for l in &self.layouts {
            if letters(&l.area) == x && l.host_item_id.as_deref().is_some_and(|h| h != id) {
                let host = l.host_item_id.as_deref().and_then(|h| self.item(h));
                let where_ = host.and_then(|h| h.address.as_deref()).and_then(|a| self.layout(key(a)));
                if where_.map_or("", |w| letters(&w.area)) != x {
                    return None;
                }
            }
        }
        Some(x.to_string())
    }

    // ── routes ───────────────────────────────────────────────────────────

    pub fn place_route(&self, addr: Option<&str>) -> String {
        let Some(addr) = addr.filter(|a| !a.is_empty()) else { return String::new() };
        let Some(l) = self.layout(key(addr)) else { return String::new() };
        if Self::is_root(l) {
            return format!("room/{}", letters(&l.area));
        }
        if l.position_format.as_deref() == Some("level") {
            return format!("level/{}/{}", l.key, level_digit(addr).unwrap_or('0'));
        }
        format!("area/{}", l.area)
    }

    fn redirect(&self, r: &str) -> Option<String> {
        let (k, a, b) = split_route(r);
        match k {
            "item" => {
                let ar = Self::areas_of(&self.hosted_by(a));
                (ar.len() == 1 && self.kids_of(a).is_empty()).then(|| format!("area/{}", ar[0]))
            }
            "code" => {
                let ar = Self::areas_of(&self.code_layouts(a));
                (ar.len() == 1).then(|| format!("area/{}", ar[0]))
            }
            "level" => {
                let digit = b.chars().next();
                let its = self
                    .items_on(a)
                    .into_iter()
                    .filter(|i| i.address.as_deref().and_then(level_digit) == digit)
                    .count();
                let st: Vec<&Layout> = self
                    .sitting_on(a)
                    .into_iter()
                    .filter(|l| l.parent_address.as_deref().and_then(level_digit) == digit)
                    .collect();
                (its == 0 && st.len() == 1).then(|| format!("area/{}", st[0].area))
            }
            _ => None,
        }
    }

    /// Follow redirects: a page that would show one thing shows that thing instead.
    pub fn resolve(&self, r: &str) -> String {
        let mut r = r.to_string();
        for _ in 0..6 {
            match self.redirect(&r) {
                Some(x) if x != r => r = x,
                _ => break,
            }
        }
        r
    }

    fn layout_parent(&self, l: &Layout) -> String {
        if let Some(h) = &l.host_item_id {
            return format!("item/{h}");
        }
        if let Some(p) = &l.parent_address {
            let pr = self.place_route(Some(p));
            if pr.starts_with("room/") && Self::areas_of(&self.code_layouts(letters(&l.area))).len() > 1 {
                return format!("code/{}", letters(&l.area));
            }
            return pr;
        }
        format!("room/{}", letters(&l.area))
    }

    fn parent_of(&self, r: &str) -> Option<String> {
        let (k, a, _) = split_route(r);
        Some(match k {
            "" => return None,
            "room" => String::new(),
            "item" => self.place_route(self.item(a).and_then(|i| i.address.as_deref())),
            "code" => {
                let ls = self.code_layouts(a);
                ls.first().map_or(String::new(), |l| self.place_route(l.parent_address.as_deref()))
            }
            "area" => {
                let ls = self.area_layouts(a);
                let leads_back = |x: &&Layout| {
                    x.host_item_id.as_deref().is_some_and(|h| {
                        let host_addr = self.item(h).and_then(|i| i.address.as_deref()).unwrap_or("");
                        self.layout(key(host_addr)).is_some_and(|l| l.area == a)
                    })
                };
                let l = ls
                    .iter()
                    .find(|x| !Self::is_root(x) && !leads_back(x))
                    .or_else(|| ls.iter().find(|x| !Self::is_root(x)))
                    .or(ls.first());
                match l {
                    None => String::new(),
                    Some(l) if Self::is_root(l) => format!("room/{}", letters(&l.area)),
                    Some(l) => self.layout_parent(l),
                }
            }
            "level" => format!("area/{}", self.layout(a).map_or("", |l| l.area.as_str())),
            _ => String::new(),
        })
    }

    /// The breadcrumbs from home to `r` (pages that would redirect are left out).
    pub fn chain(&self, r: &str) -> Vec<String> {
        let mut out: Vec<String> = vec![];
        let mut c = Some(r.to_string());
        let mut n = 0;
        while let Some(cur) = c {
            if n >= 25 || out.contains(&cur) {
                break;
            }
            n += 1;
            out.insert(0, cur.clone());
            c = self.parent_of(&cur);
        }
        let last = out.len().saturating_sub(1);
        out.into_iter().enumerate().filter(|(i, x)| *i == last || self.resolve(x) == *x).map(|(_, x)| x).collect()
    }

    pub fn valid(&self, r: &str) -> bool {
        let (k, a, b) = split_route(r);
        match k {
            "" => true,
            "room" => !self.roots_of(a).is_empty(),
            "item" => self.i.contains_key(a),
            "code" => !self.code_layouts(a).is_empty(),
            "area" => self.area.contains_key(a),
            "level" => self.l.contains_key(a) && b.len() == 1 && b.chars().all(|c| c.is_ascii_digit()),
            _ => false,
        }
    }

    // ── names ────────────────────────────────────────────────────────────

    fn level_word(l: Option<&Layout>) -> &'static str {
        match l.and_then(|l| l.view.as_deref()) {
            Some("drawers") => "Drawer",
            Some("shelves") => "Shelf",
            _ => "Level",
        }
    }

    fn sole_host_of_area(&self, a: &str) -> Option<String> {
        let ls = self.area_layouts(a);
        let h = ls.first()?.host_item_id.clone()?;
        let all = ls.iter().all(|l| l.host_item_id.as_deref() == Some(h.as_str()));
        (all && self.resolve(&format!("item/{h}")) == format!("area/{a}")).then_some(h)
    }

    pub fn title(&self, r: &str) -> String {
        let (k, a, b) = split_route(r);
        match k {
            "" => "Home".into(),
            "room" => self.names.get(a).cloned().unwrap_or_else(|| {
                self.roots_of(a).first().and_then(|l| l.label.clone()).unwrap_or_else(|| a.to_string())
            }),
            "item" => self
                .host_code(a)
                .and_then(|c| self.names.get(&c).cloned())
                .or_else(|| self.item(a).map(|i| i.name.clone()))
                .unwrap_or_else(|| a.to_string()),
            "code" => self.names.get(a).cloned().unwrap_or_else(|| a.to_string()),
            "area" => {
                if let Some(n) = self.names.get(a) {
                    return n.clone();
                }
                if let Some(h) = self.sole_host_of_area(a) {
                    return self.title(&format!("item/{h}"));
                }
                let ls = self.area_layouts(a);
                let l = ls.iter().find(|x| x.prefix.as_deref() == Some("STD")).or(ls.first());
                l.and_then(|l| l.label.clone()).unwrap_or_else(|| a.to_string())
            }
            "level" => self
                .names
                .get(&format!("{a}{b}00000"))
                .cloned()
                .unwrap_or_else(|| format!("{} {b}", Self::level_word(self.layout(a)))),
            _ => r.to_string(),
        }
    }

    pub fn code(&self, r: &str) -> String {
        let (k, a, b) = split_route(r);
        match k {
            "room" => self.roots_of(a).iter().map(|l| l.key.as_str()).collect::<Vec<_>>().join(" · "),
            "item" => format!("{}#{a}", self.host_code(a).map(|c| format!("{c} · ")).unwrap_or_default()),
            "code" => a.to_string(),
            "area" => {
                let ls = self.area_layouts(a);
                let size = ls
                    .first()
                    .and_then(|l| Some(format!(" · {}×{}", l.columns.filter(|c| *c > 0)?, l.rows.unwrap_or(0))))
                    .unwrap_or_default();
                let prefixes: Vec<&str> = ls.iter().map(|l| l.prefix.as_deref().unwrap_or("")).collect();
                format!("{} {a}{size}", prefixes.join("+"))
            }
            "level" => format!("{a}{b}00000"),
            _ => String::new(),
        }
    }

    pub fn count(&self, r: &str) -> usize {
        let (k, a, b) = split_route(r);
        let cl = |l: &Layout| self.count_l.get(&l.key).copied().unwrap_or(0);
        match k {
            "room" => self.roots_of(a).into_iter().map(cl).sum(),
            "item" => self.count_i.get(a).copied().filter(|c| *c > 0).unwrap_or_else(|| self.kid_count(a)),
            "code" => self.code_layouts(a).into_iter().map(cl).sum(),
            "area" => self.area_layouts(a).into_iter().map(cl).sum(),
            "level" => {
                let d = b.chars().next();
                self.items_on(a).iter().filter(|i| i.address.as_deref().and_then(level_digit) == d).count()
                    + self
                        .sitting_on(a)
                        .into_iter()
                        .filter(|x| x.parent_address.as_deref().and_then(level_digit) == d)
                        .map(cl)
                        .sum::<usize>()
            }
            _ => 0,
        }
    }

    pub fn card(&self, r: &str) -> Card {
        Card { route: r.into(), title: self.title(r), code: self.code(r), count: self.count(r) }
    }

    /// Where an item lives, in words: "Desk drawer 2 · C03R05".
    pub fn place_title(&self, addr: Option<&str>) -> String {
        let r = self.place_route(addr);
        let Some(addr) = addr.filter(|_| !r.is_empty()) else { return addr.unwrap_or("no home").to_string() };
        let t = self.title(&r);
        match self.layout(key(addr)).and_then(|l| l.position_format.as_deref()) {
            Some("grid") => format!("{t} · {}", pos(addr)),
            Some("seq") if pos(addr) != "000000" => format!("{t} · spot {}", pos(addr).parse::<u64>().unwrap_or(0)),
            _ => t,
        }
    }

    // ── what a page shows (views.js) ─────────────────────────────────────

    fn cards_for(&self, items: Vec<Item>, layouts: Vec<&Layout>) -> (Vec<Card>, Vec<Item>) {
        let (mut cards, mut things) = (vec![], vec![]);
        let mut seen: Vec<String> = vec![];
        for i in items {
            if self.is_hosting(&i.id) {
                cards.push(self.card(&format!("item/{}", i.id)));
            } else {
                things.push(i);
            }
        }
        for l in layouts {
            let x = letters(&l.area).to_string();
            let in_room = l.parent_address.as_deref().is_some_and(|p| self.place_route(Some(p)).starts_with("room/"));
            if in_room && Self::areas_of(&self.code_layouts(&x)).len() > 1 {
                if !seen.contains(&x) {
                    cards.push(self.card(&format!("code/{x}")));
                    seen.push(x);
                }
            } else {
                cards.push(self.card(&format!("area/{}", l.area)));
            }
        }
        (cards, things)
    }

    pub fn page(&self, r: &str) -> Page {
        let (k, a, b) = split_route(r);
        let mut out = Page { things_head: "Things here".into(), ..Default::default() };
        match k {
            "" => {
                let mut rooms = self.room_codes();
                rooms.sort_by_key(|room| {
                    let route = format!("room/{room}");
                    (self.count(&route) == 0, self.title(&route).to_lowercase())
                });
                out.cards = rooms.iter().map(|room| self.card(&format!("room/{room}"))).collect();
            }
            "room" => {
                let roots = self.roots_of(a);
                let items = self.top_only(roots.iter().flat_map(|l| self.items_on(&l.key)).collect());
                let lays: Vec<&Layout> =
                    roots.iter().flat_map(|l| self.sitting_on(&l.key)).filter(|x| x.host_item_id.is_none()).collect();
                (out.cards, out.things) = self.cards_for(items, lays);
            }
            "item" => {
                let areas = Self::areas_of(&self.hosted_by(a));
                let levelled: Vec<&String> = areas
                    .iter()
                    .filter(|x| self.area_layouts(x).iter().any(|l| l.position_format.as_deref() == Some("level")))
                    .collect();
                let inline = (levelled.len() == 1).then(|| levelled[0].clone());
                out.cards = areas
                    .iter()
                    .filter(|x| Some(*x) != inline.as_ref())
                    .map(|x| self.card(&format!("area/{x}")))
                    .collect();
                if let Some(x) = inline {
                    out.inline = Some((self.title(&format!("area/{x}")), format!("area/{x}")));
                    out.zones = self.zones(&x);
                }
                out.things = self.kids_of(a);
                out.things_head = "Inside".into();
            }
            "code" => {
                out.cards =
                    Self::areas_of(&self.code_layouts(a)).iter().map(|x| self.card(&format!("area/{x}"))).collect()
            }
            "area" => out.zones = self.zones(a),
            "level" => {
                let d = b.chars().next();
                let its = self.top_only(
                    self.items_on(a).into_iter().filter(|i| i.address.as_deref().and_then(level_digit) == d).collect(),
                );
                let st: Vec<&Layout> = self
                    .sitting_on(a)
                    .into_iter()
                    .filter(|x| x.parent_address.as_deref().and_then(level_digit) == d)
                    .collect();
                (out.cards, out.things) = self.cards_for(its, st);
                out.things_head = format!("In this {}", Self::level_word(self.layout(a)).to_lowercase());
            }
            _ => {}
        }
        out
    }

    fn zones(&self, a: &str) -> Vec<Zone> {
        let mut out = vec![];
        for l in self.area_layouts(a) {
            let zone = match l.position_format.as_deref() {
                Some("grid") => self.grid(l),
                Some("level") => Zone::Levels { layout: l.clone(), levels: self.levels(l) },
                Some("sorted") => {
                    Zone::Media { layout: l.clone(), items: Self::in_order(self.top_only(self.items_on(&l.key))) }
                }
                _ => {
                    let its = self.top_only(self.items_on(&l.key));
                    let (hosting, plain): (Vec<Item>, Vec<Item>) =
                        its.into_iter().partition(|i| self.is_hosting(&i.id));
                    let (cards, _) = self.cards_for(hosting, self.sitting_on(&l.key));
                    let mut spots: Vec<(String, Vec<Item>)> = vec![];
                    for i in plain {
                        let spot = pos(i.address.as_deref().unwrap_or("")).to_string();
                        match spots.iter_mut().find(|(s, _)| *s == spot) {
                            Some((_, v)) => v.push(i),
                            None => spots.push((spot, vec![i])),
                        }
                    }
                    spots.sort_by(|x, y| x.0.cmp(&y.0));
                    Zone::List { layout: l.clone(), cards, spots }
                }
            };
            out.push(zone);
        }
        out
    }

    fn grid(&self, l: &Layout) -> Zone {
        let on = self.items_on(&l.key);
        let (mut c, mut r) = (l.columns.unwrap_or(0), l.rows.unwrap_or(0));
        for i in &on {
            if let Some((ic, ir)) = cell(pos(i.address.as_deref().unwrap_or(""))) {
                c = c.max(ic + i.span_columns.unwrap_or(1) - 1);
                r = r.max(ir + i.span_rows.unwrap_or(1) - 1);
            }
        }
        let top =
            self.top_only(on.into_iter().filter(|i| cell(pos(i.address.as_deref().unwrap_or(""))).is_some()).collect());
        let blocks = top
            .into_iter()
            .map(|i| {
                let (bc, br) = cell(pos(i.address.as_deref().unwrap_or(""))).unwrap_or((1, 1));
                let confirmed = i
                    .footprint_confirmed
                    .as_ref()
                    .is_some_and(|v| v.as_bool() == Some(true) || v.as_i64().unwrap_or(0) != 0);
                Block {
                    c: bc,
                    r: br,
                    w: i.span_columns.unwrap_or(1),
                    h: i.span_rows.unwrap_or(1),
                    kids: self.kid_count(&i.id),
                    assumed: i.span_columns.is_none() || !confirmed,
                    item: i,
                }
            })
            .collect();
        let subs = self
            .sitting_on(&l.key)
            .into_iter()
            .filter_map(|s| {
                let (sc, sr) = cell(pos(s.parent_address.as_deref().unwrap_or("")))?;
                Some(Sub { layout: s.clone(), c: sc, r: sr, route: format!("area/{}", s.area) })
            })
            .collect();
        Zone::Grid {
            layout: l.clone(),
            cols: c.max(1),
            rows: r.max(1),
            blocks,
            subs,
            compartments: l.view.as_deref() == Some("compartments"),
        }
    }

    fn levels(&self, l: &Layout) -> Vec<Level> {
        let digit = |a: Option<&str>| a.and_then(level_digit).and_then(|d| d.to_digit(10)).unwrap_or(0) as i64;
        let on = self.items_on(&l.key);
        let sits = self.sitting_on(&l.key);
        let n = [l.levels.unwrap_or(0), 1]
            .into_iter()
            .chain(on.iter().map(|i| digit(i.address.as_deref())))
            .chain(sits.iter().map(|s| digit(s.parent_address.as_deref())))
            .max()
            .unwrap_or(1);
        (1..=n)
            .rev()
            .map(|f| {
                let name = self
                    .names
                    .get(&format!("{}{f}00000", l.key))
                    .cloned()
                    .unwrap_or_else(|| format!("{} {f}", Self::level_word(Some(l))));
                Level {
                    route: format!("level/{}/{f}", l.key),
                    name,
                    place: if f == n {
                        "top"
                    } else if f == 1 {
                        "bottom"
                    } else {
                        ""
                    },
                    subs: sits
                        .iter()
                        .filter(|s| digit(s.parent_address.as_deref()) == f)
                        .map(|s| (*s).clone())
                        .collect(),
                    items: self.top_only(on.iter().copied().filter(|i| digit(i.address.as_deref()) == f).collect()),
                }
            })
            .collect()
    }

    // ── sorting and search ───────────────────────────────────────────────

    /// The title things sort by: the sort key, else the name without a leading article.
    pub fn sort_title(i: &Item) -> String {
        if let Some(k) = i.sort_key.as_deref().filter(|k| !k.trim().is_empty()) {
            return k.trim().to_string();
        }
        let name = i.name.trim();
        let mut words = name.splitn(2, char::is_whitespace);
        let first = words.next().unwrap_or("");
        match words.next() {
            Some(rest)
                if ARTICLE_WORDS.contains(&first.to_lowercase().as_str()) || first.eq_ignore_ascii_case("ett") =>
            {
                rest.trim().to_string()
            }
            _ => name.to_string(),
        }
    }

    /// Swedish order (å ä ö after z), numbers by value.
    pub fn in_order(mut items: Vec<Item>) -> Vec<Item> {
        let collate = |t: &str| {
            let t: String = t
                .to_lowercase()
                .chars()
                .map(|c| match c {
                    'å' => "{1".to_string(),
                    'ä' | 'æ' => "{2".to_string(),
                    'ö' | 'ø' => "{3".to_string(),
                    c => c.to_string(),
                })
                .collect();
            let mut out = String::new();
            let mut digits = String::new();
            for c in t.chars().chain(std::iter::once('\0')) {
                if c.is_ascii_digit() {
                    digits.push(c);
                    continue;
                }
                if !digits.is_empty() {
                    out.push_str(&format!("{digits:0>8}"));
                    digits.clear();
                }
                if c != '\0' {
                    out.push(c);
                }
            }
            out
        };
        items.sort_by_key(|i| (collate(&Self::sort_title(i)), i.id.clone()));
        items
    }

    /// Other words for a search word (Swedish endings stripped, synonyms).
    fn equivalents(&self, t: &str) -> Vec<String> {
        let mut found: HashSet<String> = HashSet::new();
        let mut forms = vec![t.to_string()];
        for e in ENDINGS {
            if t.ends_with(e) && t.chars().count() >= e.chars().count() + 3 {
                forms.push(t[..t.len() - e.len()].to_string());
            }
        }
        for w in &forms {
            if let Some(others) = self.synonyms.get(w) {
                found.insert(w.clone());
                found.extend(others.iter().cloned());
            }
        }
        if found.is_empty() && t.chars().count() >= 4 {
            for (w, others) in &self.synonyms {
                if w.starts_with(t) {
                    found.insert(w.clone());
                    found.extend(others.iter().cloned());
                }
            }
        }
        found.remove(t);
        found.into_iter().collect()
    }

    fn matches(&self, i: &Item, q: &str) -> bool {
        if q.trim().is_empty() {
            return true;
        }
        let fields = [
            Some(i.name.as_str()),
            Some(i.id.as_str()),
            i.address.as_deref(),
            i.spec_code.as_deref(),
            i.category.as_deref(),
            i.sort_key.as_deref(),
            i.search_words.as_deref(),
        ];
        let mut hay: String = fields.iter().flatten().copied().collect::<Vec<_>>().join(" ");
        if let Some(n) = self.nick.get(&i.id) {
            hay.push(' ');
            hay.push_str(&n.join(" "));
        }
        let hay = hay.to_lowercase();
        let starts: String = format!(
            " {}",
            hay.chars()
                .map(|c| if c.is_ascii_lowercase() || c.is_ascii_digit() || "åäöé".contains(c) { c } else { ' ' })
                .collect::<String>()
        );
        q.to_lowercase()
            .split_whitespace()
            .all(|t| hay.contains(t) || self.equivalents(t).iter().any(|o| starts.contains(&format!(" {o}"))))
    }

    pub fn search(&self, q: &str, status: Option<&str>) -> Vec<Item> {
        let live: Vec<Item> = self
            .items
            .iter()
            .filter(|i| i.status() != "retired")
            .filter(|i| status.is_none_or(|s| i.status() == s))
            .filter(|i| self.matches(i, q))
            .cloned()
            .collect();
        Self::in_order(live).into_iter().take(200).collect()
    }

    pub fn status_counts(&self) -> HashMap<String, usize> {
        let mut out = HashMap::new();
        for i in &self.items {
            *out.entry(i.status().to_string()).or_default() += 1;
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn world() -> World {
        World::new(json!({
            "layouts": [
                {"key": "STDKON0001", "area": "KON0001", "label": "Office", "prefix": "STD"},
                {"key": "DRWDES0001", "area": "DES0001", "label": "Desk", "prefix": "DRW", "parent_address": "STDKON0001000001",
                 "position_format": "level", "view": "drawers", "levels": 3},
                {"key": "GRIDES0002", "area": "DES0002", "label": "Bin grid", "prefix": "GRI", "parent_address": "DRWDES0001200000",
                 "position_format": "grid", "view": "gridfinity", "columns": 4, "rows": 2},
            ],
            "items": [
                {"id": "00000001", "name": "The Soldering iron", "address": "GRIDES0002C02R01", "category": "tools", "span_columns": 2, "span_rows": 1, "footprint_confirmed": true},
                {"id": "00000002", "name": "Skruvarna", "address": "DRWDES0001100000", "status": "away", "category": "bits"},
                {"id": "00000003", "name": "Ålkorv", "address": "STDKON0001000001"},
            ],
            "synonyms": {"skruv": ["screw", "skruvarna"]},
            "aliases": [{"target": "00000003", "alias": "the sausage", "display": false}],
        }))
    }

    #[test]
    fn routes_titles_and_counts() {
        let w = world();
        assert_eq!(w.page("").cards.iter().map(|c| c.route.as_str()).collect::<Vec<_>>(), vec!["room/KON"]);
        assert_eq!(w.title("room/KON"), "Office");
        assert_eq!(w.count("room/KON"), 3);
        // Drawer 2 holds only the bin grid: the level page skips straight to it.
        assert_eq!(w.resolve("level/DRWDES0001/2"), "area/DES0002");
        assert_eq!(w.chain("area/DES0002"), vec!["", "room/KON", "code/DES", "area/DES0001", "area/DES0002"]);
        assert_eq!(w.place_title(Some("GRIDES0002C02R01")), "Bin grid · C02R01");
        assert_eq!(w.title("level/DRWDES0001/1"), "Drawer 1");
        let Zone::Grid { cols, blocks, .. } = &w.page("area/DES0002").zones[0] else { panic!("a grid") };
        assert_eq!((*cols, blocks[0].w, blocks[0].assumed), (4, 2, false));
        let Zone::Levels { levels, .. } = &w.page("area/DES0001").zones[0] else { panic!("levels") };
        assert_eq!(levels.iter().map(|l| l.place).collect::<Vec<_>>(), vec!["top", "", "bottom"]);
    }

    #[test]
    fn search_with_swedish_endings_synonyms_and_nicknames() {
        let w = world();
        assert_eq!(w.search("skruvar", None)[0].id, "00000002", "skruvar → skruv (an ending off)");
        assert_eq!(w.search("sausage", None)[0].id, "00000003");
        assert_eq!(w.search("", Some("away")).len(), 1);
        let order: Vec<String> = World::in_order(w.items.clone()).into_iter().map(|i| i.name).collect();
        assert_eq!(order, vec!["Skruvarna", "The Soldering iron", "Ålkorv"], "the article is skipped, å sorts last");
    }

    /// Compare with the Python model: TK_WORLD=world.json cargo test parity -- --ignored --nocapture
    #[test]
    #[ignore]
    fn parity_dump() {
        let Ok(path) = std::env::var("TK_WORLD") else { return };
        let w = World::new(serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap());
        let mut todo = vec![String::new()];
        let mut seen = HashSet::new();
        while let Some(r) = todo.pop() {
            if !seen.insert(r.clone()) {
                continue;
            }
            let page = w.page(&r);
            println!(
                "R {r} | {} | {} | {} | {} | {}",
                w.title(&r),
                w.code(&r),
                w.count(&r),
                w.resolve(&r),
                w.chain(&r).join(">")
            );
            println!(
                "P {r} | cards {} | things {} | zones {} | {}",
                page.cards.iter().map(|c| c.route.as_str()).collect::<Vec<_>>().join(","),
                page.things.iter().map(|i| i.id.as_str()).collect::<Vec<_>>().join(","),
                page.zones.len(),
                page.things_head
            );
            for c in &page.cards {
                todo.push(c.route.clone());
            }
            for z in &page.zones {
                if let Zone::Levels { levels, .. } = z {
                    todo.extend(levels.iter().map(|l| l.route.clone()));
                }
            }
        }
        for q in ["skruv", "kabel", "usb", "gridfinity", "bok", "hammare", "lampa", "00000002", "des", "printer"] {
            println!("S {q} | {}", w.search(q, None).iter().map(|i| i.id.as_str()).collect::<Vec<_>>().join(","));
        }
        for i in w.items.iter().take(300) {
            println!("T {} | {}", i.id, w.place_title(i.address.as_deref()));
        }
    }
}
