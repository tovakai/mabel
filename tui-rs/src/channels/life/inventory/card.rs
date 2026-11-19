//! INVENTORY's item card: an item's details, where it is, its history, and its photo.

use super::*;

impl Inventory {
    // ── the item card ────────────────────────────────────────────────────

    /// The item card's lines: its status and home address, stage, size, specs and notes,
    /// what's inside it, and what happened to it lately.
    pub(super) fn card_lines(&self, pal: &Palette) -> Vec<Line<'static>> {
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
}
