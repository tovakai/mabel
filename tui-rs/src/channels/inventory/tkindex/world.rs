//! The whole index in memory (`World`): indexes for fast lookups, routes (where you are),
//! redirects, parents, titles, codes, counts, and building a page.

use super::*;

/// The whole index in memory, as tk-index's /api/world sends it, plus lookups built once by
/// `index` (the short names match the web UI's core.js). The `usize`s are positions in
/// `layouts` / `items`.
pub struct World {
    /// Every place (room, furniture, drawer, grid…) and every item.
    pub layouts: Vec<Layout>,
    pub items: Vec<Item>,
    /// Search synonyms: word → words that mean the same.
    pub(super) synonyms: HashMap<String, Vec<String>>,
    /// Category → icon.
    pub icons: HashMap<String, String>,
    /// Place key → its layout; item id → the item.
    pub(super) l: HashMap<String, usize>,
    pub(super) i: HashMap<String, usize>,
    /// Place key → the items at it.
    pub(super) on: HashMap<String, Vec<usize>>,
    /// Item id → the items inside it.
    pub(super) kids: HashMap<String, Vec<usize>>,
    /// Item id → the places it hosts (a cabinet's drawers).
    pub(super) hosted: HashMap<String, Vec<usize>>,
    /// Area code → its layouts (in view order).
    pub(super) area: HashMap<String, Vec<usize>>,
    /// Place key → the layouts sitting on it (a box on a shelf).
    pub(super) sit: HashMap<String, Vec<usize>>,
    /// Display names (aliases shown instead of the real name) and nicknames (found by search).
    pub(super) names: HashMap<String, String>,
    pub(super) nick: HashMap<String, Vec<String>>,
    /// How many things are under each place, and inside each hosting item, all the way down.
    pub(super) count_l: HashMap<String, usize>,
    pub(super) count_i: HashMap<String, usize>,
}

/// A route's three parts: "level/DRWLHE0001/3" → ("level", "DRWLHE0001", "3").
pub(super) fn split_route(r: &str) -> (&str, &str, &str) {
    let mut parts = r.splitn(3, '/');
    (parts.next().unwrap_or(""), parts.next().unwrap_or(""), parts.next().unwrap_or(""))
}

impl World {
    /// The index from /api/world's JSON, with its lookups built.
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

    /// Build the lookups (see the fields), names and nicknames from the aliases, and the counts.
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

    /// The place with key `k`.
    pub fn layout(&self, k: &str) -> Option<&Layout> {
        self.l.get(k).map(|n| &self.layouts[*n])
    }

    /// The item with id `id`.
    pub fn item(&self, id: &str) -> Option<&Item> {
        self.i.get(id).map(|n| &self.items[*n])
    }

    /// The layouts a lookup lists under `k`.
    fn many(&self, map: &HashMap<String, Vec<usize>>, k: &str) -> Vec<&Layout> {
        map.get(k).map(|v| v.iter().map(|n| &self.layouts[*n]).collect()).unwrap_or_default()
    }

    /// The items at place `k`.
    fn items_on(&self, k: &str) -> Vec<&Item> {
        self.on.get(k).map(|v| v.iter().map(|n| &self.items[*n]).collect()).unwrap_or_default()
    }

    /// The layouts in area `a`.
    fn area_layouts(&self, a: &str) -> Vec<&Layout> {
        self.many(&self.area, a)
    }

    /// The places item `id` hosts.
    fn hosted_by(&self, id: &str) -> Vec<&Layout> {
        self.many(&self.hosted, id)
    }

    /// The layouts sitting on place `k`.
    fn sitting_on(&self, k: &str) -> Vec<&Layout> {
        self.many(&self.sit, k)
    }

    /// How many things are inside item `id`.
    pub fn kid_count(&self, id: &str) -> usize {
        self.kids.get(id).map_or(0, |v| v.len())
    }

    /// Does item `id` host places (is it furniture)?
    pub fn is_hosting(&self, id: &str) -> bool {
        self.hosted.get(id).is_some_and(|v| !v.is_empty())
    }

    /// A top-level place: not hosted by an item, not sitting on another place (a room's own areas).
    fn is_root(l: &Layout) -> bool {
        l.host_item_id.is_none() && l.parent_address.is_none()
    }

    /// A room's top-level places.
    fn roots_of(&self, room: &str) -> Vec<&Layout> {
        self.layouts.iter().filter(|l| Self::is_root(l) && letters(&l.area) == room).collect()
    }

    /// Every room's code (the letters of its top-level areas), in order.
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

    /// The layouts whose area has the letters `x`.
    fn code_layouts(&self, x: &str) -> Vec<&Layout> {
        self.layouts
            .iter()
            .filter(|l| letters(&l.area) == x && l.host_item_id.is_none() && l.parent_address.is_some())
            .collect()
    }

    /// The distinct areas of some layouts, in order.
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

    /// The area letters an item's hosted places share, if there's exactly one (its "code").
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

    /// Where a route goes instead, if its page would hold a single thing (an item hosting one
    /// area opens that area, and so on), like the web UI.
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

    /// The route above a place (its host item, the place it sits on, or its room).
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

    /// The route above route `r` (None at home).
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

    /// Does route `r` lead somewhere that exists?
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

    /// What a level is called in a layout's view: Drawer, Shelf, or Level.
    fn level_word(l: Option<&Layout>) -> &'static str {
        match l.and_then(|l| l.view.as_deref()) {
            Some("drawers") => "Drawer",
            Some("shelves") => "Shelf",
            _ => "Level",
        }
    }

    /// The item hosting all of area `a`, when the item's own page redirects to it (so the area
    /// can be titled after the item).
    fn sole_host_of_area(&self, a: &str) -> Option<String> {
        let ls = self.area_layouts(a);
        let h = ls.first()?.host_item_id.clone()?;
        let all = ls.iter().all(|l| l.host_item_id.as_deref() == Some(h.as_str()));
        (all && self.resolve(&format!("item/{h}")) == format!("area/{a}")).then_some(h)
    }

    /// A route's title, as the web UI shows it.
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

    /// A route's short code (shown on cards and breadcrumbs).
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

    /// How many things are under a route, all the way down.
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

    /// A place card for a route (title, code, count).
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

    /// Split things into cards (hosting items and places, which you zoom into) and plain items.
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

    /// Everything shown for a route: cards to zoom into, zones (maps, levels, spot lists), and
    /// the things there.
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

    /// An area's zones: a grid as a map, drawers/shelves as levels, otherwise spot lists.
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

    /// A grid layout as a map: its cells, and the blocks (bins, items with footprints) on it.
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

    /// A drawers/shelves layout's levels, each with its spots and things.
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
}
