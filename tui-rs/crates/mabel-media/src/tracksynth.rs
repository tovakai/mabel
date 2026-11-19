//! TRACKER's engine: a tick sequencer driving a small chip synth and MIDI out.
//!
//! Time runs in ticks, 24 per beat (LSDJ's tempo): a step lasts groove[i] ticks (6 = a 16th).
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

pub const TICKS_PER_BEAT: f64 = 24.0;
const MASTER: f64 = 0.32;
/// Samples a gain change takes (~1.5 ms at 44.1 kHz): steps and note starts don't click.
const RAMP: usize = 64;

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

// ── MIDI ─────────────────────────────────────────────────────────────────

pub fn midi_ports() -> Vec<String> {
    let mut ports: Vec<String> = std::fs::read_dir("/dev/snd")
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path().to_string_lossy().into_owned())
        .filter(|p| p.rsplit('/').next().is_some_and(|n| n.starts_with("midiC")))
        .collect();
    ports.sort();
    ports
}

/// Writes raw MIDI bytes to a rawmidi device. Silent if there is none.
pub struct Midi {
    pub port: String,
    file: Option<std::fs::File>,
    programs: HashMap<i64, i64>,
}

impl Midi {
    /// `port`: a device path, "" = the first one found, None = no MIDI at all.
    pub fn open(port: Option<&str>) -> Self {
        let port = match port {
            None => String::new(),
            Some("") => midi_ports().into_iter().next().unwrap_or_default(),
            Some(p) => p.to_string(),
        };
        let file = (!port.is_empty())
            .then(|| std::fs::OpenOptions::new().write(true).custom_flags(libc::O_NONBLOCK).open(&port).ok())
            .flatten();
        Midi { port, file, programs: HashMap::new() }
    }

    pub fn connected(&self) -> bool {
        self.file.is_some()
    }

    fn send(&mut self, data: &[u8]) {
        if let Some(f) = &mut self.file {
            let _ = f.write_all(data);
        }
    }

    fn note_on(&mut self, ch: i64, note: i64, vel: i64) {
        self.send(&[0x90 | ((ch - 1) & 15) as u8, (note & 127) as u8, vel.clamp(1, 127) as u8]);
    }

    fn note_off(&mut self, ch: i64, note: i64) {
        self.send(&[0x80 | ((ch - 1) & 15) as u8, (note & 127) as u8, 0]);
    }

    fn program(&mut self, ch: i64, prog: i64) {
        if self.programs.get(&ch) != Some(&prog) {
            self.programs.insert(ch, prog);
            self.send(&[0xC0 | ((ch - 1) & 15) as u8, (prog & 127) as u8]);
        }
    }

    pub fn all_off(&mut self) {
        for ch in 0..16u8 {
            self.send(&[0xB0 | ch, 123, 0]);
        }
    }
}

impl Drop for Midi {
    fn drop(&mut self) {
        self.all_off();
    }
}

// ── a voice per track ────────────────────────────────────────────────────

#[derive(Clone, Copy, PartialEq)]
enum Stage {
    Off,
    Attack,
    Decay,
    Sustain,
    Release,
}

struct Voice {
    #[allow(dead_code)] // kept to mirror the Python engine; handy when debugging
    track: usize,
    inst: Option<Instrument>,
    inst_id: Option<i64>,
    on: bool,
    level: f64,
    stage: Stage,
    rel_from: f64,
    note: f64,
    target: Option<f64>,
    slide: f64,
    phase: f64,
    vol: f64,
    pan: f64,
    width: f64,
    wave: Vec<f64>,
    table: Option<Vec<TableRow>>,
    table_row: usize,
    table_vol: f64,
    table_tr: i64,
    chord: Option<(i64, i64)>,
    chord_i: usize,
    bend: f64,
    vib: (i64, i64),
    vib_phase: f64,
    retrig: i64,
    retrig_n: i64,
    kill: i64,
    delay: i64,
    pending: Option<(i64, Option<i64>)>,
    midi_note: Option<(i64, i64)>,
    pub peak: f64,
    /// The gain last rendered, ramped from on every change.
    amp: f64,
}

