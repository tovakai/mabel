//! The channels, one folder (or file) each, in tab order. `core/channel.rs` (`all`) builds
//! the list and decides the order; this file only declares them.

pub mod audio; // CH-03: Navidrome music, the player, offline downloads, MPRIS
pub mod comms; // CH-01: MABEL (chat), INBOX (Beeper), COURSE
pub mod creative; // CH-07: PIXEL, TRACKER and the file browser
pub mod inventory; // CH-04: the Gremlin Index (tk-index)
pub mod life; // CH-02: what she's doing, her journal, his lists
pub mod sys; // CH-08: settings and status (full on the deck)
pub mod tapes;
pub mod term; // CH-06: shells
pub mod vault; // CH-05: the Obsidian vault, with a vim editor // CH-09: DreamTapes
