//! TRACKER's engine: a tick sequencer driving a small chip synth and MIDI out.
//!
//! Time runs in ticks, 24 per beat (LSDJ's tempo): a step lasts `groove[i]` ticks (6 = a 16th).
//! `Engine::render(n)` makes n stereo samples and runs every tick that falls inside them, so the
//! same code plays live (`Output`: a thread piping to `aplay` or `pw-cat`) and renders files
//! offline (`export_wav`, `export_midi`) with no sound card at all.
//!
//! MIDI goes straight to a rawmidi device (/dev/snd/midiC*D*), like Mabel's music job does for
//! the NSX-39 Pocket Miku; no MIDI library needed.

use std::collections::{BTreeSet, HashMap};
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use anyhow::Result;

use crate::tracker::{self as tk, Instrument, Song, TableRow};

/// WAV and MIDI files (`export_wav`, `export_midi`).
mod export;
/// MIDI out (`Midi`, `midi_ports`).
mod midi;
/// Playing live (`Output`, `Source`).
mod output;
/// Walking through the song, step by step.
mod sequencer;
/// Notes, commands and the sound each voice makes.
mod voice;

pub use export::{export_midi, export_wav};
pub use midi::{Midi, midi_ports};
pub use output::{Output, Source};

/// The sequencer's clock: ticks per beat.
pub const TICKS_PER_BEAT: f64 = 24.0;
/// The mix's overall level before soft limiting (8 voices together stay below clipping).
const MASTER: f64 = 0.32;
/// Samples a gain change takes (~1.5 ms at 44.1 kHz): steps and note starts don't click.
const RAMP: usize = 64;

/// A MIDI note number (69 = A4) as a frequency in Hz (fractions for slides and bends).
pub fn note_hz(n: f64) -> f64 {
    440.0 * 2f64.powf((n - 69.0) / 12.0)
}

/// Noise as the Python engine makes it: a fixed table, so the hats sound the same every run.
fn noise_table() -> Vec<f64> {
    let mut x: u64 = 7;
    (0..32768)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            (x as f64 / u64::MAX as f64) * 2.0 - 1.0
        })
        .collect()
}

// ── a voice per track ────────────────────────────────────────────────────

/// Where a voice is in its volume envelope (attack, decay, sustain, release).
#[derive(Clone, Copy, PartialEq)]
enum Stage {
    Off,
    Attack,
    Decay,
    Sustain,
    Release,
}

/// One track's sound: the note playing, its envelope, and the state of every command on it.
struct Voice {
    #[allow(dead_code)] // kept to mirror the Python engine; handy when debugging
    track: usize,
    /// The instrument playing (a copy, so edits mid-note don't jump) and its number.
    inst: Option<Instrument>,
    inst_id: Option<i64>,
    /// A note is held.
    on: bool,
    /// The envelope: current level, stage, and the level release started from.
    level: f64,
    stage: Stage,
    rel_from: f64,
    /// The pitch (a note number, fractional while sliding), and L's slide target and speed.
    note: f64,
    target: Option<f64>,
    slide: f64,
    /// Where the oscillator is in its cycle.
    phase: f64,
    /// E's volume, O's pan, W's pulse width, and the wave for "wave" instruments.
    vol: f64,
    pan: f64,
    width: f64,
    wave: Vec<f64>,
    /// A's table (or the instrument's): its rows, the row it's on, and its volume/transpose now.
    table: Option<Vec<TableRow>>,
    table_row: usize,
    table_vol: f64,
    table_tr: i64,
    /// C's chord (two intervals) and which note of it is sounding.
    chord: Option<(i64, i64)>,
    chord_i: usize,
    /// P's bend so far, V's vibrato (speed, depth) and its phase.
    bend: f64,
    vib: (i64, i64),
    vib_phase: f64,
    /// R: retrigger every n ticks (and the countdown); K: ticks until the note is killed.
    retrig: i64,
    retrig_n: i64,
    kill: i64,
    /// D: ticks to wait, and the note waiting (note, instrument).
    delay: i64,
    pending: Option<(i64, Option<i64>)>,
    /// The MIDI note sounding (channel, note), so it can be released.
    midi_note: Option<(i64, i64)>,
    /// How loud it is now (TRACKER's level meters).
    pub peak: f64,
    /// The gain last rendered, ramped from on every change.
    amp: f64,
}

impl Voice {
    /// A silent voice.
    fn new(track: usize) -> Self {
        Voice {
            track,
            inst: None,
            inst_id: None,
            on: false,
            level: 0.0,
            stage: Stage::Off,
            rel_from: 1.0,
            note: 60.0,
            target: None,
            slide: 0.0,
            phase: 0.0,
            vol: 1.0,
            pan: 0.5,
            width: 0.5,
            wave: vec![0.0; 32],
            table: None,
            table_row: 0,
            table_vol: 1.0,
            table_tr: 0,
            chord: None,
            chord_i: 0,
            bend: 0.0,
            vib: (0, 0),
            vib_phase: 0.0,
            retrig: 0,
            retrig_n: 0,
            kill: -1,
            delay: -1,
            pending: None,
            midi_note: None,
            peak: 0.0,
            amp: 0.0,
        }
    }
}