impl Voice {
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

fn wave_of(song: &Song, id: i64) -> Vec<f64> {
    song.waves.get(&id).cloned().unwrap_or(vec![8; 32]).iter().map(|v| *v as f64 / 7.5 - 1.0).collect()
}

// ── the sequencer ────────────────────────────────────────────────────────

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Mode {
    Song,
    Chain,
    Phrase,
}

struct Track {
    #[allow(dead_code)] // kept to mirror the Python engine; handy when debugging
    i: usize,
    row: usize,
    cstep: usize,
    pstep: usize,
    ticks_left: i64,
    groove_i: usize,
    fresh: bool,
    hop: bool,
    looped: bool,
    running: bool,
}

impl Track {
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

    fn reset(&mut self, row: usize) {
        (self.row, self.cstep, self.pstep, self.ticks_left, self.groove_i, self.fresh, self.hop, self.looped) =
            (row, 0, 0, 0, 0, true, false, false);
    }
}

/// A note event for MIDI export: (sample, on?, track, note, instrument).
type NoteEvent = (u64, bool, usize, i64, Option<Instrument>);

pub struct Engine {
    pub song: Song,
    pub sr: u32,
    pub midi: Midi,
    voices: Vec<Voice>,
    tracks: Vec<Track>,
    pub mode: Option<Mode>,
    /// (track, chain or phrase id) for chain/phrase loops.
    pub focus: (usize, i64),
    pub bpm: i64,
    tick_left: f64,
    pub mutes: BTreeSet<usize>,
    pub solo: Option<usize>,
    events: Option<Vec<NoteEvent>>,
    samples: u64,
    preview_off: HashMap<usize, i64>,
    noise: Vec<f64>,
}

impl Engine {
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

    pub fn playing(&self) -> bool {
        self.mode.is_some()
    }

    /// A track has come back round to its start (a phrase loop, or the song's end).
    pub fn looped_once(&self) -> bool {
        self.tracks.iter().any(|t| t.looped)
    }

    pub fn muted(&self, track: usize) -> bool {
        self.mutes.contains(&track) || self.solo.is_some_and(|s| s != track)
    }

    pub fn peak(&self, track: usize) -> f64 {
        self.voices[track].peak
    }

    /// (song row, chain step, phrase step) of a playing track.
    pub fn position(&self, track: usize) -> Option<(usize, usize, usize)> {
        let t = &self.tracks[track];
        t.running.then_some((t.row, t.cstep, t.pstep))
    }

    fn start(&mut self, mode: Mode, focus: (usize, i64)) {
        self.stop_all();
        (self.mode, self.focus, self.bpm, self.tick_left) = (Some(mode), focus, self.song.bpm, 0.0);
    }

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

    pub fn play_chain(&mut self, track: usize, chain: i64) {
        self.start(Mode::Chain, (track, chain));
        self.tracks[track].reset(0);
        self.tracks[track].running = true;
    }

    pub fn play_phrase(&mut self, track: usize, phrase: i64) {
        self.start(Mode::Phrase, (track, phrase));
        self.tracks[track].reset(0);
        self.tracks[track].running = true;
    }

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

    pub fn toggle_mute(&mut self, track: usize) {
        if !self.mutes.remove(&track) {
            self.mutes.insert(track);
        }
    }

    // ── voices ───────────────────────────────────────────────────────────

    fn log(&mut self, on: bool, track: usize, note: i64, inst: Option<Instrument>) {
        let samples = self.samples;
        if let Some(e) = &mut self.events {
            e.push((samples, on, track, note, inst));
        }
    }

