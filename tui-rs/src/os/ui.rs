//! The look: panels, and the casing around the channels (label strip, tabs, status strip,
//! footer), the help card and toasts. Mirrors tui/mabel_tui/theme.tcss.

use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, BorderType, Borders, Clear, Paragraph, Wrap};

use crate::os::help;
use crate::os::palette::Palette;

pub const LOGO: &str = "▙▟ MABEL-OS 2.6";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PanelState {
    Idle,
    /// Nav mode's lit section: heavy bright border on a faint amber ground.
    Lit,
    /// Keys go here: a bright rounded border.
    Inside,
}

pub fn bold(colour: ratatui::style::Color) -> Style {
    Style::new().fg(colour).add_modifier(Modifier::BOLD)
}

/// A `.panel`: rounded amber border, beige title, panel background.
pub fn panel<'a>(pal: &Palette, title: &str, state: PanelState) -> Block<'a> {
    let (border, colour, bg) = match state {
        PanelState::Idle => (BorderType::Rounded, pal.amber_dim, pal.panel),
        PanelState::Inside => (BorderType::Rounded, pal.amber, pal.panel),
        PanelState::Lit => (BorderType::Thick, pal.amber_hi, pal.amber_faint),
    };
    Block::new()
        .borders(Borders::ALL)
        .border_type(border)
        .border_style(Style::new().fg(colour))
        .title(if title.is_empty() { Span::raw("") } else { Span::styled(format!(" {title} "), bold(pal.beige)) })
        .style(Style::new().bg(bg).fg(pal.amber))
        .padding(ratatui::widgets::Padding::horizontal(1))
}

/// The casing's label strip: logo, link, clock.
pub fn casing(
    frame: &mut Frame,
    area: Rect,
    pal: &Palette,
    host: &str,
    link_ok: Option<bool>,
    badge: Option<&str>,
    clock: &str,
) {
    let strip = Style::new().bg(pal.beige).fg(pal.casing_text);
    let mut left = vec![
        Span::styled(LOGO, bold(pal.casing_text)),
        Span::raw("  ░  TERMINAL UNIT 01  ░  LINK "),
        Span::styled(host.to_string(), bold(pal.casing_text)),
    ];
    match link_ok {
        Some(false) => left.push(Span::styled("  ✖ NO LINK", bold(pal.rust))),
        None => left.push(Span::raw("  …")),
        Some(true) => {}
    }
    if let Some(badge) = badge {
        left.push(Span::styled(format!("  {badge}"), bold(pal.rust)));
    }
    let inner = Rect { x: area.x + 1, width: area.width.saturating_sub(2), ..area };
    frame.render_widget(Block::new().style(strip), area);
    frame.render_widget(Paragraph::new(Line::from(left)).style(strip), inner);
    frame.render_widget(
        Paragraph::new(Span::styled(clock.to_string(), bold(pal.casing_text))).alignment(Alignment::Right).style(strip),
        inner,
    );
}

/// Tab labels as drawn, and where each one sits (for clicks). Too narrow for "CH-01 COMMS" on
/// every tab (the deck), the inactive ones drop their "CH-0n".
pub fn tab_places(area: Rect, labels: &[&str], active: usize) -> Vec<(String, Rect)> {
    let full: Vec<String> = labels.iter().enumerate().map(|(i, l)| format!("CH-{:02} {l}", i + 1)).collect();
    let width = |names: &[String]| names.iter().map(|n| n.chars().count() as u16 + 4).sum::<u16>();
    let names = if width(&full) <= area.width {
        full
    } else {
        full.iter().enumerate().map(|(i, n)| if i == active { n.clone() } else { labels[i].to_string() }).collect()
    };
    let mut x = area.x;
    names
        .into_iter()
        .map(|n| {
            let w = n.chars().count() as u16 + 4;
            let r = Rect::new(x, area.y, w.min(area.right().saturating_sub(x)), 1);
            x = x.saturating_add(w);
            (n, r)
        })
        .collect()
}

/// The channel tabs and the line under them.
pub fn tabs(frame: &mut Frame, area: Rect, pal: &Palette, labels: &[&str], active: usize) {
    let [row, under] = Layout::vertical([Constraint::Length(1), Constraint::Length(1)]).areas(area);
    frame.render_widget(Block::new().style(Style::new().bg(pal.bg)), area);
    let mut active_place = Rect::default();
    for (i, (name, place)) in tab_places(row, labels, active).into_iter().enumerate() {
        let style = if i == active { bold(pal.bg).bg(pal.amber) } else { bold(pal.amber_dim) };
        if i == active {
            active_place = place;
        }
        frame.render_widget(Paragraph::new(format!("  {name}  ")).style(style), place);
    }
    frame
        .render_widget(Paragraph::new("━".repeat(under.width as usize)).style(Style::new().fg(pal.amber_faint)), under);
    let bar = Rect { y: under.y, height: 1, ..active_place }.intersection(under);
    frame.render_widget(Paragraph::new("━".repeat(bar.width as usize)).style(Style::new().fg(pal.amber)), bar);
}