/// Wave `id` of the song as samples in -1..1 (a flat wave if it doesn't exist).
fn wave_of(song: &Song, id: i64) -> Vec<f64> {
    song.waves.get(&id).cloned().unwrap_or(vec![8; 32]).iter().map(|v| *v as f64 / 7.5 - 1.0).collect()
}

// ── the sequencer ────────────────────────────────────────────────────────

/// What's playing: the whole song, one chain looping, or one phrase looping.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    Song,
    Chain,
    Phrase,
}

/// One track's place in the song: which row, chain step and phrase step, and the step's timing.
struct Track {
    #[allow(dead_code)] // kept to mirror the Python engine; handy when debugging
    i: usize,
    /// Song row, chain step, phrase step.
    row: usize,
    cstep: usize,
    pstep: usize,
    /// Ticks until the next step, and which groove entry timed this one.
    ticks_left: i64,
    groove_i: usize,
    /// The next step is the first (play it straight away).
    fresh: bool,
    /// H: end the phrase after this step.
    hop: bool,
    /// Came back round to its start (the song's end, or a phrase loop): exports stop here.
    looped: bool,
    running: bool,
}

impl Track {
    /// A stopped track.
    fn new(i: usize) -> Self {
        Track {
            i,
            row: 0,
            cstep: 0,
            pstep: 0,
            ticks_left: 0,
            groove_i: 0,
            fresh: true,
            hop: false,
            looped: false,
            running: false,
        }
    }

    /// Back to the start of a song row.
    fn reset(&mut self, row: usize) {
        (self.row, self.cstep, self.pstep, self.ticks_left, self.groove_i, self.fresh, self.hop, self.looped) =
            (row, 0, 0, 0, 0, true, false, false);
    }
}

/// A note event for MIDI export: (sample, on?, track, note, instrument).
type NoteEvent = (u64, bool, usize, i64, Option<Instrument>);

/// The engine: a song, eight tracks walking through it, eight voices sounding them, MIDI out.
pub struct Engine {
    pub song: Song,
    /// Sample rate.
    pub sr: u32,
    pub midi: Midi,
    voices: Vec<Voice>,
    tracks: Vec<Track>,
    /// What's playing (None: stopped).
    pub mode: Option<Mode>,
    /// (track, chain or phrase id) for chain/phrase loops.
    pub focus: (usize, i64),
    /// The tempo now (T changes it while playing).
    pub bpm: i64,
    /// Samples until the next tick.
    tick_left: f64,
    /// m mutes tracks; s solos one.
    pub mutes: BTreeSet<usize>,
    pub solo: Option<usize>,
    /// Notes logged for MIDI export (None when not exporting).
    events: Option<Vec<NoteEvent>>,
    /// Samples made so far.
    samples: u64,
    /// Preview notes: ticks until each track's preview is released.
    preview_off: HashMap<usize, i64>,
    /// The noise table (the same every run).
    noise: Vec<f64>,
}

impl Engine {
    /// An engine for `song` at sample rate `sr`, stopped.
    pub fn new(song: Song, sr: u32, midi: Midi) -> Self {
        let bpm = song.bpm;
        Engine {
            song,
            sr,
            midi,
            voices: (0..tk::MAX_TRACKS).map(Voice::new).collect(),
            tracks: (0..tk::MAX_TRACKS).map(Track::new).collect(),
            mode: None,
            focus: (0, 0),
            bpm,
            tick_left: 0.0,
            mutes: BTreeSet::new(),
            solo: None,
            events: None,
            samples: 0,
            preview_off: HashMap::new(),
            noise: noise_table(),
        }
    }

    /// Is anything playing?
    pub fn playing(&self) -> bool {
        self.mode.is_some()
    }

    /// A track has come back round to its start (a phrase loop, or the song's end).
    pub fn looped_once(&self) -> bool {
        self.tracks.iter().any(|t| t.looped)
    }

    /// Is this track silent (muted, or another one soloed)?
    pub fn muted(&self, track: usize) -> bool {
        self.mutes.contains(&track) || self.solo.is_some_and(|s| s != track)
    }

    /// How loud a track is now (for the meters).
    pub fn peak(&self, track: usize) -> f64 {
        self.voices[track].peak
    }

    /// (song row, chain step, phrase step) of a playing track.
    pub fn position(&self, track: usize) -> Option<(usize, usize, usize)> {
        let t = &self.tracks[track];
        t.running.then_some((t.row, t.cstep, t.pstep))
    }