    fn trigger(&mut self, track: usize, note: i64, inst_id: Option<i64>, slide: i64) {
        let inst_id = inst_id.or(self.voices[track].inst_id).unwrap_or(0);
        let inst = self.song.instruments.get(&inst_id).cloned().unwrap_or_default();
        let note = note + tk::signed(inst.transpose);
        let same = self.voices[track].on && self.voices[track].inst_id == Some(inst_id);
        if slide != 0 && same {
            let v = &mut self.voices[track];
            (v.target, v.slide) = (Some(note as f64), slide as f64 / 16.0);
            self.midi_on(track, &inst, note);
            return;
        }
        let wave = wave_of(&self.song, if inst.kind == "wave" { inst.width } else { 0 });
        let table = inst.table.and_then(|t| self.song.tables.get(&t).cloned());
        let v = &mut self.voices[track];
        v.inst_id = Some(inst_id);
        (v.note, v.target) = (note as f64, None);
        (v.vol, v.pan) = (1.0, inst.pan as f64 / 255.0);
        v.width = (inst.width as f64 / 256.0).clamp(0.05, 0.95);
        v.wave = wave;
        v.table = table;
        (v.table_row, v.table_vol, v.table_tr) = (0, 1.0, 0);
        (v.chord, v.bend, v.vib, v.retrig, v.kill) = (None, 0.0, (0, 0), 0, -1);
        v.on = inst.kind != "midi" && (inst.out == "synth" || inst.out == "both");
        v.stage = if v.on { Stage::Attack } else { Stage::Off };
        v.level = if v.on && inst.attack > 0 {
            0.0
        } else if v.on {
            1.0
        } else {
            0.0
        };
        if v.on && inst.attack == 0 {
            v.stage = Stage::Decay;
        }
        v.inst = Some(inst.clone());
        self.midi_on(track, &inst, note);
        self.log(true, track, note, Some(inst));
    }

    fn midi_on(&mut self, track: usize, inst: &Instrument, note: i64) {
        self.midi_off(track);
        if inst.kind == "midi" || inst.out == "midi" || inst.out == "both" {
            let ch = inst.midi_channel.clamp(1, 16);
            if let Some(p) = inst.midi_program {
                self.midi.program(ch, p);
            }
            self.midi.note_on(ch, note, inst.volume / 2);
            self.voices[track].midi_note = Some((ch, note));
        }
    }

    fn midi_off(&mut self, track: usize) {
        if let Some((ch, note)) = self.voices[track].midi_note.take() {
            self.midi.note_off(ch, note);
        }
    }

    fn release(&mut self, track: usize) {
        let v = &mut self.voices[track];
        let sounding = !matches!(v.stage, Stage::Off | Stage::Release) || v.midi_note.is_some();
        if !matches!(v.stage, Stage::Off | Stage::Release) {
            v.stage = Stage::Release;
            v.rel_from = v.level;
        }
        let (note, inst) = (v.note as i64, v.inst.clone());
        self.midi_off(track);
        if sounding {
            self.log(false, track, note, inst);
        }
    }

    fn command(&mut self, track: usize, cmd: &str, val: i64) {
        let wave = (cmd == "W").then(|| wave_of(&self.song, val));
        let table = (cmd == "A").then(|| self.song.tables.get(&val).cloned()).flatten();
        let v = &mut self.voices[track];
        match cmd {
            "C" => v.chord = (val != 0).then_some((val >> 4, val & 15)),
            "E" => v.vol = val as f64 / 255.0,
            "K" => v.kill = val,
            "O" => v.pan = val as f64 / 255.0,
            "P" => v.bend = (val - 0x80) as f64 / 64.0,
            "R" => (v.retrig, v.retrig_n) = (val, val),
            "V" => v.vib = (val >> 4, val & 15),
            "W" => {
                if v.inst.as_ref().is_some_and(|i| i.kind == "wave") {
                    v.wave = wave.unwrap_or(vec![0.0; 32]);
                } else {
                    v.width = (val as f64 / 256.0).clamp(0.05, 0.95);
                }
            }
            "A" => (v.table, v.table_row) = (table, 0),
            _ => {}
        }
    }

