//! `?`: the keys of the channel (or app) on screen, then the ones that work everywhere.
//! Kept in step with HELP in tui/mabel_tui/app.py; a channel lists only what this build has.

pub type Keys = &'static [(&'static str, &'static str)];

pub const EVERYWHERE: Keys = &[
    ("alt+1–8 / F1–F8", "switch channel (again on COMMS, CREATIVE or SYS: their next app)"),
    ("h j k l", "move between sections (the lit border)"),
    ("i / a / enter", "go into the lit section"),
    ("esc", "back out to the sections"),
    ("inside: h j k l", "like the arrow keys, where the section doesn't use the letter"),
    ("ctrl+z", "drop to a plain shell (exit comes back)"),
    ("ctrl+p", "play / pause music"),
    ("ctrl+t", "colour tuner (for screens that show colours their own way)"),
    ("?", "this list"),
    ("ctrl+q", "power off"),
];

/// A channel's own keys by its id. Empty until the channel is ported.
#[allow(clippy::match_single_binding)] // channels add their arms as they are ported
pub fn channel(id: &str) -> Keys {
    match id {
        _ => &[],
    }
}
