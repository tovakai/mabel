//! Vim-style section navigation, so MABEL-OS works on a keyboard-only cyberdeck.
//!
//! Nav mode: nothing is inside; one *section* (a panel) is lit. h j k l move to the nearest
//! section in that direction on screen; i, a or enter goes in. Inside a section its own keys
//! apply, h j k l act as the arrows wherever the section doesn't use the letter, and esc comes
//! back out. The mouse keeps working: clicking into a section simply means you're inside it.

use ratatui::layout::Rect;

/// Where the keyboard is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Focus {
    /// Nav mode; the lit section (None until something is lit).
    Nav(Option<usize>),
    /// Inside section N: its keys apply.
    Inside(usize),
}

/// h j k l as a direction (x, y).
pub fn dir(key: char) -> Option<(i32, i32)> {
    match key {
        'h' => Some((-1, 0)),
        'l' => Some((1, 0)),
        'k' => Some((0, -1)),
        'j' => Some((0, 1)),
        _ => None,
    }
}

/// The nearest section in direction `key` from `current` (by on-screen centres), as the Python
/// client's nav.neighbour: a section that overlaps on the other axis beats a diagonal one.
pub fn neighbour(current: usize, sections: &[Rect], key: char) -> Option<usize> {
    let (dx, dy) = dir(key)?;
    let cur = *sections.get(current)?;
    let centre = |r: Rect| (r.x as f64 + r.width as f64 / 2.0, r.y as f64 + r.height as f64 / 2.0);
    let (cx, cy) = centre(cur);
    let mut best: Option<(usize, f64)> = None;
    for (i, s) in sections.iter().enumerate() {
        if i == current || s.width == 0 || s.height == 0 {
            continue;
        }
        let (sx, sy) = centre(*s);
        let ahead = (sx - cx) * dx as f64 + (sy - cy) * dy as f64;
        if ahead <= 0.0 {
            continue;
        }
        let (overlap, side) = if dx != 0 {
            (s.bottom().min(cur.bottom()) as i32 - s.y.max(cur.y) as i32, (sy - cy).abs())
        } else {
            (s.right().min(cur.right()) as i32 - s.x.max(cur.x) as i32, (sx - cx).abs())
        };
        let score = ahead + side * 2.0 - if overlap > 0 { 50.0 } else { 0.0 };
        if best.is_none_or(|(_, b)| score < b) {
            best = Some((i, score));
        }
    }
    best.map(|(i, _)| i)
}

/// The section under a mouse click.
pub fn hit(sections: &[Rect], x: u16, y: u16) -> Option<usize> {
    sections.iter().position(|r| r.contains((x, y).into()))
}

#[cfg(test)]
mod tests {
    use super::*;

    // ┌list┐┌─ chat ─────┐
    // │    ││            │
    // │    │└────────────┘
    // │    │┌─ prompt ───┐
    // └────┘└────────────┘
    fn comms() -> Vec<Rect> {
        vec![Rect::new(0, 0, 30, 40), Rect::new(30, 0, 100, 36), Rect::new(30, 36, 100, 4)]
    }

    #[test]
    fn moves_by_screen_position() {
        let s = comms();
        assert_eq!(neighbour(0, &s, 'l'), Some(1)); // overlapping chat beats the prompt
        assert_eq!(neighbour(1, &s, 'j'), Some(2));
        assert_eq!(neighbour(2, &s, 'k'), Some(1));
        assert_eq!(neighbour(2, &s, 'h'), Some(0));
        assert_eq!(neighbour(0, &s, 'h'), None);
    }

    #[test]
    fn hidden_sections_are_skipped_and_clicks_land() {
        let mut s = comms();
        s[1] = Rect::default();
        assert_eq!(neighbour(0, &s, 'l'), Some(2));
        assert_eq!(hit(&comms(), 31, 37), Some(2));
    }
}
