//! The ten buttons a tape reads, and how terminal keys become them.
//!
//! "Held" needs key-release events (the kitty keyboard protocol: kitty and foot have it).
//! Without them, a held direction is guessed from key repeat and stays down for
//! `GUESS` after the last repeat. START and SELECT (enter, backspace) never report a
//! release even with the protocol, so they count as pressed for one frame only.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

/// Button numbers, the same in kit (`Btn`) and here.
pub const LEFT: usize = 0;
pub const RIGHT: usize = 1;
pub const UP: usize = 2;
pub const DOWN: usize = 3;
pub const A: usize = 4;
pub const B: usize = 5;
pub const X: usize = 6;
pub const Y: usize = 7;
pub const START: usize = 8;
pub const SELECT: usize = 9;
pub const COUNT: usize = 10;

pub const NAMES: [&str; COUNT] = ["left", "right", "up", "down", "a", "b", "x", "y", "start", "select"];

/// Without release events: how long a key counts as held after its last press or repeat.
/// Longer than a keyboard's first repeat delay (usually 250-600 ms).
const GUESS: Duration = Duration::from_millis(600);

/// The default keys per button, as key names (see `key_name` in the app).
fn defaults() -> [&'static [&'static str]; COUNT] {
    [
        &["left", "h", "a"],
        &["right", "l", "d"],
        &["up", "k", "w"],
        &["down", "j", "s"],
        &["z"],
        &["x"],
        &["c"],
        &["v"],
        &["enter"],
        &["backspace"],
    ]
}

/// Which button each key name presses.
#[derive(Clone, Debug)]
pub struct Keymap(BTreeMap<String, usize>);

impl Keymap {
    /// The defaults, with a tape's `[keys]` overrides: `a = ["space"]` gives A to space only.
    pub fn new(overrides: &BTreeMap<String, Vec<String>>) -> Self {
        let mut map = BTreeMap::new();
        for (button, keys) in defaults().iter().enumerate() {
            if overrides.contains_key(NAMES[button]) {
                continue;
            }
            for k in *keys {
                map.insert(k.to_string(), button);
            }
        }
        for (name, keys) in overrides {
            if let Some(button) = NAMES.iter().position(|n| n == name) {
                for k in keys {
                    map.insert(k.to_lowercase(), button);
                }
            }
        }
        Keymap(map)
    }

    pub fn button(&self, key: &str) -> Option<usize> {
        self.0.get(key).copied()
    }
}

impl Default for Keymap {
    fn default() -> Self {
        Keymap::new(&BTreeMap::new())
    }
}

#[derive(Clone, Debug, Default)]
pub struct Buttons {
    /// Down since (the last press or repeat), until released.
    down: [Option<Instant>; COUNT],
    /// Pressed since the last frame (not counting repeats).
    pressed: [bool; COUNT],
    /// What the tape sees during this frame.
    held_now: [bool; COUNT],
    pressed_now: [bool; COUNT],
    /// The terminal sends key releases (seen at least one).
    releases: bool,
}

impl Buttons {
    pub fn press(&mut self, b: usize, repeat: bool, now: Instant) {
        if b < COUNT {
            if !repeat && self.down[b].is_none() {
                self.pressed[b] = true;
            }
            if !repeat && !self.releases {
                self.pressed[b] = true; // no releases: every real press is a new press
            }
            self.down[b] = Some(now);
        }
    }

    pub fn release(&mut self, b: usize) {
        self.releases = true;
        if b < COUNT {
            self.down[b] = None;
        }
    }

    /// Everything up (the tape was paused or reset).
    pub fn clear(&mut self) {
        let releases = self.releases;
        *self = Buttons { releases, ..Buttons::default() };
    }

    /// Take a snapshot for the frame about to run.
    pub fn begin_frame(&mut self, now: Instant) {
        for b in 0..COUNT {
            let one_frame = b == START || b == SELECT;
            if let Some(at) = self.down[b]
                && (one_frame || (!self.releases && now.duration_since(at) > GUESS))
            {
                self.down[b] = None;
            }
            self.held_now[b] = self.down[b].is_some() || self.pressed[b];
            self.pressed_now[b] = self.pressed[b];
            self.pressed[b] = false;
        }
    }

    /// Held down during this frame.
    pub fn btn(&self, b: usize) -> bool {
        self.held_now.get(b).copied().unwrap_or(false)
    }

    /// Pressed since the last frame.
    pub fn btnp(&self, b: usize) -> bool {
        self.pressed_now.get(b).copied().unwrap_or(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keymap_defaults_and_overrides() {
        let km = Keymap::default();
        assert_eq!(km.button("left"), Some(LEFT));
        assert_eq!(km.button("a"), Some(LEFT)); // WASD
        assert_eq!(km.button("z"), Some(A));
        let over = BTreeMap::from([("a".to_string(), vec!["Space".to_string()])]);
        let km = Keymap::new(&over);
        assert_eq!(km.button("space"), Some(A));
        assert_eq!(km.button("z"), None);
    }

    #[test]
    fn held_and_pressed_with_releases() {
        let t = Instant::now();
        let mut b = Buttons::default();
        b.release(UP); // the terminal speaks releases
        b.press(LEFT, false, t);
        b.begin_frame(t);
        assert!(b.btn(LEFT) && b.btnp(LEFT));
        b.press(LEFT, true, t);
        b.begin_frame(t + Duration::from_secs(2));
        assert!(b.btn(LEFT) && !b.btnp(LEFT)); // still held, no new press
        b.release(LEFT);
        b.begin_frame(t + Duration::from_secs(3));
        assert!(!b.btn(LEFT));
    }

    #[test]
    fn a_tap_between_frames_still_counts() {
        let t = Instant::now();
        let mut b = Buttons::default();
        b.release(A);
        b.press(A, false, t);
        b.release(A);
        b.begin_frame(t);
        assert!(b.btn(A) && b.btnp(A));
    }

    #[test]
    fn without_releases_held_is_guessed() {
        let t = Instant::now();
        let mut b = Buttons::default();
        b.press(RIGHT, false, t);
        b.begin_frame(t + Duration::from_millis(100));
        assert!(b.btn(RIGHT));
        b.begin_frame(t + Duration::from_millis(900));
        assert!(!b.btn(RIGHT));
    }

    #[test]
    fn start_is_one_frame() {
        let t = Instant::now();
        let mut b = Buttons::default();
        b.release(A);
        b.press(START, false, t);
        b.begin_frame(t);
        assert!(b.btnp(START));
        b.begin_frame(t);
        assert!(!b.btn(START));
    }
}
