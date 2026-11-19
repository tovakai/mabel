//! CH-02 LIFE: what Mabel is doing and has been up to, from /api/tui/life. Refreshes every
//! minute (r: now). v / p open her last dream / paper note in VAULT.
//!
//! PC: SYSTEMS | LAST DREAM | JOURNAL over LISTS. The deck: a 2×2 grid.

use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use serde_json::Value;

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::timefmt::hm;
use crate::os::ui::{self, bold};
use crate::widgets::picture::Picture;
use crate::widgets::wrap;

const ID: &str = "life";
const SYSTEMS: usize = 0;
const DREAM: usize = 1;
const JOURNAL: usize = 2;
const LISTS: usize = 3;
const REFRESH_EVERY: Duration = Duration::from_secs(60);

/// Background work reporting back.
enum Got {
    Life(Result<Value, String>),
    Dream { at: String, bytes: Option<Vec<u8>> },
}

/// A job's name as LIFE shows it (her internal names → words).
fn job_name(job: &str) -> &str {
    match job {
        "letters" => "tova letters",
        "revisit" => "look back",
        "music" => "compose",
        "paper" => "paper note",
        "morning" => "morning page",
        "housekeeping" => "gremlin round",
        other => other,
    }
}

/// A text field ("" if missing).
fn s<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}

/// The first `max` characters, with … when cut.
fn clip(text: &str, max: usize) -> String {
    if text.chars().count() > max + 1 {
        format!("{}…", text.chars().take(max).collect::<String>())
    } else {
        text.into()
    }
}

/// What she's doing now, and each job's last run and what's holding it back.
fn status_lines(data: &Value, pal: &Palette) -> Vec<Line<'static>> {
    let doing: Vec<&str> = data["doing"].as_array().into_iter().flatten().filter_map(|d| d.as_str()).collect();
    let mut out = vec![Line::from(vec![
        Span::styled("NOW  ", bold(pal.beige)),
        if doing.is_empty() {
            Span::styled("idle, waiting for you to step away", Style::new().fg(pal.amber_dim))
        } else {
            Span::styled(doing.join(", "), bold(pal.teal))
        },
    ])];
    out.push(Line::raw(""));
    for job in data["jobs"].as_array().into_iter().flatten() {
        let ran = job["last_ran"].as_str().filter(|r| !r.is_empty());
        let waiting = job["waiting_on"].as_array().and_then(|w| w.first()).and_then(|w| w.as_str()).unwrap_or("");
        let reason = match s(job, "outcome") {
            "" => waiting,
            o => o,
        };
        out.push(Line::from(vec![
            Span::styled(
                if ran.is_some() { "● " } else { "○ " },
                Style::new().fg(if ran.is_some() { pal.teal } else { pal.amber_faint }),
            ),
            Span::styled(format!("{:<14}", job_name(s(job, "job"))), Style::new().fg(pal.amber)),
            Span::styled(
                format!("{}  ", hm(ran)),
                Style::new().fg(if ran.is_some() { pal.amber_hi } else { pal.amber_faint }),
            ),
            Span::styled(clip(reason, 26), Style::new().fg(pal.amber_dim)),
        ]));
    }
    out
}

/// Her latest journal entries (dream, day account…).
fn journal_lines(data: &Value, pal: &Palette) -> Vec<Line<'static>> {
    let mut out = vec![];
    let head = |out: &mut Vec<Line<'static>>, label: &str, when: Option<&str>| {
        out.push(Line::raw(""));
        let mut spans = vec![Span::styled(label.to_string(), bold(pal.beige))];
        if let Some(w) = when {
            spans.push(Span::styled(format!("  ·  {}", hm(Some(w))), Style::new().fg(pal.beige_dark)));
        }
        out.push(Line::from(spans));
    };
    let body = |out: &mut Vec<Line<'static>>, text: &str, max: usize| {
        for para in text.chars().take(max).collect::<String>().split('\n') {
            out.push(Line::styled(para.to_string(), Style::new().fg(pal.amber)));
        }
    };
    let song = &data["song"];
    if song.is_object() {
        head(&mut out, "♪ LAST COMPOSITION", song["at"].as_str());
        out.push(Line::styled(s(song, "title").to_string(), bold(pal.amber_hi)));
    }
    let paper = &data["paper"];
    if paper.is_object() {
        head(&mut out, "✎ ON PAPER", paper["at"].as_str());
        body(&mut out, s(paper, "text"), 400);
    }
    let news = &data["news"];
    if !s(news, "message").is_empty() {
        head(&mut out, &format!("◈ NEWS  {}", s(news, "day")), None);
        body(&mut out, s(news, "message"), 420);
    }
    out
}

