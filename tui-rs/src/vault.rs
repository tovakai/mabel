//! The Obsidian vault for the VAULT channel: notes, links, tags, headings, backlinks, search,
//! rename, daily notes. Read straight from disk (his PC's vault, or the deck's Syncthing copy).

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::SystemTime;

use anyhow::{Result, anyhow, bail};
use chrono::{Datelike, Duration, Local, NaiveDate, NaiveDateTime, Timelike};
use regex::Regex;

pub static WIKILINK: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(!?)\[\[([^\]\|#]*)(#[^\]\|]*)?(?:\|([^\]]*))?\]\]").expect("regex"));
/// #tag, not inside a word, a path or an &entity; the first character is the one before it.
pub static INLINE_TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(^|[^\w/#&])#(\p{L}[\w/\-]*)").expect("regex"));
static HEADING: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(#{1,6})\s+(.+?)\s*#*\s*$").expect("regex"));

#[derive(Debug, Clone)]
pub struct Note {
    pub path: PathBuf,
    /// Vault-relative, with .md.
    pub rel: String,
    /// The file stem (what [[links]] use).
    pub name: String,
    pub mtime: SystemTime,
    /// key → value(s), in file order.
    pub frontmatter: Vec<(String, Vec<String>)>,
    /// Raw link targets (names or paths), no .md.
    pub links: Vec<String>,
    pub tags: Vec<String>,
    /// (level, text, line in the file).
    pub headings: Vec<(usize, String, usize)>,
}

impl Note {
    pub fn folder(&self) -> &str {
        self.rel.rsplit_once('/').map_or("", |(f, _)| f)
    }
}

/// Frontmatter in a small YAML subset (what notes use): `key: value`, `key: [a, b]`, and
/// `key:` followed by `- item` lines. Returns (pairs, body, lines the frontmatter took).
pub fn split_frontmatter(text: &str) -> (Vec<(String, Vec<String>)>, &str, usize) {
    let Some(rest) = text.strip_prefix("---\n") else { return (vec![], text, 0) };
    let Some(end) = rest.find("\n---") else { return (vec![], text, 0) };
    let head = &rest[..end];
    let mut body = &rest[end + 4..];
    body = body.strip_prefix('\n').unwrap_or(body);
    let mut pairs: Vec<(String, Vec<String>)> = vec![];
    let clean = |v: &str| v.trim().trim_matches(|c| c == '"' || c == '\'').to_string();
    for line in head.lines() {
        let trimmed = line.trim_start();
        if let Some(item) = trimmed.strip_prefix("- ") {
            if let Some((_, values)) = pairs.last_mut() {
                values.push(clean(item));
            }
            continue;
        }
        let Some((key, value)) = line.split_once(':') else { continue };
        if line.starts_with(' ') || key.trim().is_empty() {
            continue;
        }
        let value = value.trim();
        let values = if let Some(list) = value.strip_prefix('[').and_then(|v| v.strip_suffix(']')) {
            list.split(',').map(clean).filter(|v| !v.is_empty()).collect()
        } else if value.is_empty() {
            vec![]
        } else {
            vec![clean(value)]
        };
        pairs.push((key.trim().to_string(), values));
    }
    // The opening ---, the head's lines, the closing ---: the body starts after them.
    (pairs, body, head.matches('\n').count() + 3)
}

type Scan = (Vec<(String, Vec<String>)>, Vec<String>, Vec<String>, Vec<(usize, String, usize)>);

/// Frontmatter, links, tags and headings of a note's text (fenced code is skipped).
pub fn scan_text(text: &str) -> Scan {
    let (fm, body, offset) = split_frontmatter(text);
    let (mut links, mut tags, mut heads) = (vec![], vec![], vec![]);
    let mut fenced = false;
    for (n, line) in body.lines().enumerate() {
        let t = line.trim();
        if t.starts_with("```") || t.starts_with("~~~") {
            fenced = !fenced;
            continue;
        }
        if fenced {
            continue;
        }
        for m in WIKILINK.captures_iter(line) {
            let target = m[2].trim();
            if !target.is_empty() {
                links.push(target.to_string());
            }
        }
        for m in INLINE_TAG.captures_iter(line) {
            let tag = m[2].to_string();
            if !tags.contains(&tag) {
                tags.push(tag);
            }
        }
        if let Some(h) = HEADING.captures(line) {
            heads.push((h[1].len(), h[2].to_string(), n + offset));
        }
    }
    for (key, values) in &fm {
        if key == "tags" || key == "tag" {
            for v in values.iter().flat_map(|v| v.split([',', ' '])) {
                let tag = v.trim().trim_start_matches('#').to_string();
                if !tag.is_empty() && !tags.contains(&tag) {
                    tags.push(tag);
                }
            }
        }
    }
    (fm, links, tags, heads)
}

pub struct Vault {
    pub root: PathBuf,
    /// rel → note.
    pub notes: BTreeMap<String, Note>,
    by_name: HashMap<String, Vec<String>>,
}

fn last_segment(target: &str) -> String {
    target.trim().trim_end_matches(".md").rsplit('/').next().unwrap_or("").to_lowercase()
}

impl Vault {
    pub fn new(root: PathBuf) -> Self {
        Vault { root, notes: BTreeMap::new(), by_name: HashMap::new() }
    }

    pub fn exists(&self) -> bool {
        self.root.is_dir()
    }

    // ── scanning ─────────────────────────────────────────────────────────

    pub fn scan(mut self) -> Self {
        let mut stack = vec![self.root.clone()];
        while let Some(dir) = stack.pop() {
            let Ok(entries) = std::fs::read_dir(&dir) else { continue };
            for e in entries.flatten() {
                let name = e.file_name().to_string_lossy().into_owned();
                if name.starts_with('.') {
                    continue;
                }
                let path = e.path();
                if path.is_dir() {
                    stack.push(path);
                } else if name.ends_with(".md") {
                    let note = self.read_note(&path);
                    self.notes.insert(note.rel.clone(), note);
                }
            }
        }
        self.index();
        self
    }

    fn read_note(&self, path: &Path) -> Note {
        let rel = path.strip_prefix(&self.root).unwrap_or(path).to_string_lossy().into_owned();
        let text = std::fs::read_to_string(path).unwrap_or_default();
        let mtime = std::fs::metadata(path).and_then(|m| m.modified()).unwrap_or(SystemTime::UNIX_EPOCH);
        let (frontmatter, links, tags, headings) = scan_text(&text);
        let name = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
        Note { path: path.to_path_buf(), rel, name, mtime, frontmatter, links, tags, headings }
    }

    fn index(&mut self) {
        self.by_name.clear();
        for note in self.notes.values() {
            self.by_name.entry(note.name.to_lowercase()).or_default().push(note.rel.clone());
        }
    }

    pub fn refresh_note(&mut self, rel: &str) -> Option<&Note> {
        let path = self.root.join(rel);
        if !path.exists() {
            self.notes.remove(rel);
            self.index();
            return None;
        }
        let note = self.read_note(&path);
        self.notes.insert(rel.to_string(), note);
        self.index();
        self.notes.get(rel)
    }

    // ── links ────────────────────────────────────────────────────────────

    /// A [[target]] → the note's rel: by path if it has a slash, else by name (the source's
    /// folder first when several share the name).
    pub fn resolve(&self, target: &str, source: Option<&Note>) -> Option<String> {
        let t = target.trim().trim_end_matches(".md");
        if t.is_empty() {
            return source.map(|s| s.rel.clone());
        }
        if t.contains('/') {
            let direct = format!("{t}.md");
            if self.notes.contains_key(&direct) {
                return Some(direct);
            }
        }
        let mut found: Vec<&String> = self.by_name.get(&last_segment(t))?.iter().collect();
        if let Some(src) = source {
            found.sort_by_key(|rel| (rel.rsplit_once('/').map_or("", |(f, _)| f) != src.folder(), rel.len()));
        }
        found.first().map(|r| (*r).clone())
    }

    pub fn backlinks(&self, note: &Note) -> Vec<&Note> {
        let name = note.name.to_lowercase();
        let mut out: Vec<&Note> = self
            .notes
            .values()
            .filter(|other| other.rel != note.rel)
            .filter(|other| {
                other
                    .links
                    .iter()
                    .any(|t| last_segment(t) == name && self.resolve(t, Some(other)).as_deref() == Some(&note.rel))
            })
            .collect();
        out.sort_by_key(|n| n.rel.to_lowercase());
        out
    }

    /// tag → notes, most used first.
    pub fn tags(&self) -> Vec<(String, Vec<&Note>)> {
        let mut map: HashMap<&str, Vec<&Note>> = HashMap::new();
        for note in self.notes.values() {
            for t in &note.tags {
                map.entry(t.as_str()).or_default().push(note);
            }
        }
        let mut out: Vec<(String, Vec<&Note>)> = map.into_iter().map(|(t, n)| (t.to_string(), n)).collect();
        out.sort_by_key(|(t, n)| (std::cmp::Reverse(n.len()), t.to_lowercase()));
        out
    }

    // ── search ───────────────────────────────────────────────────────────

    /// Fuzzy match on names and paths (quick open, the [[ picker). Empty: the newest notes.
    pub fn find_notes(&self, query: &str, limit: usize) -> Vec<&Note> {
        let q = query.trim().to_lowercase();
        if q.is_empty() {
            let mut all: Vec<&Note> = self.notes.values().collect();
            all.sort_by_key(|n| std::cmp::Reverse(n.mtime));
            all.truncate(limit);
            return all;
        }
        let score = |n: &Note| -> Option<u8> {
            let (name, rel) = (n.name.to_lowercase(), n.rel.to_lowercase());
            if name == q {
                Some(0)
            } else if name.starts_with(&q) {
                Some(1)
            } else if name.contains(&q) {
                Some(2)
            } else if rel.contains(&q) {
                Some(3)
            } else {
                let mut it = name.chars();
                q.chars().all(|c| it.any(|x| x == c)).then_some(4)
            }
        };
        let mut scored: Vec<(u8, &Note)> = self.notes.values().filter_map(|n| score(n).map(|s| (s, n))).collect();
        scored.sort_by_key(|(s, n)| (*s, n.name.chars().count()));
        scored.into_iter().take(limit).map(|(_, n)| n).collect()
    }

    /// Lines with every word of the query: (rel, line number, the line). Newest notes first.
    pub fn search(&self, query: &str, limit: usize) -> Vec<(String, usize, String)> {
        let words: Vec<String> = query.split_whitespace().map(|w| w.to_lowercase()).collect();
        if words.is_empty() {
            return vec![];
        }
        let mut notes: Vec<&Note> = self.notes.values().collect();
        notes.sort_by_key(|n| std::cmp::Reverse(n.mtime));
        let mut hits = vec![];
        for note in notes {
            let Ok(text) = std::fs::read_to_string(&note.path) else { continue };
            for (n, line) in text.lines().enumerate() {
                let low = line.to_lowercase();
                if words.iter().all(|w| low.contains(w)) {
                    hits.push((note.rel.clone(), n, line.trim().to_string()));
                    if hits.len() >= limit {
                        return hits;
                    }
                }
            }
        }
        hits
    }

    // ── files ────────────────────────────────────────────────────────────

    pub fn read(&self, rel: &str) -> String {
        std::fs::read_to_string(self.root.join(rel)).unwrap_or_default()
    }

    /// Write through a temporary file, so a crash never leaves half a note.
    pub fn write(&mut self, rel: &str, text: &str) -> Result<()> {
        let path = self.root.join(rel);
        let tmp = path.with_file_name(format!(
            ".{}.mabel-tmp",
            path.file_name().map(|f| f.to_string_lossy()).unwrap_or_default()
        ));
        std::fs::write(&tmp, text)?;
        std::fs::rename(&tmp, &path)?;
        self.refresh_note(rel);
        Ok(())
    }

    pub fn create(&mut self, rel: &str, text: &str) -> Result<String> {
        let rel = if rel.ends_with(".md") { rel.to_string() } else { format!("{rel}.md") };
        if rel.split('/').any(|p| p == ".." || p.is_empty()) {
            bail!("outside the vault");
        }
        let path = self.root.join(&rel);
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        if !path.exists() {
            std::fs::write(&path, text)?;
        }
        self.refresh_note(&rel);
        Ok(rel)
    }

    /// Rename in place and rewrite [[links]] to it across the vault (Obsidian's "always update
    /// links"). Returns (new rel, notes changed).
    pub fn rename(&mut self, rel: &str, new_name: &str) -> Result<(String, usize)> {
        let new_name = new_name.trim().trim_end_matches(".md");
        if new_name.is_empty() || new_name.contains('/') {
            bail!("a plain name, no folders");
        }
        let note = self.notes.get(rel).cloned().ok_or_else(|| anyhow!("no such note"))?;
        let new_path = note.path.with_file_name(format!("{new_name}.md"));
        if new_path.exists() {
            bail!("{new_name}.md already exists here");
        }
        let old = note.name.to_lowercase();
        let mut changed = 0;
        let others: Vec<Note> =
            self.notes.values().filter(|o| o.links.iter().any(|t| last_segment(t) == old)).cloned().collect();
        for other in others {
            let text = self.read(&other.rel);
            let new_text = WIKILINK.replace_all(&text, |m: &regex::Captures| {
                if self.resolve(&m[2], Some(&other)).as_deref() != Some(rel) {
                    return m[0].to_string();
                }
                let folder = m[2].rsplit_once('/').map_or(String::new(), |(f, _)| format!("{f}/"));
                let head = m.get(3).map_or("", |h| h.as_str());
                let alias = m.get(4).map_or(String::new(), |a| format!("|{}", a.as_str()));
                format!("{}[[{folder}{new_name}{head}{alias}]]", &m[1])
            });
            if new_text != text {
                let new_text = new_text.into_owned();
                self.write(&other.rel, &new_text)?;
                changed += 1;
            }
        }
        std::fs::rename(&note.path, &new_path)?;
        self.notes.remove(rel);
        let new_rel = new_path.strip_prefix(&self.root).unwrap_or(&new_path).to_string_lossy().into_owned();
        self.refresh_note(&new_rel);
        Ok((new_rel, changed))
    }

    // ── daily note ───────────────────────────────────────────────────────

    /// Today's daily note, created from the vault's template if missing (.obsidian/daily-notes.json).
    pub fn daily(&mut self, day: NaiveDate) -> Result<String> {
        let settings: serde_json::Value = std::fs::read_to_string(self.root.join(".obsidian/daily-notes.json"))
            .ok()
            .and_then(|t| serde_json::from_str(&t).ok())
            .unwrap_or_default();
        let fmt = settings["format"].as_str().filter(|f| !f.is_empty()).unwrap_or("YYYY-MM-DD");
        let folder = settings["folder"].as_str().unwrap_or("").trim_matches('/');
        let stem = moment(day.and_hms_opt(0, 0, 0).expect("midnight"), fmt);
        let rel = if folder.is_empty() { format!("{stem}.md") } else { format!("{folder}/{stem}.md") };
        if self.notes.contains_key(&rel) {
            return Ok(rel);
        }
        let template = settings["template"].as_str().filter(|t| !t.is_empty()).map(|t| {
            let t = if t.ends_with(".md") { t.to_string() } else { format!("{t}.md") };
            fill_template(&self.read(&t), day, &stem, fmt)
        });
        self.create(&rel, &template.unwrap_or_default())
    }
}

// ── Obsidian's (moment.js) date formats ─────────────────────────────────

fn ordinal(d: u32) -> String {
    let suffix = if (11..=13).contains(&(d % 100)) {
        "th"
    } else {
        match d % 10 {
            1 => "st",
            2 => "nd",
            3 => "rd",
            _ => "th",
        }
    };
    format!("{d}{suffix}")
}

/// How one moment.js token is written out.
type Token = fn(&NaiveDateTime) -> String;

/// Format with moment.js tokens (what Obsidian uses), e.g. "dddd, MMMM Do, YYYY".
pub fn moment(when: NaiveDateTime, fmt: &str) -> String {
    let tokens: [(&str, Token); 20] = [
        ("YYYY", |w| w.format("%Y").to_string()),
        ("YY", |w| w.format("%y").to_string()),
        ("MMMM", |w| w.format("%B").to_string()),
        ("MMM", |w| w.format("%b").to_string()),
        ("MM", |w| w.format("%m").to_string()),
        ("M", |w| w.month().to_string()),
        ("DDDD", |w| w.format("%j").to_string()),
        ("Do", |w| ordinal(w.day())),
        ("DD", |w| w.format("%d").to_string()),
        ("D", |w| w.day().to_string()),
        ("dddd", |w| w.format("%A").to_string()),
        ("ddd", |w| w.format("%a").to_string()),
        ("HH", |w| w.format("%H").to_string()),
        ("H", |w| w.hour().to_string()),
        ("hh", |w| w.format("%I").to_string()),
        ("mm", |w| w.format("%M").to_string()),
        ("ss", |w| w.format("%S").to_string()),
        ("A", |w| w.format("%p").to_string()),
        ("ww", |w| format!("{:02}", w.iso_week().week())),
        ("w", |w| w.iso_week().week().to_string()),
    ];
    let mut out = String::new();
    let mut rest = fmt;
    'outer: while !rest.is_empty() {
        if let Some(lit) = rest.strip_prefix('[') {
            match lit.find(']') {
                Some(end) => {
                    out.push_str(&lit[..end]);
                    rest = &lit[end + 1..];
                }
                None => {
                    out.push_str(rest);
                    rest = "";
                }
            }
            continue;
        }
        for (token, f) in &tokens {
            if let Some(after) = rest.strip_prefix(token) {
                out.push_str(&f(&when));
                rest = after;
                continue 'outer;
            }
        }
        let c = rest.chars().next().expect("not empty");
        out.push(c);
        rest = &rest[c.len_utf8()..];
    }
    out
}

static TEMPLATE_DATE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\{\{(date|time):([^}]+)\}\}").expect("regex"));

/// Obsidian's template variables: {{date}}, {{date:FMT}}, {{time}}, {{time:FMT}}, {{title}},
/// {{yesterday}}, {{tomorrow}}.
pub fn fill_template(text: &str, day: NaiveDate, title: &str, fmt: &str) -> String {
    let now = day.and_time(Local::now().time());
    let at = |d: NaiveDate| moment(d.and_hms_opt(0, 0, 0).expect("midnight"), fmt);
    let text = TEMPLATE_DATE.replace_all(text, |m: &regex::Captures| moment(now, &m[2]));
    text.replace("{{date}}", &at(day))
        .replace("{{time}}", &now.format("%H:%M").to_string())
        .replace("{{title}}", title)
        .replace("{{yesterday}}", &at(day - Duration::days(1)))
        .replace("{{tomorrow}}", &at(day + Duration::days(1)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scanning_links_tags_headings_frontmatter() {
        let text = "---\ntags: [a, b]\ntitle: \"Hej\"\nalias:\n  - x\n  - y\n---\n# Top\nsee [[Other Note#Part|alias]] and ![[pic.png]]\n#idea and a#not and https://x.y/#nope\n```\n[[in code]] #nocode\n```\n## Sub ##";
        let (fm, links, tags, heads) = scan_text(text);
        assert_eq!(fm[0], ("tags".into(), vec!["a".into(), "b".into()]));
        assert_eq!(fm[1], ("title".into(), vec!["Hej".into()]));
        assert_eq!(fm[2].1, vec!["x".to_string(), "y".to_string()]);
        assert_eq!(links, vec!["Other Note", "pic.png"]);
        assert!(tags.contains(&"idea".to_string()) && tags.contains(&"a".to_string()));
        assert!(!tags.iter().any(|t| t == "not" || t == "nope" || t == "nocode"));
        assert_eq!(heads, vec![(1, "Top".into(), 7), (2, "Sub".into(), 13)], "file line numbers");
    }

    #[test]
    fn moment_formats() {
        let d = NaiveDate::from_ymd_opt(2026, 11, 19).unwrap().and_hms_opt(14, 5, 0).unwrap();
        assert_eq!(moment(d, "YYYY-MM-DD"), "2026-11-19");
        assert_eq!(moment(d, "dddd, MMMM Do, YYYY"), "Thursday, November 19th, 2026");
        assert_eq!(moment(d, "[Week] ww HH:mm"), "Week 47 14:05");
        let t = fill_template("# {{title}}\n{{date:ddd D MMM}} ← {{yesterday}}", d.date(), "2026-11-19", "YYYY-MM-DD");
        assert_eq!(t, "# 2026-11-19\nThu 19 Nov ← 2026-11-18");
    }

    #[test]
    fn resolve_rename_and_backlinks() {
        let dir = std::env::temp_dir().join(format!("mabel-vault-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("a")).unwrap();
        std::fs::write(dir.join("a/Target.md"), "# T").unwrap();
        std::fs::write(dir.join("Source.md"), "link [[Target]] and [[a/Target#H|alias]] and [[Else]]").unwrap();
        let mut v = Vault::new(dir.clone()).scan();
        assert_eq!(v.resolve("Target", None).as_deref(), Some("a/Target.md"));
        let target = v.notes["a/Target.md"].clone();
        assert_eq!(v.backlinks(&target).len(), 1);
        let (new_rel, changed) = v.rename("a/Target.md", "Goal").unwrap();
        assert_eq!((new_rel.as_str(), changed), ("a/Goal.md", 1));
        assert_eq!(v.read("Source.md"), "link [[Goal]] and [[a/Goal#H|alias]] and [[Else]]");
        assert_eq!(v.find_notes("gol", 5)[0].name, "Goal");
        assert_eq!(v.search("goal alias", 5).len(), 1);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
