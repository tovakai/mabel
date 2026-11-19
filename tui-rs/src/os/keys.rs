//! Key matching that survives a Swedish keyboard and the Linux console.
//!
//! Punctuation is matched by the character that arrives, never by the physical key: on a
//! Swedish layout `?` is shift++ and `[ ] { }` are AltGr chords, so a check that also demanded
//! "no shift" would miss them. Letters with ctrl/alt are matched case-blind.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

/// A plain character: no ctrl, no alt (shift and AltGr are part of the character).
pub fn ch(key: &KeyEvent) -> Option<char> {
    let mods = key.modifiers;
    // AltGr arrives as ctrl+alt on some terminals: the character is what counts then.
    let altgr = mods.contains(KeyModifiers::CONTROL | KeyModifiers::ALT);
    match key.code {
        KeyCode::Char(c) if altgr || !mods.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => Some(c),
        _ => None,
    }
}

/// Is this key the character `c`?
pub fn is(key: &KeyEvent, c: char) -> bool {
    ch(key) == Some(c)
}

/// Is this ctrl+`c` (either case)?
pub fn ctrl(key: &KeyEvent, c: char) -> bool {
    match key.code {
        KeyCode::Char(k) => key.modifiers == KeyModifiers::CONTROL && k.eq_ignore_ascii_case(&c),
        _ => false,
    }
}

/// ctrl+]: the escape hatch out of a TERM shell. The legacy byte 0x1d arrives as ctrl+5.
pub fn ctrl_bracket(key: &KeyEvent) -> bool {
    key.modifiers.contains(KeyModifiers::CONTROL) && matches!(key.code, KeyCode::Char(']' | '5'))
}

/// alt+1…8 → 1…8 (the channel keys that work without F-keys).
pub fn alt_digit(key: &KeyEvent) -> Option<usize> {
    match key.code {
        KeyCode::Char(c @ '1'..='9') if key.modifiers == KeyModifiers::ALT => c.to_digit(10).map(|d| d as usize),
        _ => None,
    }
}

/// Which F-key, if it's one.
pub fn fkey(key: &KeyEvent) -> Option<u8> {
    match key.code {
        KeyCode::F(n) => Some(n),
        _ => None,
    }
}

/// h j k l as the arrows they stand for inside a section.
pub fn arrow_for(c: char) -> Option<KeyEvent> {
    let code = match c {
        'h' => KeyCode::Left,
        'j' => KeyCode::Down,
        'k' => KeyCode::Up,
        'l' => KeyCode::Right,
        _ => return None,
    };
    Some(KeyEvent::new(code, KeyModifiers::NONE))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(code: KeyCode, mods: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, mods)
    }

    #[test]
    fn swedish_punctuation_and_altgr() {
        assert!(is(&k(KeyCode::Char('?'), KeyModifiers::SHIFT), '?'));
        assert!(is(&k(KeyCode::Char('['), KeyModifiers::CONTROL | KeyModifiers::ALT), '['));
        assert!(!is(&k(KeyCode::Char('h'), KeyModifiers::CONTROL), 'h'));
        assert!(ctrl(&k(KeyCode::Char('Q'), KeyModifiers::CONTROL), 'q'));
        assert_eq!(alt_digit(&k(KeyCode::Char('3'), KeyModifiers::ALT)), Some(3));
        assert!(ctrl_bracket(&k(KeyCode::Char('5'), KeyModifiers::CONTROL)));
    }
}