    /// Once per tick: envelope, table, chord, slide, vibrato, retrigger, kill, delay.
    fn voice_tick(&mut self, track: usize) {
        {
            let v = &mut self.voices[track];
            if v.delay >= 0 {
                v.delay -= 1;
            }
        }
        if self.voices[track].delay < 0
            && let Some((note, inst)) = self.voices[track].pending.take()
        {
            self.trigger(track, note, inst, 0);
        }
        let Some(inst) = self.voices[track].inst.clone() else { return };
        let mut table_cmd = None;
        {
            let v = &mut self.voices[track];
            match v.stage {
                Stage::Attack => {
                    v.level += 1.0 / inst.attack.max(1) as f64;
                    if v.level >= 1.0 {
                        (v.level, v.stage) = (1.0, Stage::Decay);
                    }
                }
                Stage::Decay => {
                    let sus = inst.sustain as f64 / 255.0;
                    v.level -= (1.0 - sus) / inst.decay.max(1) as f64;
                    if v.level <= sus {
                        (v.level, v.stage) = (sus, Stage::Sustain);
                        if sus <= 0.0 {
                            (v.stage, v.on) = (Stage::Off, false);
                        }
                    }
                }
                Stage::Release => {
                    v.level -= v.rel_from / inst.release.max(1) as f64;
                    if v.level <= 0.0 {
                        (v.level, v.stage, v.on) = (0.0, Stage::Off, false);
                    }
                }
                _ => {}
            }
            if let Some(table) = &v.table {
                let row = table[v.table_row % tk::STEPS].clone();
                if let Some(vol) = row.vol {
                    v.table_vol = vol as f64 / 255.0;
                }
                v.table_tr = tk::signed(row.transpose);
                if row.cmd == "H" {
                    v.table_row = (row.val as usize) % tk::STEPS;
                } else {
                    if !row.cmd.is_empty() {
                        table_cmd = Some((row.cmd.clone(), row.val));
                    }
                    v.table_row += 1;
                }
            }
        }
        if let Some((cmd, val)) = table_cmd {
            self.command(track, &cmd, val);
        }
        let mut kill_now = false;
        {
            let v = &mut self.voices[track];
            if v.chord.is_some() {
                v.chord_i = (v.chord_i + 1) % 3;
            }
            v.note += v.bend;
            if let Some(target) = v.target {
                let step = if v.slide != 0.0 { v.slide } else { 1.0 };
                v.note = if target > v.note { target.min(v.note + step) } else { target.max(v.note - step) };
                if v.note == target {
                    v.target = None;
                }
            }
            if v.vib.0 != 0 {
                v.vib_phase += v.vib.0 as f64 / 16.0;
            }
            if v.retrig != 0 {
                v.retrig_n -= 1;
                if v.retrig_n <= 0 {
                    (v.retrig_n, v.level, v.stage) = (v.retrig, 1.0, Stage::Decay);
                }
            }
            if v.kill >= 0 {
                v.kill -= 1;
                kill_now = v.kill < 0;
            }
        }
        if kill_now {
            self.release(track);
        }
    }

