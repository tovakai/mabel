//! A small form in a box: a few labelled fields, keyboard-first.
//!
//! tab / ↓ next field · shift+tab / ↑ previous · enter next (saves on the last) · ctrl+s save ·
//! esc cancel. A choice field cycles with ← → or space. Multi-line text shows its line breaks
//! as ⏎ (type ⏎-free text; the breaks you had are kept).

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::Frame;
use ratatui::layout::Rect;
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;

use crate::keys;
use crate::palette::Palette;
use crate::ui::bold;
use crate::widgets::input::{Input, InputResult};
use crate::widgets::popup::{Answer, frame_box};

const BREAK: &str = " ⏎ ";

pub enum Kind {
    Line,
    Choice(Vec<String>),
    /// Multi-line text, edited on one line.
    Text,
}

pub struct Field {
    pub key: String,
    label: String,
    kind: Kind,
    input: Input,
    choice: usize,
}

impl Field {
    pub fn new(key: &str, label: &str, value: &str, kind: Kind) -> Self {
        let mut input = Input::default();
        let mut choice = 0;
        match &kind {
            Kind::Choice(options) => choice = options.iter().position(|o| o == value).unwrap_or(0),
            Kind::Text => input.set(&value.replace('\n', BREAK)),
            Kind::Line => input.set(value),
        }
        Field { key: key.into(), label: label.into(), kind, input, choice }
    }

    fn value(&self) -> String {
        match &self.kind {
            Kind::Choice(options) => options.get(self.choice).cloned().unwrap_or_default(),
            Kind::Text => self.input.value.replace(BREAK, "\n").replace('⏎', "\n"),
            Kind::Line => self.input.value.clone(),
        }
    }
}

pub struct Form {
    title: String,
    fields: Vec<Field>,
    at: usize,
    scroll: usize,
}

impl Form {
    pub fn new(title: &str, fields: Vec<Field>) -> Self {
        Form { title: title.into(), fields, at: 0, scroll: 0 }
    }

    fn done(&self) -> Answer<Vec<(String, String)>> {
        Answer::Chosen(self.fields.iter().map(|f| (f.key.clone(), f.value())).collect())
    }

    pub fn key(&mut self, key: &KeyEvent) -> Answer<Vec<(String, String)>> {
        let n = self.fields.len();
        if keys::ctrl(key, 's') {
            return self.done();
        }
        match key.code {
            KeyCode::Esc => return Answer::Cancel,
            KeyCode::Tab | KeyCode::Down => self.at = (self.at + 1) % n,
            KeyCode::BackTab | KeyCode::Up => self.at = (self.at + n - 1) % n,
            KeyCode::Enter => {
                if self.at + 1 == n {
                    return self.done();
                }
                self.at += 1;
            }
            _ => {
                let field = &mut self.fields[self.at];
                match &field.kind {
                    Kind::Choice(options) => {
                        let m = options.len().max(1);
                        match (keys::ch(key), key.code) {
                            (_, KeyCode::Left) => field.choice = (field.choice + m - 1) % m,
                            (Some(' '), _) | (_, KeyCode::Right) => field.choice = (field.choice + 1) % m,
                            _ => {}
                        }
                    }
                    _ => {
                        let _ = field.input.key(key) == InputResult::Used;
                    }
                }
            }
        }
        Answer::Open
    }

    pub fn paste(&mut self, text: &str) {
        if let Some(f) = self.fields.get_mut(self.at) {
            f.input.insert(&text.replace('\n', BREAK));
        }
    }

    pub fn render(&mut self, frame: &mut Frame, area: Rect, pal: &Palette) {
        let want = self.fields.len() as u16 * 2 + 2;
        let inner =
            frame_box(frame, area, pal, &self.title, "ctrl+s save · tab next · esc cancel", 72, want.min(area.height));
        let per = 2usize;
        let fits = (inner.height as usize / per).max(1);
        if self.at < self.scroll {
            self.scroll = self.at;
        } else if self.at >= self.scroll + fits {
            self.scroll = self.at + 1 - fits;
        }
        for (slot, (i, f)) in self.fields.iter_mut().enumerate().skip(self.scroll).take(fits).enumerate() {
            let y = inner.y + (slot * per) as u16;
            let on = i == self.at;
            let label = Rect { y, height: 1, ..inner };
            let value = Rect { y: y + 1, height: 1, ..inner };
            frame.render_widget(
                Paragraph::new(Line::styled(
                    f.label.clone(),
                    if on { bold(pal.beige) } else { Style::new().fg(pal.beige_dark) },
                )),
                label,
            );
            match &f.kind {
                Kind::Choice(options) => {
                    let current = options.get(f.choice).map_or("", |s| s.as_str());
                    let shown = if current.is_empty() { "—" } else { current };
                    let style = if on { Style::new().fg(pal.bg).bg(pal.amber) } else { Style::new().fg(pal.amber_hi) };
                    frame.render_widget(
                        Paragraph::new(Line::from(vec![
                            Span::styled(format!(" ‹ {shown} › "), style),
                            Span::styled("  ← → to change", Style::new().fg(pal.amber_faint)),
                        ])),
                        value,
                    );
                }
                _ => {
                    let bg = if on { pal.amber_faint } else { pal.panel };
                    frame.render_widget(Paragraph::new("").style(Style::new().bg(bg)), value);
                    f.input.render(
                        frame,
                        value,
                        Style::new().fg(pal.amber_hi).bg(bg),
                        Style::new().fg(pal.amber_dim).bg(bg),
                        on,
                    );
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;

    fn press(f: &mut Form, code: KeyCode) -> Answer<Vec<(String, String)>> {
        f.key(&KeyEvent::new(code, KeyModifiers::NONE))
    }

    #[test]
    fn fields_choices_and_breaks() {
        let mut f = Form::new(
            "EDIT",
            vec![
                Field::new("name", "Name", "Bin", Kind::Line),
                Field::new("stage", "Stage", "Placed", Kind::Choice(vec!["".into(), "Placed".into(), "Review".into()])),
                Field::new("notes", "Notes", "one\ntwo", Kind::Text),
            ],
        );
        press(&mut f, KeyCode::Char('!'));
        press(&mut f, KeyCode::Tab);
        press(&mut f, KeyCode::Right);
        press(&mut f, KeyCode::Tab);
        press(&mut f, KeyCode::Char('x'));
        let Answer::Chosen(values) = press(&mut f, KeyCode::Enter) else { panic!("saved on the last field") };
        assert_eq!(
            values,
            vec![
                ("name".into(), "Bin!".into()),
                ("stage".into(), "Review".into()),
                ("notes".into(), "one\ntwox".into())
            ]
        );
    }
}
