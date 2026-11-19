//! INVENTORY's page, built as blocks: the places, each zone's spots and things, search
//! results, suggestions. `rebuild` makes them whenever what's shown changes.

use super::*;

impl Inventory {
    // ── the page, as blocks ──────────────────────────────────────────────

    /// An item as one line: a dot in its category colour (red when it's checked out), its
    /// name, its id, how many things are inside it, and its status if it isn't at home.
    pub(super) fn thing_line(&self, item: &Item, prefix: &str, pal: &Palette) -> Line<'static> {
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

    /// A heading block.
    pub(super) fn head(text: String, pal: &Palette) -> Block {
        Block::Text(Line::styled(text, bold(pal.beige)))
    }

    /// Build the page's blocks for what's shown now: a place (breadcrumbs, the places inside
    /// it as cards, its zones, its things), search results, the inbox, or loose ends.
    pub(super) fn rebuild(&mut self, pal: &Palette) {
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

    /// One zone of a place: a grid zone as a map, or levels (drawers, shelves) and spot lists,
    /// with the things in each spot.
    pub(super) fn zone_blocks(&self, zi: usize, blocks: &mut Vec<Block>, pal: &Palette) {
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
                let mut sorted: Vec<&crate::channels::life::inventory::tkindex::Block> = bins.iter().collect();
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
}
