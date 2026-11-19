//! The channels, one folder (or file) each. `os/channel.rs` (`all`) builds the list and
//! decides the tab order; this file only declares them (rustfmt sorts them by name).

/// CH-02: MABEL (chat), INBOX (Beeper), COURSE
pub mod comms;
/// CH-05: PIXEL, TRACKER and the file browser
pub mod creative;
/// Not a real channel: a commented one to copy when you make your own (see its first lines).
pub mod example;
/// CH-01: the home screen (a wallpaper)
pub mod home;
/// CH-03: HER, VAULT, INVENTORY
pub mod life;
/// CH-04: AUDIO (a gallery later)
pub mod media;
/// CH-00: settings and status (full on the deck), always the last tab
pub mod sys;
/// CH-07: DreamTapes
pub mod tapes;
/// CH-06: shells
pub mod term;
