//! CH-07 CREATIVE: a suite of PIXEL (sprites) and TRACKER (chip music), sharing the ctrl+o
//! file browser. The file formats and the synth live in the `mabel-media` crate, so tapes
//! and standalone games can read them too; these files are the screens.

pub mod browser; // ctrl+o: open and save files, with previews
pub mod pixelview; // PIXEL
pub mod trackerview; // TRACKER
