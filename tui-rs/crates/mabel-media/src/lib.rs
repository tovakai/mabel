//! PIXEL pictures (`.mpx`), TRACKER songs (`.mtk`) and the synth that plays them.
//!
//! Split out of the MABEL-OS app (2026-11-19) so DreamTapes and standalone games can
//! read the same files and use the same engine. PIXEL and TRACKER stay general
//! creative tools: nothing here is game-specific.

pub mod pixel;
pub mod tracker;
pub mod tracksynth;

/// True if `tool` is an executable file somewhere on PATH.
pub fn have(tool: &str) -> bool {
    std::env::var_os("PATH").map(|p| std::env::split_paths(&p).any(|d| d.join(tool).is_file())).unwrap_or(false)
}