/// The status strip: what plays, and a status line in rust.
pub fn statusline(frame: &mut Frame, area: Rect, pal: &Palette, playing: Option<(&str, bool)>, status: &str) {
    let base = Style::new().bg(pal.beige_dark).fg(pal.casing_text);
    let mut spans = match playing {
        Some((song, paused)) => vec![
            Span::styled(if paused { "❚❚ " } else { "▶ " }, bold(pal.casing_text)),
            Span::raw(song.chars().take(60).collect::<String>()),
        ],
        None => vec![Span::raw("◌ deck empty")],
    };
    if !status.is_empty() {
        spans.push(Span::styled(format!("   {status}"), bold(pal.rust)));
    }
    let inner = Rect { x: area.x + 1, width: area.width.saturating_sub(2), ..area };
    frame.render_widget(Block::new().style(base), area);
    frame.render_widget(Paragraph::new(Line::from(spans)).style(base), inner);
}

/// The footer: the keys worth showing everywhere.
pub fn footer(frame: &mut Frame, area: Rect, pal: &Palette, keys: &[(&str, &str)]) {
    let mut spans = vec![];
    for (key, what) in keys {
        spans.push(Span::styled(format!(" {key} "), bold(pal.amber_hi).bg(pal.amber_faint)));
        spans.push(Span::styled(format!(" {what} "), Style::new().fg(pal.amber_dim)));
    }
    frame.render_widget(Paragraph::new(Line::from(spans)).style(Style::new().bg(pal.panel)), area);
}

/// The lines of the help card: the channel's (or app's) keys, then everywhere's.
pub fn help_text(pal: &Palette, id: &str) -> Text<'static> {
    let mut lines = vec![];
    let own = help::channel(id);
    let mut part = |name: &str, keys: help::Keys| {
        lines.push(Line::styled(name.to_uppercase(), bold(pal.beige)));
        for (k, what) in keys {
            lines.push(Line::from(vec![
                Span::styled(format!("  {k:<22}"), bold(pal.amber_hi)),
                Span::styled(what.to_string(), Style::new().fg(pal.amber)),
            ]));
        }
        lines.push(Line::raw(""));
    };
    let heading = if id.starts_with("sys-") { "sys" } else { id };
    if own.is_empty() {
        part(heading, &[("(not ported yet)", "the Python MABEL-OS has this channel's keys")]);
    } else {
        part(heading, own);
    }
    part("everywhere", help::EVERYWHERE);
    Text::from(lines)
}

/// The help card over everything, scrolled by `scroll` lines.
pub fn help_card(frame: &mut Frame, area: Rect, pal: &Palette, id: &str, scroll: &mut u16) {
    let text = help_text(pal, id);
    let width = 92.min(area.width.saturating_sub(2));
    // Long descriptions wrap on a narrow screen: count the lines as drawn.
    let inner = width.saturating_sub(6).max(1) as usize;
    let drawn: u16 = text.lines.iter().map(|l| l.width().div_ceil(inner).max(1) as u16).sum();
    let height = (drawn + 4).min(area.height.saturating_sub(2));
    let card = Rect::new(area.x + (area.width - width) / 2, area.y + (area.height - height) / 2, width, height);
    let max = (drawn + 4).saturating_sub(height);
    *scroll = (*scroll).min(max);
    let block = Block::bordered()
        .border_type(BorderType::Thick)
        .border_style(Style::new().fg(pal.amber))
        .title(Span::styled(" KEYS ", bold(pal.beige)))
        .title_bottom(
            Line::styled(
                if max > 0 { " j k scroll · esc closes " } else { " esc closes " },
                Style::new().fg(pal.amber_dim),
            )
            .right_aligned(),
        )
        .style(Style::new().bg(pal.panel))
        .padding(ratatui::widgets::Padding::new(2, 2, 1, 1));
    frame.render_widget(Clear, card);
    frame.render_widget(Paragraph::new(text).wrap(Wrap { trim: false }).block(block).scroll((*scroll, 0)), card);
}

pub struct Toast {
    pub title: String,
    pub text: String,
    pub error: bool,
    pub until: std::time::Instant,
}

/// Toasts stack up from the bottom right, over the channel.
pub fn toasts(frame: &mut Frame, area: Rect, pal: &Palette, toasts: &[Toast]) {
    let width = 48.min(area.width);
    let mut bottom = area.bottom();
    for toast in toasts.iter().rev() {
        let body = Paragraph::new(toast.text.clone()).wrap(Wrap { trim: true });
        let inner = width.saturating_sub(4).max(1) as usize;
        let lines = toast.text.chars().count().div_ceil(inner).max(1) + 1; // +1: word wrap wastes some
        let height = (lines as u16 + 2).min(6);
        if bottom < area.y + height {
            break;
        }
        bottom -= height;
        let r = Rect::new(area.right() - width, bottom, width, height);
        let edge = if toast.error { pal.red } else { pal.amber };
        let block = Block::bordered()
            .border_type(BorderType::Thick)
            .border_style(Style::new().fg(edge))
            .title(Span::styled(format!(" {} ", toast.title), bold(pal.beige)))
            .style(Style::new().bg(pal.panel).fg(pal.amber_hi))
            .padding(ratatui::widgets::Padding::horizontal(1));
        frame.render_widget(Clear, r);
        frame.render_widget(body.block(block), r);
    }
}
