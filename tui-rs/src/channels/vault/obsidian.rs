//! Obsidian's extras (properties, callouts, `[[links]]`, highlights, comments) turned into
//! plain Markdown, so the note view can show them with the shared Markdown renderer.

use std::sync::LazyLock;

use regex::Regex;

use super::*;

/// An Obsidian callout's first line: `> [!kind]± title`.
static CALLOUT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(>\s*)\[!(\w+)\]([+-]?)\s*(.*)$").expect("regex"));
/// `==highlighted==` text.
static HIGHLIGHT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"==([^=]+)==").expect("regex"));

/// The icon shown for a callout kind.
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

/// Obsidian's extras as plain Markdown: properties as a quote, callouts with an icon, `[[links]]`
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
