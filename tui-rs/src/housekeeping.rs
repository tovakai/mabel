//! Mabel's Gremlin housekeeping suggestions, read from (and ticked in) the Obsidian note she writes.
//!
//! Lines look like:  - [ ] **Generic 1×4 Gridfinity bin** `00000217`: spec ID: GFST0104 _(check which one is right)_
//! A suggestion whose change is "spec ID: …", "name: …" or "category: …" can be applied straight
//! to tk-index (one edit per item, undoable there); anything else ("specs: measure the gauge") is
//! a to-do for him, which he ticks off himself.

use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use regex::Regex;
use serde_json::{Value, json};

pub const NOTE: &str = "daily life of Anthon/Gremlin housekeeping.md";

static LINE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^(?P<lead>\s*[-*]\s+\[)(?P<mark>[ xX])(?P<rest>\]\s+\*\*(?P<name>.+?)\*\*\s+(?P<ids>(?:`\d{8}`(?:,\s*)?)+):\s*(?P<change>.+?)(?:\s+_\((?P<why>.*)\)_)?\s*)$")
        .expect("regex")
});
static SECTION: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^##\s+(.+?)\s*$").expect("regex"));
static ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\d{8}").expect("regex"));

#[derive(Debug, Clone)]
pub struct Suggestion {
    pub line_no: usize,
    pub raw: String,
    pub section: String,
    pub name: String,
    pub ids: Vec<String>,
    pub change: String,
    pub why: String,
    pub done: bool,
    /// {"spec_code": "GFST0104"} when it can be applied.
    pub edit: Option<Value>,
}

impl Suggestion {
    pub fn applicable(&self) -> bool {
        self.edit.is_some()
    }
}

pub fn note_path(vault: &str) -> PathBuf {
    crate::config::expand(vault).join(NOTE)
}

pub fn parse(text: &str) -> Vec<Suggestion> {
    let mut out = vec![];
    let mut section = String::new();
    for (n, line) in text.lines().enumerate() {
        if let Some(h) = SECTION.captures(line) {
            section = h[1].to_string();
            continue;
        }
        let Some(m) = LINE.captures(line) else { continue };
        let change = m["change"].trim().to_string();
        let (field, value) = change.split_once(':').unwrap_or((&change, ""));
        let key = match field.trim().to_lowercase().as_str() {
            "spec id" | "spec" => Some("spec_code"),
            "name" => Some("name"),
            "category" => Some("category"),
            _ => None,
        };
        let edit = key.filter(|_| !value.trim().is_empty()).map(|k| json!({ k: value.trim() }));
        out.push(Suggestion {
            line_no: n,
            raw: line.to_string(),
            section: section.clone(),
            name: m["name"].to_string(),
            ids: ID.find_iter(&m["ids"]).map(|x| x.as_str().to_string()).collect(),
            change,
            why: m.name("why").map_or(String::new(), |w| w.as_str().trim().to_string()),
            done: &m["mark"] != " ",
            edit,
        });
    }
    out
}

pub fn load(vault: &str) -> Vec<Suggestion> {
    std::fs::read_to_string(note_path(vault)).map(|t| parse(&t)).unwrap_or_default()
}

/// Tick (or untick) that exact line; false if the note changed under us.
pub fn tick(vault: &str, s: &Suggestion, done: bool) -> bool {
    let path = note_path(vault);
    tick_in(&path, s, done)
}

fn tick_in(path: &Path, s: &Suggestion, done: bool) -> bool {
    let Ok(text) = std::fs::read_to_string(path) else { return false };
    let mut lines: Vec<String> = text.split_inclusive('\n').map(String::from).collect();
    let same = |l: &String| l.trim_end_matches('\n') == s.raw;
    let n = if lines.get(s.line_no).is_some_and(same) {
        s.line_no
    } else {
        match lines.iter().position(same) {
            Some(n) => n,
            None => return false,
        }
    };
    let ending = if lines[n].ends_with('\n') { "\n" } else { "" };
    let Some(m) = LINE.captures(lines[n].trim_end_matches('\n')) else { return false };
    let new = format!("{}{}{}{ending}", &m["lead"], if done { "x" } else { " " }, &m["rest"]);
    lines[n] = new;
    let tmp = path.with_extension("md.tmp");
    std::fs::write(&tmp, lines.concat()).and_then(|()| std::fs::rename(&tmp, path)).is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_and_tick() {
        let text = "# Gremlin\n## Spec IDs\n- [ ] **Generic 1×4 bin** `00000217`, `00000218`: spec ID: GFST0104 _(check it)_\n- [x] **Old** `00000001`: name: Older\n## To do\n- [ ] **Caliper** `00000300`: specs: measure the gauge\n";
        let s = parse(text);
        assert_eq!(s.len(), 3);
        assert_eq!((s[0].section.as_str(), s[0].ids.len(), s[0].why.as_str()), ("Spec IDs", 2, "check it"));
        assert_eq!(s[0].edit, Some(json!({"spec_code": "GFST0104"})));
        assert!(s[1].done && !s[2].applicable());
        let dir = std::env::temp_dir().join(format!("mabel-hk-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("note.md");
        std::fs::write(&path, text).unwrap();
        assert!(tick_in(&path, &s[2], true));
        assert!(std::fs::read_to_string(&path).unwrap().contains("- [x] **Caliper**"));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