    fn voice_render(&mut self, track: usize, out: &mut [[f64; 2]]) {
        let muted = self.muted(track);
        let sr = self.sr as f64;
        let v = &mut self.voices[track];
        let n = out.len();
        let Some(inst) = &v.inst else {
            v.amp = 0.0;
            return;
        };
        if !v.on || v.level <= 0.0 {
            v.peak *= 0.8;
            if v.amp > 0.0 {
                v.level = 0.0; // fade out what was sounding instead of cutting it
            } else {
                v.amp = 0.0;
                return;
            }
        }
        let mut pitch = v.note + v.table_tr as f64;
        if let Some((x, y)) = v.chord {
            pitch += [0, x, y][v.chord_i] as f64;
        }
        if v.vib.1 != 0 {
            pitch += (v.vib_phase * std::f64::consts::TAU).sin() * v.vib.1 as f64 / 16.0;
        }
        let rate = note_hz(pitch) * if inst.kind == "noise" { 8.0 } else { 1.0 } / sr;
        let mut amp = v.level * (inst.volume as f64 / 255.0) * v.vol * v.table_vol;
        if muted {
            amp = 0.0;
        }
        v.peak = (v.peak * 0.8).max(amp);
        let (from, ramp) = (v.amp, if amp != v.amp { n.min(RAMP) } else { 0 });
        v.amp = amp;
        let (left, right) = ((v.pan * std::f64::consts::FRAC_PI_2).cos(), (v.pan * std::f64::consts::FRAC_PI_2).sin());
        let kind = inst.kind.as_str();
        for (i, frame) in out.iter_mut().enumerate() {
            let phase = v.phase + rate * (i + 1) as f64;
            let frac = phase.fract();
            let sig = match kind {
                "pulse" => {
                    if frac < v.width {
                        1.0
                    } else {
                        -1.0
                    }
                }
                "tri" => 4.0 * (frac - 0.5).abs() - 1.0,
                "saw" => 2.0 * frac - 1.0,
                "wave" => v.wave[((frac * 32.0) as usize) % 32],
                _ => self.noise[(phase.floor() as usize) % 32768],
            };
            let gain = if i < ramp { from + (amp - from) * i as f64 / ramp as f64 } else { amp };
            frame[0] += sig * gain * left;
            frame[1] += sig * gain * right;
        }
        v.phase = (v.phase + rate * n as f64) % 32768.0;
    }

    // ── song walking ─────────────────────────────────────────────────────

    fn cell(&self, t: usize) -> Option<i64> {
        if self.mode == Some(Mode::Chain) && t == self.focus.0 {
            return Some(self.focus.1);
        }
        self.song.rows.get(self.tracks[t].row).and_then(|r| r[t])
    }

    fn block_start(&self, t: usize, row: usize) -> Option<usize> {
        let mut r = row.min(tk::ROWS - 1) as isize;
        while r >= 0 && self.song.rows[r as usize][t].is_none() {
            r -= 1;
        }
        if r < 0 {
            return None;
        }
        while r > 0 && self.song.rows[r as usize - 1][t].is_some() {
            r -= 1;
        }
        Some(r as usize)
    }

    fn chain_step(&self, t: usize) -> Option<tk::ChainStep> {
        if self.mode == Some(Mode::Phrase) {
            return Some(tk::ChainStep { phrase: Some(self.focus.1), transpose: 0 });
        }
        self.song.chains.get(&self.cell(t)?).map(|c| c[self.tracks[t].cstep].clone())
    }

    fn advance(&mut self, t: usize) {
        let tr = &mut self.tracks[t];
        tr.pstep += 1;
        if tr.pstep < tk::STEPS && !tr.hop {
            return;
        }
        (tr.pstep, tr.hop) = (0, false);
        if self.mode == Some(Mode::Phrase) {
            tr.looped = true; // came round once: a one-shot sound effect stops here
            return;
        }
        self.tracks[t].cstep += 1;
        let cstep = self.tracks[t].cstep;
        let has_next = self
            .cell(t)
            .and_then(|c| self.song.chains.get(&c))
            .is_some_and(|c| cstep < tk::STEPS && c[cstep].phrase.is_some());
        if has_next {
            return;
        }
        self.tracks[t].cstep = 0;
        if self.mode == Some(Mode::Chain) {
            return;
        }
        self.tracks[t].row += 1;
        let row = self.tracks[t].row;
        if row >= tk::ROWS || self.song.rows[row][t].is_none() {
            match self.block_start(t, row - 1) {
                None => self.tracks[t].running = false,
                Some(start) => (self.tracks[t].row, self.tracks[t].looped) = (start, true),
            }
        }
    }

