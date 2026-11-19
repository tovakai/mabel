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

// The model is split by layer; everything is re-exported, so callers use `tkindex::…`.
/// the HTTP API
mod api;
/// layouts, items, addresses, page pieces
mod model;
/// sorting and search
mod search;
/// the whole index: routes, titles, pages
mod world;

pub use api::*;
pub use model::*;
pub use world::*;

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
