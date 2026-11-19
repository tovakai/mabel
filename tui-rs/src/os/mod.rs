//! The OS part of MABEL-OS, the machinery every channel shares: the app's state and key routing, the channel
//! interface, the frame around the screen, colours, config, and talking to Mabel.
//!
//! Start with `app.rs` (what happens to a key press) and `channel.rs` (what a channel is).

/// Mabel's HTTP API: chats, music, LIFE, Beeper, the tape repo
pub mod api;
/// the app: channels, focus, key routing, the casing around them
pub mod app;
/// Mabel's pixel face (COMMS and PIXEL's :avatar)
pub mod avatar;
/// the Channel trait, Ctx (what a channel may use), Request, the channel list
pub mod channel;
/// ~/.config/mabel-tui/config.toml
pub mod config;
/// the `?` card: each channel's keys
pub mod help;
/// key matching that survives a Swedish keyboard and the Linux console
pub mod keys;
/// nav mode: which section h/j/k/l move to
pub mod nav;
/// the colours (and the deck preset)
pub mod palette;
/// a channel made of several apps (COMMS, CREATIVE, SYS)
pub mod suite;
/// dates and times for display
pub mod timefmt;
/// ctrl+t: the colour tuner
pub mod tuner;
/// the casing, tabs, panels, status line, footer, toasts, help card
pub mod ui;
/// F12: installing the build Mabel serves
pub mod update;
