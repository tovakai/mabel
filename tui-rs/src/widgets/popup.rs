//! Small boxes over a channel: pick one of a list, or type a line. The channel owns the state
//! and routes keys here while one is open (Channel::captures_keys).

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Clear, List, ListItem, ListState, Padding};

use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::bold;
use crate::widgets::input::{Input, InputResult};

/// A centred box with a heavy amber border; returns the inside.
pub fn frame_box(frame: &mut Frame, area: Rect, pal: &Palette, title: &str, foot: &str, w: u16, h: u16) -> Rect {
    let w = w.min(area.width.saturating_sub(2));
    let h = h.min(area.height.saturating_sub(2));
    let r = Rect::new(area.x + (area.width - w) / 2, area.y + (area.height - h) / 2, w, h);
    let block = Block::bordered()
        .border_type(BorderType::Thick)
        .border_style(Style::new().fg(pal.amber))
        .title(Span::styled(format!(" {title} "), bold(pal.beige)))
        .title_bottom(Line::styled(format!(" {foot} "), Style::new().fg(pal.amber_dim)).right_aligned())
        .style(Style::new().bg(pal.panel).fg(pal.amber))
        .padding(Padding::horizontal(1));
    let inner = block.inner(r);
    frame.render_widget(Clear, r);
    frame.render_widget(block, r);
    inner
}

/// What a popup said after a key: still open, cancelled, or an answer.
pub enum Answer<T> {
    /// Still open.
    Open,
    Cancel,
    Chosen(T),
}

/// Pick one of a list: j/k or arrows, enter picks, esc cancels.
pub struct Pick {
    pub title: String,
    pub options: Vec<(String, String)>, // (id, label)
    state: ListState,
}

impl Pick {
    /// A list of (id, label) to pick from, the first one selected.
    pub fn new(title: &str, options: Vec<(String, String)>) -> Self {
        Pick { title: title.into(), options, state: ListState::default().with_selected(Some(0)) }
    }

    pub fn key(&mut self, key: &KeyEvent) -> Answer<String> {
        let n = self.options.len();
        let at = self.state.selected().unwrap_or(0);
        match (keys::ch(key), key.code) {
            (_, KeyCode::Esc) | (Some('q'), _) => return Answer::Cancel,
            (_, KeyCode::Enter) => {
                return self.options.get(at).map_or(Answer::Cancel, |(id, _)| Answer::Chosen(id.clone()));
            }
            (Some('j'), _) | (_, KeyCode::Down) if n > 0 => self.state.select(Some((at + 1).min(n - 1))),
            (Some('k'), _) | (_, KeyCode::Up) => self.state.select(Some(at.saturating_sub(1))),
            _ => {}
        }
        Answer::Open
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        let h = (self.options.len() as u16 + 2).min(area.height * 7 / 10).max(5);
        let inner = frame_box(frame, area, pal, &self.title, "enter picks · esc cancels", 60, h);
        let items: Vec<ListItem> = self.options.iter().map(|(_, label)| ListItem::new(label.clone())).collect();
        let list = List::new(items).highlight_style(Style::new().bg(pal.amber_faint).fg(pal.amber_hi));
        frame.render_stateful_widget(list, inner, &mut self.state);
    }
}

/// Type one line: enter answers, esc cancels.
pub struct Ask {
    pub title: String,
    pub input: Input,
}

impl Ask {
    /// A question with one line to answer.
    pub fn new(title: &str, label: &str) -> Self {
        Ask { title: title.into(), input: Input::new(label) }
    }

    pub fn key(&mut self, key: &KeyEvent) -> Answer<String> {
        if key.code == KeyCode::Esc {
            return Answer::Cancel;
        }
        match self.input.key(key) {
            InputResult::Submit => Answer::Chosen(self.input.value.trim().to_string()),
            _ => Answer::Open,
        }
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        let inner = frame_box(frame, area, pal, &self.title, "enter saves · esc cancels", 60, 3);
        self.input.render(frame, inner, Style::new().fg(pal.amber_hi), Style::new().fg(pal.amber_dim), true);
    }
}