/// His lists: to do, groceries.
fn list_lines(data: &Value, pal: &Palette) -> Vec<Line<'static>> {
    let mut out = vec![Line::styled("GROCERIES", bold(pal.beige))];
    let items = data["groceries"].as_array().cloned().unwrap_or_default();
    for item in &items {
        let bought = item["bought"].as_bool().unwrap_or(false);
        let amount = match s(item, "amount") {
            "" => String::new(),
            a => format!(" ({a})"),
        };
        let (mark, colour) = if bought { ("☑", pal.beige_dark) } else { ("☐", pal.amber_hi) };
        out.push(Line::styled(format!(" {mark} {}{amount}", s(item, "item")), Style::new().fg(colour)));
    }
    if items.is_empty() {
        out.push(Line::styled(" nothing on the list", Style::new().fg(pal.amber_dim)));
    }
    out.push(Line::raw(""));
    out.push(Line::styled("GREMLIN · CHECKED OUT", bold(pal.beige)));
    let gone = data["gremlin_out"].as_array().cloned().unwrap_or_default();
    for item in &gone {
        let days = item["days_out"].as_u64().unwrap_or(0);
        out.push(Line::from(vec![
            Span::styled(
                format!(" ▪ {}", s(item, "item").chars().take(30).collect::<String>()),
                Style::new().fg(pal.amber),
            ),
            Span::styled(format!("  {days}d"), Style::new().fg(if days >= 3 { pal.red } else { pal.amber_dim })),
        ]));
    }
    if gone.is_empty() {
        out.push(Line::styled(" everything is home", Style::new().fg(pal.amber_dim)));
    }
    out
}

/// CH-02 LIFE: her day at a glance.
pub struct Life {
    data: Option<Value>,
    error: Option<String>,
    picture: Option<Picture>,
    picture_at: Option<String>,
    /// Lines scrolled away, per section.
    scroll: [usize; 4],
    /// Lines that fit, per section (for paging and clamping).
    page: [usize; 4],
    last_refresh: Option<Instant>,
}

impl Life {
    /// Empty until the first fetch.
    pub fn new() -> Self {
        Life {
            data: None,
            error: None,
            picture: None,
            picture_at: None,
            scroll: [0; 4],
            page: [1; 4],
            last_refresh: None,
        }
    }

    /// Fetch LIFE (and the dream picture) again.
    fn refresh(&mut self, ctx: &Ctx) {
        self.last_refresh = Some(Instant::now());
        ctx.spawn(ID, |api| async move { Got::Life(api.life().await.map_err(|e| e.to_string())) });
    }

    /// v / p: open her note for this part (the dream, the page) in VAULT, in her journal.
    fn open_in_vault(&self, part: &str, ctx: &mut Ctx) {
        let note = self.data.as_ref().map(|d| s(&d[part], "note")).unwrap_or("");
        if note.is_empty() {
            ctx.toast("LIFE", "Nothing to open yet");
            return;
        }
        // Her notes live in her journal; the snapshot names them from the journal's parent.
        let rel = note.strip_prefix("journal/").unwrap_or(note).to_string();
        let open = crate::os::channel::OpenNote { vault: "Mabel's journal".into(), rel, line: None };
        ctx.requests.push(crate::os::channel::Request::Deliver { to: "vault", msg: Box::new(open) });
    }

