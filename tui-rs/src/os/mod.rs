//! The OS part of MABEL-OS, the machinery every channel shares: the app's state and key routing, the channel
//! interface, the frame around the screen, colours, config, and talking to Mabel.
//!
//! Start with `app.rs` (what happens to a key press) and `channel.rs` (what a channel is).

pub mod api; // Mabel's HTTP API: chats, music, LIFE, Beeper, the tape repo
pub mod app; // the app: channels, focus, key routing, the casing around them
pub mod avatar; // Mabel's pixel face (COMMS and PIXEL's :avatar)
pub mod channel; // the Channel trait, Ctx (what a channel may use), Request, the channel list
pub mod config; // ~/.config/mabel-tui/config.toml
pub mod help; // the `?` card: each channel's keys
pub mod keys; // key matching that survives a Swedish keyboard and the Linux console
pub mod nav; // nav mode: which section h/j/k/l move to
pub mod palette; // the colours (and the deck preset)
pub mod suite; // a channel made of several apps (COMMS, CREATIVE, SYS)
pub mod timefmt; // dates and times for display
pub mod tuner; // ctrl+t: the colour tuner
pub mod ui; // the casing, tabs, panels, status line, footer, toasts, help card
pub mod update; // F12: installing the build Mabel serves
