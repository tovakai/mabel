//! A one-line text input: the COMMS prompt, search fields, dialogs.
//!
//! Keys: characters type, ← → move, home/end (ctrl+a / ctrl+e), backspace/delete, ctrl+w drops
//! the word before the cursor, ctrl+u everything before it, ctrl+k everything after. Enter
//! submits; esc and anything else is left to the caller.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::Frame;
use ratatui::layout::{Position, Rect};
use ratatui::style::Style;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use unicode_width::UnicodeWidthChar;

use crate::os::keys;

#[derive(Debug, PartialEq, Eq)]
pub enum InputResult {
    Used,
    Submit,
    Ignored,
}

#[derive(Debug, Default, Clone)]
pub struct Input {
    pub value: String,
    /// Cursor position in characters.
    cursor: usize,
    pub placeholder: String,
    /// First character shown, when the text is wider than the field.
    offset: usize,
    /// Show • for each character (passwords).
    pub mask: bool,
}

impl Input {
    pub fn new(placeholder: &str) -> Self {
        Input { placeholder: placeholder.into(), ..Default::default() }
    }

    pub fn clear(&mut self) {
        self.value.clear();
        self.cursor = 0;
        self.offset = 0;
    }

    pub fn set(&mut self, value: &str) {
        self.value = value.into();
        self.cursor = self.value.chars().count();
    }

    fn byte(&self, char_index: usize) -> usize {
        self.value.char_indices().nth(char_index).map_or(self.value.len(), |(i, _)| i)
    }

    pub fn insert(&mut self, text: &str) {
        let at = self.byte(self.cursor);
        // A paste with newlines becomes one line.
        let clean: String = text.chars().map(|c| if c == '\n' || c == '\r' { ' ' } else { c }).collect();
        self.value.insert_str(at, &clean);
        self.cursor += clean.chars().count();
    }

    fn delete_range(&mut self, from: usize, to: usize) {
        let (a, b) = (self.byte(from), self.byte(to));
        self.value.replace_range(a..b, "");
        self.cursor = from;
    }

    pub fn key(&mut self, key: &KeyEvent) -> InputResult {
        let len = self.value.chars().count();
        if keys::ctrl(key, 'a') {
            self.cursor = 0;
        } else if keys::ctrl(key, 'e') {
            self.cursor = len;
        } else if keys::ctrl(key, 'u') {
            self.delete_range(0, self.cursor);
        } else if keys::ctrl(key, 'k') {
            self.delete_range(self.cursor, len);
        } else if keys::ctrl(key, 'w')
            || (key.code == KeyCode::Backspace && key.modifiers.contains(KeyModifiers::CONTROL))
        {
            let chars: Vec<char> = self.value.chars().collect();
            let mut start = self.cursor;
            while start > 0 && chars[start - 1].is_whitespace() {
                start -= 1;
            }
            while start > 0 && !chars[start - 1].is_whitespace() {
                start -= 1;
            }
            self.delete_range(start, self.cursor);
        } else if let Some(c) = keys::ch(key) {
            self.insert(&c.to_string());
        } else {
            match key.code {
                KeyCode::Enter => return InputResult::Submit,
                KeyCode::Left => self.cursor = self.cursor.saturating_sub(1),
                KeyCode::Right => self.cursor = (self.cursor + 1).min(len),
                KeyCode::Home => self.cursor = 0,
                KeyCode::End => self.cursor = len,
                KeyCode::Backspace if self.cursor > 0 => self.delete_range(self.cursor - 1, self.cursor),
                KeyCode::Backspace => {}
                KeyCode::Delete if self.cursor < len => self.delete_range(self.cursor, self.cursor + 1),
                KeyCode::Delete => {}
                _ => return InputResult::Ignored,
            }
        }
        InputResult::Used
    }

    /// Draw the text (or the placeholder) in `area`; with `focused`, place the terminal cursor.
    pub fn render(&mut self, frame: &mut Frame, area: Rect, text: Style, placeholder: Style, focused: bool) {
        if area.width == 0 {
            return;
        }
        if self.value.is_empty() {
            frame.render_widget(Paragraph::new(Span::styled(self.placeholder.clone(), placeholder)), area);
            if focused {
                frame.set_cursor_position(Position::new(area.x, area.y));
            }
            return;
        }
        let chars: Vec<char> =
            if self.mask { vec!['•'; self.value.chars().count()] } else { self.value.chars().collect() };
        let w = |range: &[char]| range.iter().map(|c| c.width().unwrap_or(0)).sum::<usize>();
        let room = area.width as usize - 1;
        // Keep the cursor in view: scroll left or right as needed.
        self.offset = self.offset.min(self.cursor);
        while w(&chars[self.offset..self.cursor]) > room {
            self.offset += 1;
        }
        let mut shown = String::new();
        let mut used = 0;
        for c in &chars[self.offset..] {
            let cw = c.width().unwrap_or(0);
            if used + cw > area.width as usize {
                break;
            }
            used += cw;
            shown.push(*c);
        }
        frame.render_widget(Paragraph::new(Line::styled(shown, text)), area);
        if focused {
            let x = area.x + w(&chars[self.offset..self.cursor]) as u16;
            frame.set_cursor_position(Position::new(x.min(area.right() - 1), area.y));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn press(input: &mut Input, code: KeyCode, mods: KeyModifiers) -> InputResult {
        input.key(&KeyEvent::new(code, mods))
    }

    #[test]
    fn typing_editing_and_submit() {
        let mut i = Input::new("talk");
        for c in "hej då".chars() {
            press(&mut i, KeyCode::Char(c), KeyModifiers::NONE);
        }
        press(&mut i, KeyCode::Left, KeyModifiers::NONE);
        press(&mut i, KeyCode::Backspace, KeyModifiers::NONE);
        assert_eq!(i.value, "hej å");
        press(&mut i, KeyCode::End, KeyModifiers::NONE);
        press(&mut i, KeyCode::Char('w'), KeyModifiers::CONTROL);
        assert_eq!(i.value, "hej ");
        assert_eq!(press(&mut i, KeyCode::Enter, KeyModifiers::NONE), InputResult::Submit);
        assert_eq!(press(&mut i, KeyCode::Esc, KeyModifiers::NONE), InputResult::Ignored);
    }
}