    fn step(&mut self, t: usize) {
        let Some(cs) = self.chain_step(t) else { return };
        let Some(phrase) = cs.phrase.and_then(|p| self.song.phrases.get(&p)) else { return };
        let step = phrase[self.tracks[t].pstep].clone();
        let tr = tk::signed(cs.transpose);
        if step.cmd == "H" {
            self.tracks[t].hop = true;
        } else if step.cmd == "T" && step.val != 0 {
            self.bpm = step.val;
        }
        if let Some(note) = step.note {
            if note == tk::NOTE_OFF {
                self.release(t);
            } else if step.cmd == "D" && step.val != 0 {
                let v = &mut self.voices[t];
                (v.delay, v.pending) = (step.val, Some((note as i64 + tr, step.inst)));
            } else {
                let slide = if step.cmd == "L" { step.val } else { 0 };
                self.trigger(t, note as i64 + tr, step.inst, slide);
            }
        }
        if !step.cmd.is_empty() && !matches!(step.cmd.as_str(), "D" | "H" | "T" | "L") {
            self.command(t, &step.cmd, step.val);
        }
    }

    fn tick(&mut self) {
        for t in 0..self.tracks.len() {
            if !self.tracks[t].running {
                continue;
            }
            if self.tracks[t].ticks_left <= 0 {
                if !self.tracks[t].fresh {
                    self.advance(t);
                    if !self.tracks[t].running {
                        continue;
                    }
                }
                self.tracks[t].fresh = false;
                self.step(t);
                let groove = if self.song.groove.is_empty() { vec![6] } else { self.song.groove.clone() };
                let g = groove[self.tracks[t].groove_i % groove.len()].max(1);
                self.tracks[t].ticks_left = g;
                self.tracks[t].groove_i += 1;
            }
            self.tracks[t].ticks_left -= 1;
        }
        let due: Vec<usize> = self.preview_off.iter().filter(|(_, l)| **l <= 0).map(|(t, _)| *t).collect();
        for t in due {
            self.release(t);
            self.preview_off.remove(&t);
        }
        for left in self.preview_off.values_mut() {
            *left -= 1;
        }
        for t in 0..self.voices.len() {
            self.voice_tick(t);
        }
    }