    /// Stop everything and start playing in `mode`.
    fn start(&mut self, mode: Mode, focus: (usize, i64)) {
        self.stop_all();
        (self.mode, self.focus, self.bpm, self.tick_left) = (Some(mode), focus, self.song.bpm, 0.0);
    }

    /// Play the song from `row` (each track from the start of the block its row is in).
    pub fn play_song(&mut self, row: usize) {
        self.start(Mode::Song, (0, 0));
        for i in 0..self.song.tracks {
            self.tracks[i].reset(row);
            let cell = self.cell(i).is_some();
            let start = self.block_start(i, row);
            self.tracks[i].running = cell || start.is_some();
            if self.tracks[i].running && !cell {
                self.tracks[i].row = start.unwrap_or(row);
            }
        }
    }

    /// Loop one chain on one track.
    pub fn play_chain(&mut self, track: usize, chain: i64) {
        self.start(Mode::Chain, (track, chain));
        self.tracks[track].reset(0);
        self.tracks[track].running = true;
    }

    /// Loop one phrase on one track (tapes' sound effects use this, stopping after one pass).
    pub fn play_phrase(&mut self, track: usize, phrase: i64) {
        self.start(Mode::Phrase, (track, phrase));
        self.tracks[track].reset(0);
        self.tracks[track].running = true;
    }

    /// Stop: every track stops, every note is released, MIDI gets "all notes off".
    pub fn stop_all(&mut self) {
        self.mode = None;
        for t in &mut self.tracks {
            t.running = false;
        }
        for i in 0..self.voices.len() {
            self.release(i);
        }
        self.midi.all_off();
    }

    /// One note on a track's voice while editing (released after half a beat).
    pub fn preview(&mut self, track: usize, note: i32, inst: Option<i64>) {
        if note == tk::NOTE_OFF {
            self.release(track);
            return;
        }
        self.trigger(track, note as i64, inst, 0);
        self.preview_off.insert(track, (TICKS_PER_BEAT / 2.0) as i64);
    }

    /// m: mute or unmute a track.
    pub fn toggle_mute(&mut self, track: usize) {
        if !self.mutes.remove(&track) {
            self.mutes.insert(track);
        }
    }

    /// The next `frames` stereo samples, running every tick that falls inside them, soft-limited
    /// (a full mix rounds off, no hard clip).
    pub fn render(&mut self, frames: usize) -> Vec<[f32; 2]> {
        let mut out = vec![[0.0f64; 2]; frames];
        let mut pos = 0;
        while pos < frames {
            if self.tick_left <= 0.0 {
                self.tick();
                self.tick_left += self.sr as f64 * 60.0 / (self.bpm.max(20) as f64 * TICKS_PER_BEAT);
            }
            let n = (frames - pos).min((self.tick_left.ceil() as usize).max(1));
            for t in 0..self.voices.len() {
                self.voice_render(t, &mut out[pos..pos + n]);
            }
            pos += n;
            self.tick_left -= n as f64;
            self.samples += n as u64;
        }
        out.iter().map(|[l, r]| [(l * MASTER).tanh() as f32, (r * MASTER).tanh() as f32]).collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_demo_plays_and_exports() {
        let mut e = Engine::new(tk::demo(), 22050, Midi::open(None));
        e.play_song(0);
        let audio = e.render(22050);
        let peak = audio.iter().map(|[l, r]| l.abs().max(r.abs())).fold(0.0f32, f32::max);
        assert!(peak > 0.05 && peak < 1.0, "sound, soft-limited: {peak}");
        assert_eq!(e.position(0).map(|p| p.0), Some(0));
        let dir = std::env::temp_dir().join(format!("mabel-trk-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let secs = export_wav(&tk::demo(), &dir.join("d.wav"), 22050).unwrap();
        assert!(secs > 3.0 && secs < 20.0, "{secs}");
        let notes = export_midi(&tk::demo(), &dir.join("d.mid"), 22050).unwrap();
        assert!(notes > 20, "{notes}");
        assert_eq!(&std::fs::read(dir.join("d.mid")).unwrap()[..4], b"MThd");
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// TRACKER_OUT=<dir> cargo test export_for_parity -- --ignored: files to compare with Python's.
    #[test]
    #[ignore]
    fn export_for_parity() {
        let Ok(dir) = std::env::var("TRACKER_OUT") else { return };
        let dir = std::path::PathBuf::from(dir);
        println!("rust wav secs {:.3}", export_wav(&tk::demo(), &dir.join("rs.wav"), 22050).unwrap());
        println!("rust midi notes {}", export_midi(&tk::demo(), &dir.join("rs.mid"), 22050).unwrap());
        if let Ok(quiet) = tk::load(&dir.join("quiet.mtk")) {
            export_wav(&quiet, &dir.join("rs-quiet.wav"), 22050).unwrap();
        }
    }
}
