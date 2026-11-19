//! A tape's sound: one TRACKER song as music, plus short sound effects on top.
//!
//! A sound effect is one phrase of a `.mtk` file played once, on its own engine,
//! mixed over the music. Nothing here changes how TRACKER itself plays.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use mabel_media::tracker::{self, Song};
use mabel_media::tracksynth::{Engine, Midi, Output, Source};

/// At most this many sound effects at once; a new one replaces the oldest.
/// Sound effects at once (a fifth replaces the oldest), and samples per block sent to the player.
const MAX_SFX: usize = 4;
const BLOCK: usize = 512;

/// One sound effect: a phrase playing once.
struct Sfx {
    engine: Engine,
    /// Samples of release tail left once the phrase came round.
    tail: Option<usize>,
}

/// Mixes the music and the effects into one stream (what the player thread reads).
pub struct Mixer {
    sr: u32,
    music: Option<Engine>,
    sfx: Vec<Sfx>,
    /// Silent while the tape is paused.
    pub paused: bool,
}

impl Source for Mixer {
    fn sample_rate(&self) -> u32 {
        self.sr
    }

    fn render(&mut self, frames: usize) -> Vec<[f32; 2]> {
        let mut out = vec![[0.0f32; 2]; frames];
        if self.paused {
            return out;
        }
        let mut add = |part: Vec<[f32; 2]>| {
            for (o, p) in out.iter_mut().zip(part) {
                o[0] += p[0];
                o[1] += p[1];
            }
        };
        if let Some(m) = &mut self.music {
            add(m.render(frames));
        }
        for s in &mut self.sfx {
            add(s.engine.render(frames));
            match &mut s.tail {
                None if s.engine.looped_once() => {
                    s.engine.stop_all();
                    s.tail = Some(self.sr as usize / 2);
                }
                Some(left) => *left = left.saturating_sub(frames),
                None => {}
            }
        }
        self.sfx.retain(|s| s.tail != Some(0));
        for o in &mut out {
            o[0] = o[0].clamp(-1.0, 1.0);
            o[1] = o[1].clamp(-1.0, 1.0);
        }
        out
    }
}

/// A tape's sound: the mixer, the player, and the songs it has loaded.
pub struct Sound {
    root: PathBuf,
    mix: Arc<Mutex<Mixer>>,
    out: Option<Output>,
    songs: HashMap<String, Song>,
}

impl Sound {
    /// Sound for a tape whose files are under `root` (`music/<name>.mtk`).
    pub fn new(root: &Path, sr: u32) -> Self {
        let mix = Mixer { sr, music: None, sfx: vec![], paused: false };
        Sound { root: root.to_path_buf(), mix: Arc::new(Mutex::new(mix)), out: None, songs: HashMap::new() }
    }

    /// `music/<name>.mtk`, loaded once and kept.
    fn song(&mut self, name: &str) -> Result<Song, String> {
        if let Some(s) = self.songs.get(name) {
            return Ok(s.clone());
        }
        if name.is_empty() || name.contains('/') || name.contains("..") {
            return Err(format!("music name {name:?}: just the file name, without music/ or .mtk"));
        }
        let path = self.root.join("music").join(format!("{name}.mtk"));
        let song = tracker::load(&path).map_err(|e| format!("music/{name}.mtk: {e:#}"))?;
        self.songs.insert(name.to_string(), song.clone());
        Ok(song)
    }

    /// The sample rate.
    fn sr(&self) -> u32 {
        self.mix.lock().map(|m| m.sr).unwrap_or(44100)
    }

    /// The player process starts with the first sound, not before.
    fn ensure_output(&mut self) {
        if self.out.is_none() {
            self.out = Some(Output::start(self.mix.clone(), BLOCK));
        }
    }

    /// Play a song from the start, looping like TRACKER does. Replaces the music playing.
    pub fn music(&mut self, name: &str) -> Result<(), String> {
        let song = self.song(name)?;
        let midi = Midi::open(Some(&song.midi_port));
        let mut engine = Engine::new(song, self.sr(), midi);
        engine.play_song(0);
        self.ensure_output();
        if let Ok(mut m) = self.mix.lock() {
            m.music = Some(engine);
        }
        Ok(())
    }

    /// Stop the music (its notes fade out as they're released).
    pub fn music_stop(&mut self) {
        if let Ok(mut m) = self.mix.lock()
            && let Some(e) = &mut m.music
        {
            e.stop_all();
        }
    }

    /// Phrase `phrase` of song `name`, once, over the music.
    pub fn sfx(&mut self, name: &str, phrase: i64) -> Result<(), String> {
        let song = self.song(name)?;
        if !song.phrases.contains_key(&phrase) {
            return Err(format!("music/{name}.mtk has no phrase {phrase:02X}"));
        }
        let mut engine = Engine::new(song, self.sr(), Midi::open(None));
        engine.play_phrase(0, phrase);
        self.ensure_output();
        if let Ok(mut m) = self.mix.lock() {
            if m.sfx.len() >= MAX_SFX {
                m.sfx.remove(0);
            }
            m.sfx.push(Sfx { engine, tail: None });
        }
        Ok(())
    }

    /// Paused: silence (the engines keep their place).
    pub fn pause(&mut self, paused: bool) {
        if let Ok(mut m) = self.mix.lock() {
            m.paused = paused;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sound_effect_plays_once_then_leaves() {
        let mut song = tracker::demo();
        song.bpm = 300;
        let phrase = *song.phrases.keys().next().unwrap();
        let mut mix = Mixer { sr: 8000, music: None, sfx: vec![], paused: false };
        let mut e = Engine::new(song, 8000, Midi::open(None));
        e.play_phrase(0, phrase);
        mix.sfx.push(Sfx { engine: e, tail: None });
        // 16 steps at 300 bpm is well under 10 s.
        for _ in 0..(8000 * 10 / 512) {
            mix.render(512);
            if mix.sfx.is_empty() {
                return;
            }
        }
        panic!("the sound effect never finished");
    }

    #[test]
    fn paused_is_silent() {
        let mut mix = Mixer { sr: 8000, music: None, sfx: vec![], paused: true };
        assert!(mix.render(64).iter().all(|s| s == &[0.0, 0.0]));
    }
}
