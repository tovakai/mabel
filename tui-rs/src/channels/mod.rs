//! The channels, one folder (or file) each. `os/channel.rs` (`all`) builds the list and
//! decides the tab order; this file only declares them (rustfmt sorts them by name).

/// CH-03: Navidrome music, the player, offline downloads, MPRIS
pub mod audio;
/// CH-01: MABEL (chat), INBOX (Beeper), COURSE
pub mod comms;
/// CH-07: PIXEL, TRACKER and the file browser
pub mod creative;
/// Not a real channel: a commented one to copy when you make your own (see its first lines).
pub mod example;
/// CH-04: the Gremlin Index (tk-index)
pub mod inventory;
/// CH-02: what she's doing, her journal, his lists
pub mod life;
/// CH-08: settings and status (full on the deck)
pub mod sys;
/// CH-09: DreamTapes
pub mod tapes;
/// CH-06: shells
pub mod term;
/// CH-05: the Obsidian vault, with a vim editor
pub mod vault;