    /// `frames` stereo samples, soft-limited (a full mix rounds off, no hard clip).
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

// ── live output ──────────────────────────────────────────────────────────

/// Anything Output can play: an Engine, or a mix of several (DreamTapes: music + sound effects).
pub trait Source: Send {
    fn sample_rate(&self) -> u32;
    /// `frames` stereo samples in -1..1.
    fn render(&mut self, frames: usize) -> Vec<[f32; 2]>;
}

impl Source for Engine {
    fn sample_rate(&self) -> u32 {
        self.sr
    }
    fn render(&mut self, frames: usize) -> Vec<[f32; 2]> {
        Engine::render(self, frames)
    }
}

/// Plays an Engine: a thread renders blocks and pipes them to `aplay` (the deck) or `pw-cat`
/// (PipeWire). The pipe is kept small, so a note you play is heard within a few blocks.
pub struct Output {
    pub how: String,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl Output {
    pub fn start<S: Source + 'static>(engine: Arc<Mutex<S>>, block: usize) -> Self {
        let sr = engine.lock().map(|e| e.sample_rate()).unwrap_or(44100);
        let players: [(&str, Vec<String>); 2] = [
            (
                "aplay",
                vec![
                    "-q".into(),
                    "-f".into(),
                    "S16_LE".into(),
                    "-r".into(),
                    sr.to_string(),
                    "-c".into(),
                    "2".into(),
                    "--buffer-time=80000".into(),
                    "-".into(),
                ],
            ),
            (
                "pw-cat",
                vec![
                    "--playback".into(),
                    "--format".into(),
                    "s16".into(),
                    "--rate".into(),
                    sr.to_string(),
                    "--channels".into(),
                    "2".into(),
                    "--latency".into(),
                    format!("{block}"),
                    "-".into(),
                ],
            ),
        ];
        let Some((name, args)) = players.into_iter().find(|(n, _)| crate::have(n)) else {
            return Output { how: "none".into(), stop: Arc::new(AtomicBool::new(true)), thread: None };
        };
        let child = std::process::Command::new(name)
            .args(&args)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn();
        let Ok(mut child) = child else {
            return Output { how: "none".into(), stop: Arc::new(AtomicBool::new(true)), thread: None };
        };
        let mut stdin = child.stdin.take().expect("piped");
        {
            use std::os::fd::AsRawFd;
            // A small pipe: the latency a note has is the pipe plus the player's buffer.
            // SAFETY: fcntl on our own pipe's file descriptor.
            unsafe {
                libc::fcntl(stdin.as_raw_fd(), libc::F_SETPIPE_SZ, 4096);
            }
        }
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = std::thread::spawn(move || {
            let mut bytes = Vec::with_capacity(block * 4);
            while !flag.load(Ordering::Relaxed) {
                let audio = match engine.lock() {
                    Ok(mut e) => e.render(block),
                    Err(_) => break,
                };
                bytes.clear();
                for [l, r] in audio {
                    bytes.extend_from_slice(&((l * 32767.0) as i16).to_le_bytes());
                    bytes.extend_from_slice(&((r * 32767.0) as i16).to_le_bytes());
                }
                // Blocks while the player is full: that's what paces the engine.
                if stdin.write_all(&bytes).is_err() {
                    break;
                }
            }
            drop(stdin);
            let _ = child.kill();
            let _ = child.wait();
        });
        Output { how: name.to_string(), stop, thread: Some(thread) }
    }
}

impl Drop for Output {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

// ── offline ──────────────────────────────────────────────────────────────

fn run_offline(song: &Song, sr: u32, max_seconds: f64, events: bool) -> (Engine, Vec<[f32; 2]>) {
    let mut e = Engine::new(song.clone(), sr, Midi::open(None));
    if events {
        e.events = Some(vec![]);
    }
    e.play_song(0);
    let limit = (max_seconds * sr as f64) as u64;
    let mut audio = vec![];
    while e.samples < limit {
        audio.extend(e.render(1024));
        if e.tracks.iter().any(|t| t.looped) || !e.tracks.iter().any(|t| t.running) {
            break;
        }
    }
    e.stop_all();
    for _ in 0..(sr as usize / 1024) {
        audio.extend(e.render(1024)); // a second for release tails
    }
    (e, audio)
}

/// The song once through (until a track loops) as 16-bit stereo. Returns seconds.
pub fn export_wav(song: &Song, path: &Path, sr: u32) -> Result<f64> {
    let (_, audio) = run_offline(song, sr, 600.0, false);
    let data_len = (audio.len() * 4) as u32;
    let mut out = Vec::with_capacity(44 + data_len as usize);
    out.extend_from_slice(b"RIFF");
    out.extend_from_slice(&(36 + data_len).to_le_bytes());
    out.extend_from_slice(b"WAVEfmt ");
    out.extend_from_slice(&16u32.to_le_bytes());
    out.extend_from_slice(&1u16.to_le_bytes()); // PCM
    out.extend_from_slice(&2u16.to_le_bytes());
    out.extend_from_slice(&sr.to_le_bytes());
    out.extend_from_slice(&(sr * 4).to_le_bytes());
    out.extend_from_slice(&4u16.to_le_bytes());
    out.extend_from_slice(&16u16.to_le_bytes());
    out.extend_from_slice(b"data");
    out.extend_from_slice(&data_len.to_le_bytes());
    for [l, r] in &audio {
        out.extend_from_slice(&((l * 32767.0) as i16).to_le_bytes());
        out.extend_from_slice(&((r * 32767.0) as i16).to_le_bytes());
    }
    std::fs::write(path, out)?;
    Ok(audio.len() as f64 / sr as f64)
}

fn vlq(mut n: u64) -> Vec<u8> {
    let mut out = vec![(n & 0x7F) as u8];
    while n > 0x7F {
        n >>= 7;
        out.push(((n & 0x7F) | 0x80) as u8);
    }
    out.reverse();
    out
}

fn chunk(mut evs: Vec<(u64, Vec<u8>)>) -> Vec<u8> {
    evs.sort_by_key(|(w, _)| *w);
    let mut body = vec![];
    let mut last = 0;
    for (when, data) in evs {
        body.extend(vlq(when - last));
        body.extend(data);
        last = when;
    }
    body.extend([0x00, 0xFF, 0x2F, 0x00]);
    let mut out = b"MTrk".to_vec();
    out.extend((body.len() as u32).to_be_bytes());
    out.extend(body);
    out
}

/// The song once through as a type-1 .mid, one MIDI track per tracker track. Returns notes.
pub fn export_midi(song: &Song, path: &Path, sr: u32) -> Result<usize> {
    let (e, _) = run_offline(song, sr, 600.0, true);
    let ppq = 480.0;
    let per_sample = song.bpm as f64 / 60.0 * ppq / sr as f64;
    let mut tracks: std::collections::BTreeMap<usize, Vec<(u64, Vec<u8>)>> = Default::default();
    let mut held: HashMap<usize, (u8, i64)> = HashMap::new();
    let mut notes = 0;
    for (samples, on, track, note, inst) in e.events.clone().unwrap_or_default() {
        let midi_inst = inst.as_ref().is_some_and(|i| i.kind == "midi" || i.out != "synth");
        let ch = if midi_inst {
            inst.as_ref().map_or(1, |i| i.midi_channel.clamp(1, 16))
        } else if track + 1 != 10 {
            track as i64 + 1
        } else {
            11
        } - 1;
        let when = (samples as f64 * per_sample) as u64;
        if !on && !held.contains_key(&track) {
            continue;
        }
        let evs = tracks.entry(track).or_default();
        if let Some((pch, pnote)) = held.remove(&track) {
            evs.push((when, vec![0x80 | pch, (pnote & 127) as u8, 0]));
        }
        if on {
            let vel = (inst.as_ref().map_or(192, |i| i.volume) / 2).max(1) as u8;
            evs.push((when, vec![0x90 | ch as u8, (note & 127) as u8, vel]));
            held.insert(track, (ch as u8, note));
            notes += 1;
        }
    }
    let end = (e.samples as f64 * per_sample) as u64;
    for (track, (ch, note)) in held {
        tracks.entry(track).or_default().push((end, vec![0x80 | ch, (note & 127) as u8, 0]));
    }
    let tempo = (60_000_000 / song.bpm.max(1)) as u32;
    let title = song.title.as_bytes();
    let mut meta_title = vec![0xFF, 0x03];
    meta_title.extend(vlq(title.len() as u64));
    meta_title.extend(title);
    let meta = vec![(0, vec![0xFF, 0x51, 0x03, (tempo >> 16) as u8, (tempo >> 8) as u8, tempo as u8]), (0, meta_title)];
    let mut file = b"MThd".to_vec();
    file.extend(6u32.to_be_bytes());
    file.extend(1u16.to_be_bytes());
    file.extend(((tracks.len() + 1) as u16).to_be_bytes());
    file.extend((ppq as u16).to_be_bytes());
    file.extend(chunk(meta));
    for (_, evs) in tracks {
        file.extend(chunk(evs));
    }
    std::fs::write(path, file)?;
    Ok(notes)
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