    /// Keys that work both in nav mode and inside a panel.
    fn common_key(&mut self, key: &KeyEvent, ctx: &mut Ctx) -> Outcome {
        match keys::ch(key) {
            Some('r') => self.refresh(ctx),
            Some('v') => self.open_in_vault("dream", ctx),
            Some('p') => self.open_in_vault("paper", ctx),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    /// A scrollable text panel; returns nothing, keeps the scroll in range.
    fn text_panel(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        view: &View,
        section: usize,
        title: &str,
        lines: Vec<Line<'static>>,
    ) {
        let block = ui::panel(view.pal, title, view.panel_state(section));
        let inner = block.inner(area);
        frame.render_widget(block, area);
        let wrapped = wrap::wrap_lines(&lines, inner.width.max(1) as usize);
        self.page[section] = inner.height as usize;
        self.scroll[section] = self.scroll[section].min(wrapped.len().saturating_sub(inner.height as usize));
        frame.render_widget(Paragraph::new(wrapped).scroll((self.scroll[section] as u16, 0)), inner);
    }

    /// Her last dream: its picture and text.
    fn dream_panel(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let dream = self.data.as_ref().map(|d| d["dream"].clone()).unwrap_or(Value::Null);
        let block = ui::panel(pal, "LAST DREAM", view.panel_state(DREAM)).title_bottom(
            Line::styled(format!(" {} ", hm(dream["at"].as_str())), Style::new().fg(pal.amber_dim)).right_aligned(),
        );
        let inner = block.inner(area);
        frame.render_widget(block, area);

        let pic_max = if view.narrow { 10 } else { 14 };
        let pic_rows = match &self.picture {
            Some(p) => p.fit(inner.width, pic_max).1 as usize,
            None => 0,
        };
        let mut lines = vec![];
        if !s(&dream, "title").is_empty() {
            lines.push(Line::styled(s(&dream, "title").to_string(), bold(pal.amber_hi)));
            lines.push(Line::raw(""));
        }
        let text: String = s(&dream, "text").chars().take(1200).collect();
        for para in text.split('\n') {
            lines.push(Line::styled(para.to_string(), Style::new().fg(pal.amber)));
        }
        let wrapped = wrap::wrap_lines(&lines, inner.width.max(1) as usize);
        let total = pic_rows + usize::from(pic_rows > 0) + wrapped.len();
        self.page[DREAM] = inner.height as usize;
        self.scroll[DREAM] = self.scroll[DREAM].min(total.saturating_sub(inner.height as usize));
        let skip = self.scroll[DREAM];

        // The picture scrolls away with the text.
        let mut y = inner.y;
        if let Some(p) = &mut self.picture {
            let drawn = p.render(inner, frame.buffer_mut(), pic_max, skip as u16);
            y += drawn;
            if skip <= pic_rows {
                y += 1; // the gap under it
            }
        }
        let text_skip = skip.saturating_sub(pic_rows + usize::from(pic_rows > 0));
        let rest = Rect { y, height: inner.bottom().saturating_sub(y), ..inner };
        frame.render_widget(Paragraph::new(wrapped).scroll((text_skip as u16, 0)), rest);
    }

    /// Her journal entries.
    fn journal_panel(&mut self, frame: &mut Frame, area: Rect, view: &View) {
        let pal = view.pal;
        let data = self.data.clone().unwrap_or(Value::Null);
        let book = &data["book"];
        let mut lines = vec![
            Line::styled("▤ READING", bold(pal.beige)),
            Line::styled(
                match s(book, "title") {
                    "" => "nothing right now",
                    t => t,
                }
                .to_string(),
                bold(pal.amber_hi),
            ),
            Line::styled(s(book, "author").to_string(), Style::new().fg(pal.amber_dim)),
        ];
        let progress = book["progress"].as_f64().unwrap_or(0.0).clamp(0.0, 1.0);
        let bar_w = (area.width as usize).saturating_sub(10).clamp(4, 30);
        let filled = (progress * bar_w as f64).round() as usize;
        let colour = if progress >= 1.0 { pal.teal } else { pal.amber };
        lines.push(Line::from(vec![
            Span::styled("━".repeat(filled), Style::new().fg(colour)),
            Span::styled("━".repeat(bar_w - filled), Style::new().fg(pal.amber_faint)),
            Span::styled(format!(" {:>3.0}%", progress * 100.0), Style::new().fg(pal.amber_dim)),
        ]));
        lines.extend(journal_lines(&data, pal));
        self.text_panel(frame, area, view, JOURNAL, "JOURNAL", lines);
    }
}

impl Channel for Life {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "LIFE"
    }

    fn start(&mut self, ctx: &mut Ctx) {
        self.refresh(ctx);
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let rects = if view.narrow {
            let [top, bottom] = Layout::vertical([Constraint::Fill(1), Constraint::Fill(1)]).areas(area);
            let [a, b] = Layout::horizontal([Constraint::Fill(1), Constraint::Fill(1)]).areas(top);
            let [c, d] = Layout::horizontal([Constraint::Fill(1), Constraint::Fill(1)]).areas(bottom);
            [a, b, c, d]
        } else {
            let [a, b, right] = Layout::horizontal([Constraint::Fill(1); 3]).areas(area);
            let [c, d] = Layout::vertical([Constraint::Fill(1), Constraint::Fill(1)]).areas(right);
            [a, b, c, d]
        };
        let pal = *view.pal;
        let status = match (&self.error, &self.data) {
            (Some(e), _) => vec![Line::styled(format!("✖ can't reach Mabel: {e}"), Style::new().fg(pal.red))],
            (None, Some(d)) => status_lines(d, &pal),
            (None, None) => vec![Line::styled("loading…", Style::new().fg(pal.amber_dim))],
        };
        self.text_panel(frame, rects[SYSTEMS], view, SYSTEMS, "SYSTEMS", status);
        self.dream_panel(frame, rects[DREAM], view);
        self.journal_panel(frame, rects[JOURNAL], view);
        let lists = self.data.as_ref().map(|d| list_lines(d, &pal)).unwrap_or_default();
        self.text_panel(frame, rects[LISTS], view, LISTS, "LISTS", lists);
        rects.to_vec()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if self.common_key(&key, ctx) == Outcome::Used {
            return Outcome::Used;
        }
        let page = self.page[section].saturating_sub(1).max(1);
        let scroll = &mut self.scroll[section];
        match (keys::ch(&key), key.code) {
            (_, KeyCode::Down) => *scroll += 1,
            (_, KeyCode::Up) => *scroll = scroll.saturating_sub(1),
            (_, KeyCode::PageDown) | (Some(' '), _) => *scroll += page,
            (_, KeyCode::PageUp) => *scroll = scroll.saturating_sub(page),
            (Some('g'), _) | (_, KeyCode::Home) => *scroll = 0,
            (Some('G'), _) | (_, KeyCode::End) => *scroll = usize::MAX / 2, // clamped when drawn
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        self.common_key(&key, ctx)
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, _ctx: &mut Ctx) -> Outcome {
        let Some(i) = section else { return Outcome::Ignored };
        match event.kind {
            MouseEventKind::ScrollDown => self.scroll[i] += 3,
            MouseEventKind::ScrollUp => self.scroll[i] = self.scroll[i].saturating_sub(3),
            _ => return Outcome::Ignored,
        }
        Outcome::Used
    }

    fn tick(&mut self, now: Instant, _visible: bool, ctx: &mut Ctx) {
        if self.last_refresh.is_some_and(|t| now.duration_since(t) >= REFRESH_EVERY) {
            self.refresh(ctx);
        }
    }

    fn on_msg(&mut self, _id: &str, msg: Box<dyn std::any::Any + Send>, ctx: &mut Ctx) {
        let Ok(got) = msg.downcast::<Got>() else { return };
        match *got {
            Got::Life(Ok(data)) => {
                self.error = None;
                let dream = &data["dream"];
                let at = s(dream, "at").to_string();
                if dream["has_image"].as_bool().unwrap_or(false) && self.picture_at.as_deref() != Some(at.as_str()) {
                    ctx.spawn(ID, move |api| async move { Got::Dream { bytes: api.dream_image().await, at } });
                }
                self.data = Some(data);
            }
            Got::Life(Err(e)) => self.error = Some(e),
            Got::Dream { at, bytes } => {
                if let Some(p) = bytes.as_deref().and_then(Picture::decode) {
                    self.picture = Some(p);
                    self.picture_at = Some(at);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::widgets::wrap::width;
    use serde_json::json;

    fn text(lines: &[Line]) -> Vec<String> {
        lines.iter().map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect()).collect()
    }

    #[test]
    fn status_and_lists_read_the_snapshot() {
        let pal = Palette::base();
        let data = json!({"doing": [], "jobs": [{"job": "music", "last_ran": null, "waiting_on": ["you are at the keyboard right now"]}],
                          "groceries": [{"item": "milk", "amount": "2", "bought": false}], "gremlin_out": []});
        let st = text(&status_lines(&data, &pal));
        assert!(st[0].contains("idle"));
        assert_eq!(st[2], "○ compose       --:--  you are at the keyboard ri…");
        let li = text(&list_lines(&data, &pal));
        assert_eq!(li[1], " ☐ milk (2)");
        assert!(li.iter().any(|l| l.contains("everything is home")));
        assert_eq!(width("☐"), 1);
    }
}
