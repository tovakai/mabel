//! CREATIVE ▸ TRACKER: LSDJ's song → chain → phrase, all on one screen. The model is tracker.rs,
//! the sound tracksynth.rs.
//!
//! SONG (rows × tracks of chains) · CHAIN (16 phrases + transpose) · PHRASE (16 steps: note,
//! instrument, command) · INSTRUMENT · TABLE · WAVE. Each panel follows the cursor of the one
//! before it, so there is no menu to dive through: move in SONG and you see that chain, its
//! phrase, the instrument under the cursor and its table, all at once.
//!
//! Keys (in any panel): hjkl move · H/L (shift+←/→) next panel · enter dive in · 0-9 a-f type hex ·
//! +/- nudge · </> ±16 (an octave on notes) · n new chain/phrase · N clone · x clear · y/p copy/paste
//! a cell · Y/P a whole chain/phrase/table · space play/stop what you're looking at · m mute · s solo ·
//! u undo · ctrl+r redo · ctrl+s save · : command. In PHRASE, i = piano mode (z s x d c… / q 2 w 3 e…,
//! +/- octave, = note off, esc back). In CMD columns type the letter (C D E H K L O P R T V W A).

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::Frame;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Wrap};

use crate::channel::{Channel, Ctx, Outcome, View};
use crate::keys;
use crate::palette::Palette;
use crate::tracker::{self as tk, ChainStep, Instrument, Song, Step, TableRow};
use crate::tracksynth::{self as ts, Engine, Midi, Mode, Output};
use crate::ui::{self, bold};
use crate::widgets::input::{Input, InputResult};

const ID: &str = "tracker";
const SONG: usize = 0;
const CHAIN: usize = 1;
const PHRASE: usize = 2;
const INST: usize = 3;
const TABLE: usize = 4;
const WAVE: usize = 5;
const PANELS: usize = 6;
/// Piano: C up to E of the next octave, and one octave higher.
const LOWER: &str = "zsxdcvgbhnjm,l.;/";
const UPPER: &str = "q2w3er5t6y7ui9o0p";
/// Close audio + MIDI after this long without sound.
const IDLE_RELEASE: Duration = Duration::from_secs(30);
const FRAME: Duration = Duration::from_millis(66);
const BLOCK: usize = 512;
const PIANO_HINT: &str = "piano: z s x d c… / q 2 w 3 e…  +/- octave  = off  esc back";
const INST_FIELDS: [(&str, &str); 13] = [
    ("kind", "kind"),
    ("volume", "volume"),
    ("attack", "attack"),
    ("decay", "decay"),
    ("sustain", "sustain"),
    ("release", "release"),
    ("width", "width/wave"),
    ("transpose", "transpose"),
    ("table", "table"),
    ("pan", "pan"),
    ("out", "out"),
    ("midi_channel", "midi ch"),
    ("midi_program", "program"),
];

/// A cell's value: a (maybe empty) number or a word (kind, out, cmd).
#[derive(Clone, Debug, PartialEq)]
enum V {
    N(Option<i64>),
    T(String),
}

impl V {
    fn num(&self) -> Option<i64> {
        match self {
            V::N(n) => *n,
            V::T(_) => None,
        }
    }

    fn text(self) -> String {
        match self {
            V::T(t) => t,
            V::N(_) => String::new(),
        }
    }
}

/// What the cursor edits.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Target {
    Song(usize, usize),
    /// chain, step, column (phrase, transpose)
    Chain(i64, usize, usize),
    /// phrase, step, column (note, inst, cmd, val)
    Phrase(i64, usize, usize),
    /// instrument, field
    Inst(i64, usize),
    /// table, row, column (vol, transpose, cmd, val)
    Table(i64, usize, usize),
    Wave(i64, usize),
}

fn attr(t: Target) -> &'static str {
    match t {
        Target::Song(..) => "song",
        Target::Chain(_, _, c) => ["phrase", "transpose"][c.min(1)],
        Target::Phrase(_, _, c) => ["note", "inst", "cmd", "val"][c.min(3)],
        Target::Inst(_, f) => INST_FIELDS[f].0,
        Target::Table(_, _, c) => ["vol", "transpose", "cmd", "val"][c.min(3)],
        Target::Wave(..) => "wave",
    }
}

fn inst_get(i: &Instrument, field: &str) -> V {
    match field {
        "kind" => V::T(i.kind.clone()),
        "out" => V::T(i.out.clone()),
        "table" => V::N(i.table),
        "midi_program" => V::N(i.midi_program),
        f => V::N(Some(match f {
            "volume" => i.volume,
            "attack" => i.attack,
            "decay" => i.decay,
            "sustain" => i.sustain,
            "release" => i.release,
            "width" => i.width,
            "transpose" => i.transpose,
            "pan" => i.pan,
            "midi_channel" => i.midi_channel,
            _ => 0,
        })),
    }
}

fn inst_set(i: &mut Instrument, field: &str, v: V) {
    let n = v.num();
    match field {
        "kind" => i.kind = v.text(),
        "out" => i.out = v.text(),
        "table" => i.table = n,
        "midi_program" => i.midi_program = n,
        "volume" => i.volume = n.unwrap_or(0),
        "attack" => i.attack = n.unwrap_or(0),
        "decay" => i.decay = n.unwrap_or(0),
        "sustain" => i.sustain = n.unwrap_or(0),
        "release" => i.release = n.unwrap_or(0),
        "width" => i.width = n.unwrap_or(0),
        "transpose" => i.transpose = n.unwrap_or(0),
        "pan" => i.pan = n.unwrap_or(0),
        "midi_channel" => i.midi_channel = n.unwrap_or(1),
        _ => {}
    }
}

fn inst_value(i: &Instrument, field: &str) -> String {
    match field {
        "kind" => i.kind.clone(),
        "out" => i.out.clone(),
        "midi_channel" => format!("{:02}", i.midi_channel),
        "transpose" => format!("{} ({:+})", tk::hx(Some(i.transpose)), tk::signed(i.transpose)),
        "pan" => match i.pan {
            0x80 => "80 centre".into(),
            0 => "00 left".into(),
            0xFF => "FF right".into(),
            v => tk::hx(Some(v)),
        },
        f => tk::hx(inst_get(i, f).num()),
    }
}

/// y/p: one cell, or a whole phrase step / table row.
#[derive(Clone)]
enum Clip {
    Val(&'static str, V),
    Step(Step),
    Row(TableRow),
}

/// Y/P: a whole chain, phrase or table.
#[derive(Clone)]
enum Block {
    Chain(Vec<ChainStep>),
    Phrase(Vec<Step>),
    Table(Vec<TableRow>),
}

fn kind_name(kind: usize) -> &'static str {
    ["song", "chain", "phrase", "instrument", "table", "wave"][kind.min(5)]
}

/// The browser's preview of a song: its facts and the top of the song grid.
pub fn song_card(path: &Path, _width: u16, height: u16, pal: &Palette) -> Vec<Line<'static>> {
    let song = match tk::load(path) {
        Ok(s) => s,
        Err(e) => return vec![Line::raw(""), Line::styled(format!(" ✖ {e}"), Style::new().fg(pal.red))],
    };
    let last = song.last_row();
    let mut steps = 0;
    for t in 0..song.tracks {
        let (mut n, mut r) = (0, 0);
        while last.is_some_and(|l| r <= l) && song.rows[r][t].is_some() {
            let id = song.rows[r][t].unwrap_or(0);
            n += song.chains.get(&id).map_or(0, |c| c.iter().filter(|s| s.phrase.is_some()).count()) * tk::STEPS;
            r += 1;
        }
        steps = steps.max(n);
    }
    let groove = if song.groove.is_empty() { vec![6] } else { song.groove.clone() };
    let per_step = groove.iter().sum::<i64>() as f64 / groove.len() as f64;
    let seconds = steps as f64 * per_step * 60.0 / (song.bpm.max(1) as f64 * ts::TICKS_PER_BEAT);
    let chains = song.chains.values().filter(|c| c.iter().any(|s| s.phrase.is_some())).count();
    let phrases = song.phrases.values().filter(|p| p.iter().any(|s| s.note.is_some())).count();
    let amber = Style::new().fg(pal.amber);
    let groove_s = groove.iter().map(|g| g.to_string()).collect::<Vec<_>>().join("/");
    let mut out = vec![
        Line::raw(""),
        Line::styled(format!(" {}", song.title), bold(pal.amber_hi)),
        Line::styled(format!(" {} bpm · groove {groove_s} · {} tracks", song.bpm, song.tracks), amber),
        Line::styled(format!(" {chains} chains · {phrases} phrases · {} instruments", song.instruments.len()), amber),
        Line::styled(
            format!(" about {}:{:02} once through", (seconds / 60.0) as i64, (seconds % 60.0) as i64),
            Style::new().fg(pal.teal),
        ),
        Line::raw(""),
    ];
    let shown = last.map_or(0, |l| l + 1).min((height as usize).saturating_sub(8).max(1));
    for (r, row) in song.rows.iter().enumerate().take(shown) {
        let cells = row[..song.tracks].iter().map(|c| tk::hx(*c)).collect::<Vec<_>>().join(" ");
        out.push(Line::from(vec![
            Span::styled(format!(" {r:02X} "), Style::new().fg(pal.amber_dim)),
            Span::styled(cells, amber),
        ]));
    }
    if last.is_none() {
        out.push(Line::styled(" (an empty song)", Style::new().fg(pal.amber_dim)));
    }
    out
}

/// Live audio: the engine, the thread that plays it, and when it last made a sound.
struct Audio {
    engine: Arc<Mutex<Engine>>,
    out: Output,
    last_sound: Instant,
}

/// One look at the engine per frame, so drawing doesn't hold its lock.
#[derive(Default)]
struct Snap {
    playing: bool,
    mode: Option<Mode>,
    focus: (usize, i64),
    bpm: i64,
    mutes: Vec<usize>,
    solo: Option<usize>,
    pos: Vec<Option<(usize, usize, usize)>>,
    peaks: Vec<f64>,
    how: Option<String>,
    midi: Option<String>,
}

pub struct TrackerView {
    pub song: Song,
    undo_stack: Vec<Song>,
    redo_stack: Vec<Song>,
    cur: [[usize; 2]; PANELS],
    song_top: usize,
    /// The panel space plays from (the one you're in, or last were).
    panel: usize,
    insert: bool,
    octave: i64,
    last_inst: i64,
    clip: Option<Clip>,
    block: Option<Block>,
    message: String,
    pub dirty: bool,
    audio: Option<Audio>,
    /// Hex entry: (panel, row, col, when): a second digit within 2 s makes a byte.
    typed: Option<(usize, usize, usize, Instant)>,
    cmdline: Option<Input>,
    browser: Option<crate::browser::Browser>,
    dir: PathBuf,
    rate: u32,
    inner: [Rect; PANELS],
    loaded_last: bool,
}

impl TrackerView {
    pub fn new(cfg: &crate::config::Config) -> Self {
        TrackerView {
            song: tk::demo(),
            undo_stack: vec![],
            redo_stack: vec![],
            cur: [[0, 0]; PANELS],
            song_top: 0,
            panel: SONG,
            insert: false,
            octave: 4,
            last_inst: 0,
            clip: None,
            block: None,
            message: "space plays · ? lists the keys · :help the commands".into(),
            dirty: false,
            audio: None,
            typed: None,
            cmdline: None,
            browser: None,
            dir: crate::config::expand(&cfg.tracker_dir),
            rate: if cfg.tracker_rate > 0 { cfg.tracker_rate as u32 } else { 44100 },
            inner: [Rect::default(); PANELS],
            loaded_last: false,
        }
    }

    fn last_file() -> PathBuf {
        crate::config::cache_dir().join("tracker-last")
    }

    fn remember(&self) {
        if let Some(p) = &self.song.path {
            let _ = std::fs::create_dir_all(crate::config::cache_dir());
            let _ = std::fs::write(Self::last_file(), p.display().to_string());
        }
    }

    fn path(&self, name: &str, suffix: &str) -> PathBuf {
        let p = crate::config::expand(name);
        let mut p = if p.is_absolute() { p } else { self.dir.join(p) };
        if !suffix.is_empty() && p.extension().is_none() {
            p.set_extension(suffix);
        }
        p
    }

    // ── context: each panel follows the one before it ────────────────────

    fn track(&self) -> usize {
        self.cur[SONG][1].min(self.song.tracks.saturating_sub(1))
    }

    fn chain_id(&self) -> Option<i64> {
        self.song.rows[self.cur[SONG][0]][self.track()]
    }

    fn phrase_id(&self) -> Option<i64> {
        let chain = self.song.chains.get(&self.chain_id()?)?;
        chain.get(self.cur[CHAIN][0])?.phrase
    }

    fn inst_id(&self) -> i64 {
        self.phrase_id()
            .and_then(|p| self.song.phrases.get(&p))
            .and_then(|p| p.get(self.cur[PHRASE][0]))
            .and_then(|s| s.inst)
            .unwrap_or(self.last_inst)
    }

    fn table_id(&self) -> Option<i64> {
        self.song.instruments.get(&self.inst_id()).map_or(Instrument::default().table, |i| i.table)
    }

    fn wave_id(&self) -> i64 {
        self.song.instruments.get(&self.inst_id()).filter(|i| i.kind == "wave").map_or(0, |i| i.width)
    }

    // ── audio ────────────────────────────────────────────────────────────

    fn ensure_audio(&mut self) {
        if cfg!(test) {
            return; // tests never make a sound
        }
        if self.audio.is_none() {
            let midi = Midi::open(Some(&self.song.midi_port));
            let engine = Arc::new(Mutex::new(Engine::new(self.song.clone(), self.rate, midi)));
            let out = Output::start(engine.clone(), BLOCK);
            if out.how == "none" {
                self.message = "no audio output (install alsa-utils or pipewire); MIDI still works".into();
            }
            self.audio = Some(Audio { engine, out, last_sound: Instant::now() });
        }
        if let Some(a) = &mut self.audio {
            a.last_sound = Instant::now();
        }
        self.sync();
    }

    fn with_engine<T>(&self, f: impl FnOnce(&mut Engine) -> T) -> Option<T> {
        let a = self.audio.as_ref()?;
        let mut e = a.engine.lock().ok()?;
        Some(f(&mut e))
    }

    fn sync(&self) {
        let song = &self.song;
        self.with_engine(|e| e.song = song.clone());
    }

    fn playing(&self) -> bool {
        self.with_engine(|e| e.playing()).unwrap_or(false)
    }

    fn snap(&self) -> Snap {
        let how = self.audio.as_ref().map(|a| a.out.how.clone());
        self.with_engine(|e| Snap {
            playing: e.playing(),
            mode: e.mode,
            focus: e.focus,
            bpm: e.bpm,
            mutes: e.mutes.iter().copied().collect(),
            solo: e.solo,
            pos: (0..tk::MAX_TRACKS).map(|t| if e.playing() { e.position(t) } else { None }).collect(),
            peaks: (0..tk::MAX_TRACKS).map(|t| e.peak(t)).collect(),
            how: how.clone(),
            midi: e.midi.connected().then(|| e.midi.port.clone()),
        })
        .unwrap_or_default()
    }

    fn play(&mut self) {
        self.ensure_audio();
        let (panel, row, track, chain, phrase) =
            (self.panel, self.cur[SONG][0], self.track(), self.chain_id(), self.phrase_id());
        let stopped = self
            .with_engine(|e| {
                if e.playing() {
                    e.stop_all();
                    return true;
                }
                match (panel, chain, phrase) {
                    (SONG, ..) => e.play_song(row),
                    (CHAIN, Some(c), _) => e.play_chain(track, c),
                    (_, _, Some(p)) => e.play_phrase(track, p),
                    _ => e.play_song(row),
                }
                false
            })
            .unwrap_or(false);
        if stopped {
            self.message.clear();
        }
    }

    fn preview(&mut self, note: Option<i64>, inst: Option<i64>) {
        let Some(note) = note else { return };
        self.ensure_audio();
        let track = self.track();
        self.with_engine(|e| {
            if !e.playing() {
                e.preview(track, note as i32, inst);
            }
        });
    }

    // ── editing ──────────────────────────────────────────────────────────

    fn push(&mut self) {
        self.undo_stack.push(self.song.clone());
        if self.undo_stack.len() > 100 {
            self.undo_stack.remove(0);
        }
        self.redo_stack.clear();
        self.dirty = true;
    }

    fn undo(&mut self, back: bool) {
        let (src, dst) = if back {
            (&mut self.undo_stack, &mut self.redo_stack)
        } else {
            (&mut self.redo_stack, &mut self.undo_stack)
        };
        let Some(song) = src.pop() else {
            self.message = format!("nothing to {}", if back { "undo" } else { "redo" });
            return;
        };
        dst.push(self.song.clone());
        let path = self.song.path.take();
        self.song = song;
        self.song.path = path;
        self.message = if back { "undone" } else { "redone" }.into();
        self.dirty = true;
    }

    fn cols(&self, kind: usize) -> usize {
        [self.song.tracks, 2, 4, 1, 4, 32][kind]
    }

    fn rows(&self, kind: usize) -> usize {
        match kind {
            SONG => tk::ROWS,
            INST => INST_FIELDS.len(),
            WAVE => 1,
            _ => tk::STEPS,
        }
    }

    fn movement(&mut self, kind: usize, dr: i64, dc: i64) {
        let [r, c] = self.cur[kind];
        let clamp = |v: usize, d: i64, n: usize| (v as i64 + d).clamp(0, n.saturating_sub(1) as i64) as usize;
        self.cur[kind] = [clamp(r, dr, self.rows(kind)), clamp(c, dc, self.cols(kind))];
        self.typed = None;
    }

    fn target(&self, kind: usize) -> Option<Target> {
        let [r, c] = self.cur[kind];
        Some(match kind {
            SONG => Target::Song(r, c.min(tk::MAX_TRACKS - 1)),
            CHAIN => Target::Chain(self.chain_id()?, r, c),
            PHRASE => Target::Phrase(self.phrase_id()?, r, c),
            INST => Target::Inst(self.inst_id(), r),
            TABLE => Target::Table(self.table_id()?, r, c),
            _ => Target::Wave(self.wave_id(), c),
        })
    }

    fn get(&self, t: Target) -> V {
        let s = &self.song;
        match t {
            Target::Song(r, c) => V::N(s.rows[r][c]),
            Target::Chain(id, r, c) => {
                let st = s.chains.get(&id).and_then(|ch| ch.get(r)).cloned().unwrap_or_default();
                V::N(if c == 0 { st.phrase } else { Some(st.transpose) })
            }
            Target::Phrase(id, r, c) => {
                let st = s.phrases.get(&id).and_then(|p| p.get(r)).cloned().unwrap_or_default();
                match c {
                    0 => V::N(st.note.map(i64::from)),
                    1 => V::N(st.inst),
                    2 => V::T(st.cmd),
                    _ => V::N(Some(st.val)),
                }
            }
            Target::Inst(id, f) => inst_get(s.instruments.get(&id).unwrap_or(&Instrument::default()), INST_FIELDS[f].0),
            Target::Table(id, r, c) => {
                let row = s.tables.get(&id).and_then(|t| t.get(r)).cloned().unwrap_or_default();
                match c {
                    0 => V::N(row.vol),
                    1 => V::N(Some(row.transpose)),
                    2 => V::T(row.cmd),
                    _ => V::N(Some(row.val)),
                }
            }
            Target::Wave(id, i) => V::N(Some(s.waves.get(&id).and_then(|w| w.get(i)).copied().unwrap_or(8))),
        }
    }

    fn set(&mut self, t: Target, v: V) {
        let n = v.num();
        match t {
            Target::Song(r, c) => self.song.rows[r][c] = n,
            Target::Chain(id, r, c) => {
                let st = &mut self.song.chain(id)[r];
                if c == 0 {
                    st.phrase = n;
                } else {
                    st.transpose = n.unwrap_or(0);
                }
            }
            Target::Phrase(id, r, c) => {
                let st = &mut self.song.phrase(id)[r];
                match c {
                    0 => st.note = n.map(|x| x as i32),
                    1 => st.inst = n,
                    2 => st.cmd = v.text(),
                    _ => st.val = n.unwrap_or(0),
                }
            }
            Target::Inst(id, f) => inst_set(self.song.instrument(id), INST_FIELDS[f].0, v),
            Target::Table(id, r, c) => {
                let row = &mut self.song.table(id)[r];
                match c {
                    0 => row.vol = n,
                    1 => row.transpose = n.unwrap_or(0),
                    2 => row.cmd = v.text(),
                    _ => row.val = n.unwrap_or(0),
                }
            }
            Target::Wave(id, i) => {
                if let Some(s) = self.song.wave(id).get_mut(i) {
                    *s = n.unwrap_or(8);
                }
            }
        }
    }

    fn nudge(&mut self, kind: usize, step: i64) {
        let Some(t) = self.target(kind) else {
            self.message = "nothing here yet: n makes one".into();
            return;
        };
        let v = self.get(t);
        let a = attr(t);
        let dir = if step > 0 { 1 } else { -1 };
        self.push();
        match a {
            "note" => {
                let Target::Phrase(pid, r, _) = t else { return };
                let base = v.num().filter(|n| *n != tk::NOTE_OFF as i64).unwrap_or(60);
                let d = if step.abs() == 16 { 12 * dir } else { step };
                let new = (base + d).clamp(0, 127);
                let last = self.last_inst;
                let st = &mut self.song.phrase(pid)[r];
                st.note = Some(new as i32);
                let inst = *st.inst.get_or_insert(last);
                self.preview(Some(new), Some(inst));
            }
            "kind" | "out" => {
                let choices: &[&str] = if a == "kind" { &tk::KINDS } else { &tk::OUTS };
                let cur = v.text();
                let i = choices.iter().position(|c| *c == cur).unwrap_or(0) as i64;
                let next = choices[(i + dir).rem_euclid(choices.len() as i64) as usize];
                self.set(t, V::T(next.into()));
            }
            "cmd" => {
                let cur = v.text();
                let i = tk::COMMANDS.iter().position(|(c, _)| *c == cur).map_or(-1, |i| i as i64);
                let next = tk::COMMANDS[(i + dir).rem_euclid(tk::COMMANDS.len() as i64) as usize].0;
                self.set(t, V::T(next.into()));
            }
            "wave" => {
                let d = dir * if step.abs() == 16 { 4 } else { 1 };
                self.set(t, V::N(Some((v.num().unwrap_or(0) + d).clamp(0, 15))));
            }
            "midi_channel" => self.set(t, V::N(Some((v.num().unwrap_or(1) + dir).clamp(1, 16)))),
            _ => {
                let new = (v.num().unwrap_or(0) + step) & 0xFF;
                self.set(t, V::N(Some(new)));
                if a == "inst" {
                    self.last_inst = new;
                }
            }
        }
    }

    fn type_hex(&mut self, kind: usize, digit: i64) {
        let Some(t) = self.target(kind) else {
            self.message = "nothing here yet: n makes one".into();
            return;
        };
        let a = attr(t);
        if matches!(a, "note" | "kind" | "out" | "cmd") {
            return;
        }
        if kind == WAVE {
            self.push();
            self.set(t, V::N(Some(digit)));
            self.movement(kind, 0, 1);
            return;
        }
        let now = Instant::now();
        let [r, c] = self.cur[kind];
        let old = self.get(t).num();
        let continuing = self
            .typed
            .is_some_and(|(k, tr, tc, when)| (k, tr, tc) == (kind, r, c) && now - when < Duration::from_secs(2));
        self.push();
        let value = if a == "midi_channel" {
            let v = if continuing { old.unwrap_or(0) * 10 + digit } else { digit };
            if (1..=16).contains(&v) { v } else { digit.max(1) }
        } else if continuing {
            ((old.unwrap_or(0) << 4) | digit) & 0xFF
        } else {
            digit
        };
        self.set(t, V::N(Some(value)));
        if a == "inst" {
            self.last_inst = value;
        }
        self.typed = Some((kind, r, c, now));
    }

    fn clear(&mut self, kind: usize) {
        let Some(t) = self.target(kind) else { return };
        let a = attr(t);
        if matches!(a, "kind" | "out" | "midi_channel") {
            return;
        }
        self.push();
        match (a, t) {
            ("wave", _) => self.set(t, V::N(Some(8))),
            ("transpose" | "val" | "volume" | "attack" | "decay" | "sustain" | "release" | "width" | "pan", _) => {
                self.set(t, V::N(Some(0)))
            }
            ("cmd", Target::Phrase(id, r, _)) => {
                let st = &mut self.song.phrase(id)[r];
                (st.cmd, st.val) = (String::new(), 0);
            }
            ("cmd", Target::Table(id, r, _)) => {
                let row = &mut self.song.table(id)[r];
                (row.cmd, row.val) = (String::new(), 0);
            }
            ("note", Target::Phrase(id, r, _)) => self.song.phrase(id)[r] = Step::default(),
            _ => self.set(t, V::N(None)),
        }
    }

    /// n / N: a fresh (or cloned) chain in SONG, phrase in CHAIN, instrument in PHRASE, table in INSTRUMENT.
    fn new_thing(&mut self, kind: usize, clone: bool) {
        match kind {
            SONG => {
                let [row, col] = self.cur[SONG];
                let cur = self.song.rows[row][col];
                self.push();
                let new = match cur {
                    Some(c) if clone => self.song.clone_chain(c),
                    _ => self.song.new_chain(),
                };
                let Some(new) = new else {
                    self.message = "all 256 chains are used".into();
                    return;
                };
                self.song.rows[row][col] = Some(new);
                self.message = match cur {
                    Some(c) if clone => format!("chain {new:02X} (a copy of {c:02X})"),
                    _ => format!("chain {new:02X}"),
                };
            }
            CHAIN => {
                let Some(cid) = self.chain_id() else {
                    self.message = "no chain here: n in SONG first".into();
                    return;
                };
                let r = self.cur[CHAIN][0];
                let cur = self.song.chain(cid)[r].phrase;
                self.push();
                let new = match cur {
                    Some(p) if clone => self.song.clone_phrase(p),
                    _ => self.song.new_phrase(),
                };
                let Some(new) = new else {
                    self.message = "all 256 phrases are used".into();
                    return;
                };
                self.song.chain(cid)[r].phrase = Some(new);
                self.message = format!("phrase {new:02X}");
            }
            PHRASE if self.cur[PHRASE][1] == 1 => {
                let Some(pid) = self.phrase_id() else { return };
                let Some(new) = (0..64).find(|i| !self.song.instruments.contains_key(i)) else { return };
                self.push();
                let src = self.song.instruments.get(&self.inst_id()).cloned();
                let inst = match src {
                    Some(s) if clone => s,
                    _ => Instrument { name: format!("inst{new:02X}"), ..Default::default() },
                };
                self.song.instruments.insert(new, inst);
                self.song.phrase(pid)[self.cur[PHRASE][0]].inst = Some(new);
                self.last_inst = new;
                self.message = format!("instrument {new:02X}");
            }
            INST if INST_FIELDS[self.cur[INST][0]].0 == "table" => {
                let mut used: std::collections::BTreeSet<i64> = self.song.tables.keys().copied().collect();
                used.extend(self.song.instruments.values().filter_map(|i| i.table));
                if let Some(new) = (0..32).find(|i| !used.contains(i)) {
                    self.push();
                    let id = self.inst_id();
                    self.song.instrument(id).table = Some(new);
                    self.song.table(new);
                    self.message = format!("table {new:02X}");
                }
            }
            _ => {}
        }
    }

    fn copy_cell(&mut self, kind: usize, whole: bool) {
        if whole {
            let block = match kind {
                CHAIN => self.chain_id().and_then(|i| self.song.chains.get(&i)).cloned().map(Block::Chain),
                PHRASE => self.phrase_id().and_then(|i| self.song.phrases.get(&i)).cloned().map(Block::Phrase),
                TABLE => self.table_id().and_then(|i| self.song.tables.get(&i)).cloned().map(Block::Table),
                _ => None,
            };
            match block {
                Some(b) => {
                    self.block = Some(b);
                    self.message = format!("copied the {}", kind_name(kind));
                }
                None => self.message = "Y copies a whole chain, phrase or table".into(),
            }
            return;
        }
        let Some(t) = self.target(kind) else { return };
        self.clip = Some(match t {
            Target::Phrase(id, r, 0) => {
                Clip::Step(self.song.phrases.get(&id).and_then(|p| p.get(r)).cloned().unwrap_or_default())
            }
            Target::Table(id, r, 0) => {
                Clip::Row(self.song.tables.get(&id).and_then(|p| p.get(r)).cloned().unwrap_or_default())
            }
            _ => Clip::Val(attr(t), self.get(t)),
        });
        self.message = "copied".into();
    }

    fn paste(&mut self, kind: usize, whole: bool) {
        if whole {
            let name = kind_name(kind);
            let fits = matches!(
                (&self.block, kind),
                (Some(Block::Chain(_)), CHAIN) | (Some(Block::Phrase(_)), PHRASE) | (Some(Block::Table(_)), TABLE)
            );
            if !fits {
                self.message = format!("no {name} copied (Y)");
                return;
            }
            let id = match kind {
                CHAIN => self.chain_id(),
                PHRASE => self.phrase_id(),
                _ => self.table_id(),
            };
            let Some(id) = id else {
                self.message = format!("no {name} here: n makes one");
                return;
            };
            self.push();
            match self.block.clone() {
                Some(Block::Chain(c)) => {
                    self.song.chains.insert(id, c);
                }
                Some(Block::Phrase(p)) => {
                    self.song.phrases.insert(id, p);
                }
                Some(Block::Table(t)) => {
                    self.song.tables.insert(id, t);
                }
                None => {}
            }
            self.message = format!("pasted into {name} {id:02X}");
            return;
        }
        let (Some(t), Some(clip)) = (self.target(kind), self.clip.clone()) else { return };
        match (clip, t) {
            (Clip::Step(s), Target::Phrase(id, r, _)) => {
                self.push();
                self.song.phrase(id)[r] = s;
            }
            (Clip::Row(row), Target::Table(id, r, _)) => {
                self.push();
                self.song.table(id)[r] = row;
            }
            (Clip::Val(what, v), _) if what == attr(t) => {
                self.push();
                self.set(t, v);
            }
            _ => {
                self.message = "that doesn't fit here".into();
                return;
            }
        }
        self.message = "pasted".into();
    }

    fn dive(&mut self, kind: usize, ctx: &mut Ctx) {
        let target = match kind {
            SONG => CHAIN,
            CHAIN => PHRASE,
            PHRASE if self.cur[PHRASE][1] == 1 => INST,
            PHRASE => {
                self.insert = !self.insert;
                self.message = if self.insert { PIANO_HINT.into() } else { String::new() };
                return;
            }
            INST => TABLE,
            TABLE => WAVE,
            _ => return,
        };
        self.focus(target, ctx);
    }

    fn focus(&mut self, kind: usize, ctx: &mut Ctx) {
        self.panel = kind;
        self.typed = None;
        ctx.focus(kind);
    }

    fn piano(&mut self, c: char) -> bool {
        let Some(pid) = self.phrase_id() else {
            self.message = "no phrase here: n in CHAIN makes one".into();
            return true;
        };
        let c = if c == 'ö' || c == 'Ö' { ';' } else { c }; // Swedish layout: ö sits where ; is (D#)
        let note = if let Some(i) = LOWER.find(c) {
            (self.octave + 1) * 12 + LOWER[..i].chars().count() as i64
        } else if let Some(i) = UPPER.find(c) {
            (self.octave + 2) * 12 + UPPER[..i].chars().count() as i64
        } else if c == '=' {
            tk::NOTE_OFF as i64
        } else {
            return false;
        };
        self.push();
        let row = self.cur[PHRASE][0];
        let last = self.last_inst;
        let st = &mut self.song.phrase(pid)[row];
        if note == tk::NOTE_OFF as i64 {
            st.note = Some(tk::NOTE_OFF);
        } else {
            st.note = Some(note.clamp(0, 127) as i32);
            st.inst.get_or_insert(last);
        }
        let (n, i) = (st.note.map(i64::from), st.inst);
        self.preview(n, i);
        self.cur[PHRASE][0] = (row + 1) % tk::STEPS;
        true
    }

    /// A key inside a panel; false = not ours (esc backs out, ? opens help, h j k l arrive as arrows).
    fn panel_key(&mut self, kind: usize, key: &KeyEvent, ctx: &mut Ctx) -> bool {
        self.panel = kind;
        let c = keys::ch(key);
        let code = key.code;
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        let arrow = matches!(code, KeyCode::Up | KeyCode::Down | KeyCode::Left | KeyCode::Right);
        if code == KeyCode::Esc {
            if !self.insert {
                return false;
            }
            self.insert = false;
            self.message.clear();
        } else if self.insert && kind == PHRASE && !arrow && !matches!(c, Some(' ' | ':')) {
            match (c, code) {
                (Some('+'), _) => self.octave = (self.octave + 1).min(8),
                (Some('-'), _) => self.octave = (self.octave - 1).max(0),
                (_, KeyCode::Backspace | KeyCode::Delete) => self.clear(PHRASE),
                (Some(ch), _) if self.piano(ch) => {}
                _ => return false,
            }
        } else if code == KeyCode::Up || c == Some('k') {
            if kind == WAVE { self.nudge(kind, 1) } else { self.movement(kind, -1, 0) }
        } else if code == KeyCode::Down || c == Some('j') {
            if kind == WAVE { self.nudge(kind, -1) } else { self.movement(kind, 1, 0) }
        } else if (code == KeyCode::Left && !shift) || c == Some('h') {
            if kind == INST { self.nudge(kind, -1) } else { self.movement(kind, 0, -1) }
        } else if (code == KeyCode::Right && !shift) || c == Some('l') {
            if kind == INST { self.nudge(kind, 1) } else { self.movement(kind, 0, 1) }
        } else if matches!(code, KeyCode::PageUp | KeyCode::PageDown) {
            self.movement(kind, if code == KeyCode::PageUp { -16 } else { 16 }, 0);
        } else if c == Some('H') || code == KeyCode::Left {
            self.focus((kind + PANELS - 1) % PANELS, ctx);
        } else if c == Some('L') || code == KeyCode::Right {
            self.focus((kind + 1) % PANELS, ctx);
        } else if code == KeyCode::Enter {
            self.dive(kind, ctx);
        } else if c == Some(' ') {
            self.play();
        } else if let Some(d @ ('+' | '-')) = c {
            self.nudge(kind, if d == '+' { 1 } else { -1 });
        } else if let Some(d @ ('<' | '>')) = c {
            self.nudge(kind, if d == '>' { 16 } else { -16 });
        } else if c == Some(':') {
            self.open_cmdline("");
        } else if let Some(letter) = c.filter(|ch| {
            matches!(kind, PHRASE | TABLE)
                && self.cur[kind][1] == 2
                && tk::command_help(&ch.to_ascii_uppercase().to_string()).is_some()
        }) {
            if let Some(t) = self.target(kind) {
                let cmd = letter.to_ascii_uppercase().to_string();
                self.push();
                self.set(t, V::T(cmd.clone()));
                self.message = format!("{cmd}: {}", tk::command_help(&cmd).unwrap_or(""));
            }
        } else if keys::ctrl(key, 's') {
            self.command("w", ctx);
        } else if keys::ctrl(key, 'o') {
            self.browse();
        } else if keys::ctrl(key, 'r') {
            self.undo(false);
        } else if c == Some('u') {
            self.undo(true);
        } else if matches!(c, Some('x')) || matches!(code, KeyCode::Delete | KeyCode::Backspace) {
            self.clear(kind);
        } else if let Some(n @ ('n' | 'N')) = c {
            self.new_thing(kind, n == 'N');
        } else if let Some(y @ ('y' | 'Y')) = c {
            self.copy_cell(kind, y == 'Y');
        } else if let Some(p @ ('p' | 'P')) = c {
            self.paste(kind, p == 'P');
        } else if c == Some('m') {
            self.ensure_audio();
            let track = self.track();
            self.with_engine(|e| e.toggle_mute(track));
        } else if c == Some('s') {
            self.ensure_audio();
            let track = self.track();
            self.with_engine(|e| e.solo = if e.solo == Some(track) { None } else { Some(track) });
        } else if c == Some('i') && kind == PHRASE {
            self.insert = true;
            self.message = PIANO_HINT.into();
        } else if let Some(d) = c.filter(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase()) {
            self.type_hex(kind, d.to_digit(16).unwrap_or(0) as i64);
        } else {
            return false;
        }
        self.sync();
        if kind == PHRASE
            && let Some(i) = self.target(PHRASE).and_then(|t| match t {
                Target::Phrase(id, r, _) => self.song.phrases.get(&id).and_then(|p| p.get(r)).and_then(|s| s.inst),
                _ => None,
            })
        {
            self.last_inst = i;
        }
        true
    }

    // ── : commands and files ─────────────────────────────────────────────

    fn open_cmdline(&mut self, prefill: &str) {
        let mut input = Input::new(": command   (:w song.mtk · :bpm 140 · :export wav · :help)");
        input.set(prefill);
        self.cmdline = Some(input);
    }

    fn browse(&mut self) {
        self.browser = Some(crate::browser::Browser::new(crate::browser::Spec {
            title: "TRACKER · SONGS".into(),
            root: self.dir.clone(),
            suffixes: vec!["mtk".into()],
            also_show: vec!["wav".into(), "mid".into()],
            document_dir: None,
            extras: vec![],
            new_suffix: "mtk".into(),
            dirty: self.dirty,
            current: self.song.path.clone(),
            preview: crate::browser::Preview::Song,
        }));
    }

    fn browsed(&mut self, choice: crate::browser::Choice) {
        let name = choice.path.file_name().map_or(String::new(), |n| n.to_string_lossy().into_owned());
        let result: anyhow::Result<String> = (|| {
            if choice.save_first {
                if self.song.path.is_none() {
                    return Ok("this song has no name yet: :w name.mtk first".into());
                }
                tk::save(&mut self.song, None)?;
            }
            if choice.new {
                let title = choice.path.file_stem().map_or("untitled".into(), |s| s.to_string_lossy().into_owned());
                let mut song = Song { title, tracks: self.song.tracks, ..Default::default() };
                tk::save(&mut song, Some(&choice.path))?;
                self.load(song);
                self.remember();
                return Ok(format!("new song {name}"));
            }
            self.load(tk::load(&choice.path)?);
            self.remember();
            Ok(format!("opened {name}"))
        })();
        self.message = result.unwrap_or_else(|e| format!("✖ {e}"));
        self.sync();
    }

    fn load(&mut self, song: Song) {
        self.with_engine(|e| e.stop_all());
        self.song = song;
        self.sync();
        self.undo_stack.clear();
        self.redo_stack.clear();
        self.cur = [[0, 0]; PANELS];
        (self.song_top, self.dirty, self.insert) = (0, false, false);
    }

    fn command(&mut self, line: &str, ctx: &mut Ctx) {
        let args: Vec<&str> = line.trim().trim_start_matches(':').split_whitespace().collect();
        self.message = match self.run(&args, ctx) {
            Ok(m) => m,
            Err(e) => format!("✖ {e}"),
        };
        self.sync();
    }

    fn run(&mut self, args: &[&str], ctx: &mut Ctx) -> anyhow::Result<String> {
        let Some((first, rest)) = args.split_first() else { return Ok(String::new()) };
        let force = first.ends_with('!');
        let cmd = first.trim_end_matches('!');
        let num = |i: usize| -> anyhow::Result<i64> {
            Ok(rest.get(i).ok_or_else(|| anyhow::anyhow!("a number is missing"))?.parse()?)
        };
        if matches!(cmd, "new" | "e" | "demo") && self.dirty && !force && !(cmd == "e" && rest.is_empty()) {
            return Ok(format!("unsaved changes: :w first, or :{cmd}! to drop them"));
        }
        Ok(match cmd {
            "w" => {
                let target = (!rest.is_empty()).then(|| self.path(&rest.join(" "), "mtk"));
                if target.is_none() && self.song.path.is_none() {
                    return Ok("name it: :w song.mtk".into());
                }
                let path = tk::save(&mut self.song, target.as_deref())?;
                self.dirty = false;
                self.remember();
                format!("saved {}", path.display())
            }
            "files" | "browse" | "e" if rest.is_empty() => {
                self.browse();
                String::new()
            }
            "e" => {
                let song = tk::load(&self.path(&rest.join(" "), "mtk"))?;
                self.load(song);
                self.remember();
                format!("opened {}", self.song.path.as_ref().map_or(String::new(), |p| p.display().to_string()))
            }
            "new" => {
                let tracks = if rest.is_empty() { 4 } else { num(0)?.clamp(1, tk::MAX_TRACKS as i64) as usize };
                self.load(Song { tracks, ..Default::default() });
                format!("a new song, {tracks} tracks")
            }
            "demo" => {
                self.load(tk::demo());
                "the demo song".into()
            }
            "bpm" => {
                let bpm = num(0)?.clamp(20, 400);
                self.push();
                self.song.bpm = bpm;
                self.with_engine(|e| e.bpm = bpm);
                format!("{bpm} bpm")
            }
            "groove" => {
                let groove: Vec<i64> =
                    rest.iter().map(|g| g.parse::<i64>().map(|g| g.clamp(1, 32))).collect::<Result<_, _>>()?;
                self.push();
                self.song.groove = if groove.is_empty() { vec![6, 6] } else { groove };
                format!("groove {}", self.song.groove.iter().map(|g| g.to_string()).collect::<Vec<_>>().join("/"))
            }
            "tracks" => {
                let n = num(0)?.clamp(1, tk::MAX_TRACKS as i64) as usize;
                self.push();
                self.song.tracks = n;
                format!("{n} tracks")
            }
            "title" => {
                self.push();
                if !rest.is_empty() {
                    self.song.title = rest.join(" ");
                }
                String::new()
            }
            "name" => {
                self.push();
                let name: String = rest.join(" ").chars().take(12).collect();
                let id = self.inst_id();
                self.song.instrument(id).name = if name.is_empty() { "inst".into() } else { name };
                String::new()
            }
            "wave" => {
                let shape: fn(f64) -> f64 = match rest.first().copied() {
                    Some("sine") => |i| 7.5 + 7.5 * (i / 32.0 * std::f64::consts::TAU).sin(),
                    Some("saw") => |i| i / 31.0 * 15.0,
                    Some("square") => |i| if i < 16.0 { 15.0 } else { 0.0 },
                    Some("tri") => |i| 15.0 - (i - 16.0).abs() * 15.0 / 16.0,
                    _ => return Ok("waves: sine saw square tri".into()),
                };
                self.push();
                let id = self.wave_id();
                self.song.waves.insert(id, (0..32).map(|i| shape(i as f64).round() as i64).collect());
                format!("wave {id:02X} = {}", rest[0])
            }
            "export" => {
                let how = rest.first().copied().filter(|h| matches!(*h, "wav" | "mid")).unwrap_or("wav");
                let stem = self
                    .song
                    .path
                    .as_ref()
                    .and_then(|p| p.file_stem())
                    .map_or(self.song.title.replace(' ', "-"), |s| s.to_string_lossy().into_owned());
                let name = rest.get(1).map_or(format!("{stem}.{how}"), |n| n.to_string());
                let path = self.path(&name, how);
                if let Some(dir) = path.parent() {
                    std::fs::create_dir_all(dir)?;
                }
                if how == "wav" {
                    let seconds = ts::export_wav(&self.song, &path, self.rate)?;
                    format!("exported {} ({seconds:.0} s)", path.display())
                } else {
                    let notes = ts::export_midi(&self.song, &path, self.rate)?;
                    format!("exported {} ({notes} notes)", path.display())
                }
            }
            "midi" => {
                let ports = ts::midi_ports();
                let Some(choice) = rest.first() else {
                    let now = if self.song.midi_port.is_empty() { "auto" } else { &self.song.midi_port };
                    let list: Vec<String> = ports.iter().enumerate().map(|(i, p)| format!("{i}:{p}")).collect();
                    return Ok(format!(
                        "MIDI {now} · ports: {}",
                        if list.is_empty() { "none".into() } else { list.join(" ") }
                    ));
                };
                self.push();
                self.song.midi_port = match choice.parse::<usize>() {
                    Ok(i) if i < ports.len() => ports[i].clone(),
                    _ if *choice == "auto" => String::new(),
                    _ => choice.to_string(),
                };
                let port = self.song.midi_port.clone();
                self.with_engine(|e| {
                    e.midi.all_off();
                    e.midi = Midi::open(Some(&port));
                });
                format!("MIDI {}", if port.is_empty() { "auto" } else { &port })
            }
            "help" | "h" => {
                let cmds: Vec<String> = tk::COMMANDS.iter().map(|(k, v)| format!("{k} {v}")).collect();
                ctx.toast(
                    "TRACKER",
                    &format!(
                        ":w [file] · :e file · :new [tracks] · :demo · :bpm N · :groove 6 6 · :tracks N\n\
                         :title … · :name … (instrument) · :wave sine|saw|square|tri · :midi [N|path|auto]\n\
                         :export wav|mid [file]\nCommands: {}",
                        cmds.join("  ")
                    ),
                );
                String::new()
            }
            _ => format!("unknown command :{cmd} (try :help)"),
        })
    }

    // ── drawing ──────────────────────────────────────────────────────────

    fn cell(text: String, cursor: bool, focused: bool, play: bool, dim: bool, pal: &Palette) -> Span<'static> {
        let style = if cursor && focused {
            Style::new().fg(pal.bg).bg(pal.amber_hi).add_modifier(Modifier::BOLD)
        } else if cursor {
            bold(pal.amber_hi).add_modifier(Modifier::UNDERLINED)
        } else if play {
            bold(pal.teal)
        } else {
            Style::new().fg(if dim { pal.amber_dim } else { pal.amber })
        };
        Span::styled(text, style)
    }

    fn song_lines(&mut self, focused: bool, height: usize, s: &Snap, pal: &Palette) -> Vec<Line<'static>> {
        let [r0, c0] = self.cur[SONG];
        let rows = height.saturating_sub(1).max(1);
        if r0 < self.song_top {
            self.song_top = r0;
        } else if r0 >= self.song_top + rows {
            self.song_top = r0 + 1 - rows;
        }
        let mut head = vec![Span::styled("  ", Style::new().fg(pal.amber_dim))];
        for tr in 0..self.song.tracks {
            let mark = if s.mutes.contains(&tr) {
                "M".to_string()
            } else if s.solo == Some(tr) {
                "S".to_string()
            } else {
                (tr + 1).to_string()
            };
            let style = if mark == "M" || mark == "S" { bold(pal.red) } else { bold(pal.beige) };
            head.push(Span::styled(format!("  {mark}"), style));
        }
        let mut out = vec![Line::from(head)];
        for (r, row) in self.song.rows.iter().enumerate().skip(self.song_top).take(rows) {
            let mut parts = vec![Span::styled(
                format!("{r:02X}"),
                Style::new().fg(if r % 4 == 0 { pal.amber_hi } else { pal.amber_dim }),
            )];
            for (tr, &v) in row[..self.song.tracks].iter().enumerate() {
                let on = s.pos.get(tr).copied().flatten().is_some_and(|p| p.0 == r);
                parts.push(Span::styled(if on { "▸" } else { " " }, bold(pal.teal)));
                parts.push(Self::cell(tk::hx(v), (r, tr) == (r0, c0), focused, on, v.is_none(), pal));
            }
            out.push(Line::from(parts));
        }
        out
    }

    fn chain_lines(&self, focused: bool, s: &Snap, pal: &Palette) -> Vec<Line<'static>> {
        let c = self.chain_id();
        let chain = c.and_then(|c| self.song.chains.get(&c));
        let [r0, c0] = self.cur[CHAIN];
        let track = self.track();
        let pos = s.pos.get(track).copied().flatten();
        let live = pos.filter(|p| match s.mode {
            Some(Mode::Song) => c.is_some() && self.song.rows[p.0][track] == c,
            Some(Mode::Chain) => c.is_some_and(|c| s.focus == (track, c)),
            _ => false,
        });
        let dim = Style::new().fg(pal.amber_dim);
        let mut out = vec![Line::styled("   PH TR", dim)];
        for i in 0..tk::STEPS {
            let st = chain.and_then(|ch| ch.get(i)).cloned().unwrap_or_default();
            let on = live.is_some_and(|p| p.1 == i);
            let tr = if st.phrase.is_some() || st.transpose != 0 { tk::hx(Some(st.transpose)) } else { "--".into() };
            out.push(Line::from(vec![
                Span::styled(format!("{i:X}"), dim),
                Span::styled(if on { "▸ " } else { "  " }, bold(pal.teal)),
                Self::cell(tk::hx(st.phrase), (i, 0) == (r0, c0), focused, on, st.phrase.is_none(), pal),
                Span::raw(" "),
                Self::cell(tr, (i, 1) == (r0, c0), focused, false, st.transpose == 0, pal),
            ]));
        }
        if c.is_none() {
            out.push(Line::raw(""));
            out.push(Line::styled(" n: a new chain here", dim));
        }
        out
    }

    fn phrase_lines(&self, focused: bool, s: &Snap, pal: &Palette) -> Vec<Line<'static>> {
        let p = self.phrase_id();
        let phrase = p.and_then(|p| self.song.phrases.get(&p));
        let [r0, c0] = self.cur[PHRASE];
        let track = self.track();
        let pos = s.pos.get(track).copied().flatten();
        let live = pos.is_some()
            && p.is_some()
            && match s.mode {
                Some(Mode::Phrase) => p.is_some_and(|p| s.focus == (track, p)),
                Some(m) => {
                    let cid = if m == Mode::Chain {
                        Some(s.focus.1)
                    } else {
                        pos.and_then(|pos| self.song.rows[pos.0][track])
                    };
                    cid.and_then(|c| self.song.chains.get(&c))
                        .and_then(|ch| ch.get(pos.map_or(0, |p| p.1)))
                        .is_some_and(|st| st.phrase == p)
                }
                None => false,
            };
        let dim = Style::new().fg(pal.amber_dim);
        let mut out = vec![Line::styled("   NOTE IN CMD", dim)];
        for i in 0..tk::STEPS {
            let st = phrase.and_then(|ph| ph.get(i)).cloned().unwrap_or_default();
            let on = live && pos.is_some_and(|p| p.2 == i);
            let has_cmd = !st.cmd.is_empty();
            out.push(Line::from(vec![
                Span::styled(format!("{i:X}"), Style::new().fg(if i % 4 == 0 { pal.amber_hi } else { pal.amber_dim })),
                Span::styled(if on { "▸ " } else { "  " }, bold(pal.teal)),
                Self::cell(tk::note_name(st.note), (i, 0) == (r0, c0), focused, on, st.note.is_none(), pal),
                Span::raw(" "),
                Self::cell(tk::hx(st.inst), (i, 1) == (r0, c0), focused, false, st.inst.is_none(), pal),
                Span::raw(" "),
                Self::cell(
                    if has_cmd { st.cmd.clone() } else { "-".into() },
                    (i, 2) == (r0, c0),
                    focused,
                    false,
                    !has_cmd,
                    pal,
                ),
                Self::cell(
                    if has_cmd { tk::hx(Some(st.val)) } else { "--".into() },
                    (i, 3) == (r0, c0),
                    focused,
                    false,
                    !has_cmd,
                    pal,
                ),
            ]));
        }
        if p.is_none() {
            out.push(Line::raw(""));
            out.push(Line::styled(" n in CHAIN: a new phrase", dim));
        } else if focused && c0 == 2 {
            let cmd = phrase.and_then(|ph| ph.get(r0)).map_or(String::new(), |s| s.cmd.clone());
            let text = match tk::command_help(&cmd) {
                Some(h) => format!("{cmd}: {h}"),
                None => format!(
                    "type a letter (+/- cycles): {}",
                    tk::COMMANDS.iter().map(|(c, _)| *c).collect::<Vec<_>>().join(" ")
                ),
            };
            out.push(Line::raw(""));
            out.push(Line::styled(text, Style::new().fg(pal.sand)));
        }
        out
    }

    fn inst_lines(&self, focused: bool, pal: &Palette) -> Vec<Line<'static>> {
        let inst = self.song.instruments.get(&self.inst_id()).cloned().unwrap_or_default();
        let r0 = self.cur[INST][0];
        INST_FIELDS
            .iter()
            .enumerate()
            .map(|(i, (field, label))| {
                let dim = field.starts_with("midi") && inst.kind != "midi" && inst.out == "synth";
                Line::from(vec![
                    Span::styled(format!("{label:<11}"), Style::new().fg(pal.amber_dim)),
                    Self::cell(inst_value(&inst, field), i == r0, focused, false, dim, pal),
                ])
            })
            .collect()
    }

    fn table_lines(&self, focused: bool, height: usize, pal: &Palette) -> Vec<Line<'static>> {
        let dim = Style::new().fg(pal.amber_dim);
        let Some(tid) = self.table_id() else {
            return vec![Line::styled("no table: set one in the", dim), Line::styled("instrument (table 00…)", dim)];
        };
        let table = self.song.tables.get(&tid);
        let [r0, c0] = self.cur[TABLE];
        let mut out = vec![Line::styled("  VO TR CMD", dim)];
        for i in 0..tk::STEPS.min(height.saturating_sub(1)) {
            let row = table.and_then(|t| t.get(i)).cloned().unwrap_or_default();
            let has_cmd = !row.cmd.is_empty();
            out.push(Line::from(vec![
                Span::styled(format!("{i:X} "), dim),
                Self::cell(tk::hx(row.vol), (i, 0) == (r0, c0), focused, false, row.vol.is_none(), pal),
                Span::raw(" "),
                Self::cell(tk::hx(Some(row.transpose)), (i, 1) == (r0, c0), focused, false, row.transpose == 0, pal),
                Span::raw(" "),
                Self::cell(
                    if has_cmd { row.cmd.clone() } else { "-".into() },
                    (i, 2) == (r0, c0),
                    focused,
                    false,
                    !has_cmd,
                    pal,
                ),
                Self::cell(
                    if has_cmd { tk::hx(Some(row.val)) } else { "--".into() },
                    (i, 3) == (r0, c0),
                    focused,
                    false,
                    !has_cmd,
                    pal,
                ),
            ]));
        }
        out
    }

    fn wave_lines(&self, focused: bool, height: usize, pal: &Palette) -> Vec<Line<'static>> {
        let wave = self.song.waves.get(&self.wave_id()).cloned().unwrap_or(vec![8; 32]);
        let c0 = self.cur[WAVE][1].min(wave.len().saturating_sub(1));
        let rows = height.saturating_sub(1).clamp(2, 8);
        let mut out = vec![];
        for line in 0..rows {
            let top = (rows - line) as f64 * 16.0 / rows as f64;
            let spans: Vec<Span<'static>> = wave
                .iter()
                .enumerate()
                .map(|(i, v)| {
                    let v = (*v + 1) as f64;
                    let ch = if v >= top {
                        "█"
                    } else if v >= top - 8.0 / rows as f64 {
                        "▄"
                    } else {
                        " "
                    };
                    let style = if focused && i == c0 {
                        Style::new().fg(pal.amber_hi).bg(pal.amber_faint)
                    } else {
                        Style::new().fg(pal.amber)
                    };
                    Span::styled(ch, style)
                })
                .collect();
            out.push(Line::from(spans));
        }
        out.push(Line::styled(
            format!("sample {c0:02X} = {:X}  j/k or 0-f", wave.get(c0).copied().unwrap_or(8)),
            Style::new().fg(pal.amber_dim),
        ));
        out
    }

    fn status_line(&self, s: &Snap, pal: &Palette) -> Line<'static> {
        let mut spans = vec![];
        if s.playing {
            let mode = match s.mode {
                Some(Mode::Song) => "SONG",
                Some(Mode::Chain) => "CHAIN",
                Some(Mode::Phrase) => "PHRASE",
                None => "",
            };
            spans.push(Span::styled(format!(" ▶ {mode:<6} "), bold(pal.teal)));
        } else {
            spans.push(Span::styled(" ■ STOP   ", bold(pal.amber_dim)));
        }
        let groove = self.song.groove.iter().map(|g| g.to_string()).collect::<Vec<_>>().join("/");
        spans.push(Span::styled(
            format!("BPM {}  GROOVE {groove}  ", if s.playing { s.bpm } else { self.song.bpm }),
            Style::new().fg(pal.amber),
        ));
        if s.playing {
            const BARS: [char; 9] = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];
            for tr in 0..self.song.tracks {
                let lvl = s.peaks.get(tr).copied().unwrap_or(0.0);
                let bar = BARS[((lvl * 9.0) as usize).min(8)];
                let colour = if s.mutes.contains(&tr) { pal.red } else { pal.teal };
                spans.push(Span::styled(bar.to_string(), Style::new().fg(colour)));
            }
            spans.push(Span::raw("  "));
        }
        spans.push(Span::styled(format!("♪ {}", s.how.as_deref().unwrap_or("idle")), Style::new().fg(pal.amber_dim)));
        if let Some(port) = &s.midi {
            spans.push(Span::styled(
                format!(" · MIDI {}", port.rsplit('/').next().unwrap_or(port)),
                Style::new().fg(pal.amber_dim),
            ));
        }
        if self.insert {
            spans.push(Span::styled(format!("  ◆ PIANO oct {}", self.octave), bold(pal.red)));
        }
        if !self.message.is_empty() {
            spans.push(Span::styled(format!("  │ {}", self.message), bold(pal.sand)));
        }
        Line::from(spans)
    }

    fn click(&mut self, kind: usize, x: u16, y: u16) {
        let r = self.inner[kind];
        if !r.contains((x, y).into()) {
            return;
        }
        let (x, y) = ((x - r.x) as usize, (y - r.y) as usize);
        let row = y.checked_sub(1);
        match (kind, row) {
            (SONG, Some(row)) => {
                self.cur[SONG] = [
                    (self.song_top + row).min(tk::ROWS - 1),
                    (x.saturating_sub(2) / 3).min(self.song.tracks.saturating_sub(1)),
                ]
            }
            (CHAIN | PHRASE | TABLE, Some(row)) if row < tk::STEPS => self.cur[kind][0] = row,
            (INST, _) if y < INST_FIELDS.len() => self.cur[INST][0] = y,
            _ => {}
        }
        self.typed = None;
    }
}

impl Channel for TrackerView {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "CREATIVE"
    }

    fn render(&mut self, frame: &mut Frame, area: Rect, view: &View) -> Vec<Rect> {
        let pal = view.pal;
        if !self.loaded_last {
            self.loaded_last = true;
            if let Ok(last) = std::fs::read_to_string(Self::last_file()) {
                let p = PathBuf::from(last.trim());
                if let Ok(song) = tk::load(&p) {
                    self.load(song);
                }
            }
        }
        match view.focus {
            crate::nav::Focus::Inside(i) | crate::nav::Focus::Nav(Some(i)) if i < PANELS => self.panel = i,
            _ => {}
        }
        let s = self.snap();
        let cmd_h = if self.cmdline.is_some() { 3 } else { 0 };
        let [main, status, cmd] =
            Layout::vertical([Constraint::Fill(1), Constraint::Length(1), Constraint::Length(cmd_h)]).areas(area);
        let [song_a, chain_a, phrase_a, right] = Layout::horizontal([
            Constraint::Length(32),
            Constraint::Length(15),
            Constraint::Length(24),
            Constraint::Fill(1),
        ])
        .areas(main);
        let [inst_a, table_a, wave_a] =
            Layout::vertical([Constraint::Length(15), Constraint::Min(8), Constraint::Length(8)]).areas(right);
        let areas = [song_a, chain_a, phrase_a, inst_a, table_a, wave_a];

        let (c, p, i, t) = (self.chain_id(), self.phrase_id(), self.inst_id(), self.table_id());
        let inst_name = self.song.instruments.get(&i).map_or(String::new(), |x| x.name.clone());
        let titles = [
            format!("SONG · {}{}", self.song.title, if self.dirty { " ●" } else { "" }),
            format!("CHAIN {}", tk::hx(c)),
            format!("PHRASE {}", tk::hx(p)),
            format!("INSTRUMENT {} {inst_name}", tk::hx(Some(i))),
            format!("TABLE {}", tk::hx(t)),
            format!("WAVE {}", tk::hx(Some(self.wave_id()))),
        ];
        for k in 0..PANELS {
            let mut block = ui::panel(pal, &titles[k], view.panel_state(k));
            if k == PHRASE && self.insert {
                block = block.title_bottom(Span::styled(format!(" PIANO · oct {} ", self.octave), bold(pal.red)));
            }
            let inner = block.inner(areas[k]);
            self.inner[k] = inner;
            frame.render_widget(block, areas[k]);
            let focused = view.inside(k);
            let h = inner.height as usize;
            let lines = match k {
                SONG => self.song_lines(focused, h, &s, pal),
                CHAIN => self.chain_lines(focused, &s, pal),
                PHRASE => self.phrase_lines(focused, &s, pal),
                INST => self.inst_lines(focused, pal),
                TABLE => self.table_lines(focused, h, pal),
                _ => self.wave_lines(focused, h, pal),
            };
            frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), inner);
        }
        frame.render_widget(Paragraph::new(self.status_line(&s, pal)).style(Style::new().bg(pal.panel)), status);
        if let Some(input) = &mut self.cmdline {
            let block = ui::panel(pal, "", ui::PanelState::Inside);
            let inner = block.inner(cmd);
            frame.render_widget(block, cmd);
            input.render(frame, inner, Style::new().fg(pal.amber_hi), Style::new().fg(pal.amber_dim), true);
        }
        if let Some(b) = &mut self.browser {
            b.render(frame, area, pal);
        }
        areas.to_vec()
    }

    fn captures_keys(&self) -> bool {
        self.cmdline.is_some() || self.browser.is_some()
    }

    fn key(&mut self, section: usize, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if let Some(b) = &mut self.browser {
            let answer = b.key(&key);
            if let Some((old, new)) = b.renamed.take()
                && self.song.path.as_ref() == Some(&old)
            {
                self.song.path = Some(new); // the open song was renamed: keep saving there
                self.remember();
            }
            match answer {
                crate::browser::Answer::Open => {}
                crate::browser::Answer::Cancel => self.browser = None,
                crate::browser::Answer::Chosen(choice) => {
                    self.browser = None;
                    self.browsed(choice);
                    let panel = self.panel;
                    self.focus(panel, ctx);
                }
            }
            return Outcome::Used;
        }
        if let Some(input) = &mut self.cmdline {
            if key.code == KeyCode::Esc {
                self.cmdline = None;
                return Outcome::Used;
            }
            if input.key(&key) == InputResult::Submit {
                let line = input.value.clone();
                self.cmdline = None;
                self.command(&line, ctx);
            }
            return Outcome::Used;
        }
        if section >= PANELS {
            return Outcome::Ignored;
        }
        if self.panel_key(section, &key, ctx) { Outcome::Used } else { Outcome::Ignored }
    }

    fn nav_key(&mut self, key: KeyEvent, ctx: &mut Ctx) -> Outcome {
        if keys::ctrl(&key, 'o') {
            self.browse();
        } else if keys::ctrl(&key, 's') {
            self.command("w", ctx);
        } else if keys::is(&key, ':') {
            self.open_cmdline("");
        } else if keys::is(&key, ' ') {
            self.play();
        } else {
            return Outcome::Ignored;
        }
        Outcome::Used
    }

    fn paste(&mut self, _section: usize, text: &str, _ctx: &mut Ctx) {
        if let Some(input) = &mut self.cmdline {
            input.insert(text);
        }
    }

    fn mouse(&mut self, event: MouseEvent, section: Option<usize>, ctx: &mut Ctx) -> Outcome {
        if self.browser.is_some() {
            return Outcome::Used;
        }
        match (event.kind, section) {
            (MouseEventKind::Down(MouseButton::Left), Some(k)) if k < PANELS => {
                self.click(k, event.column, event.row);
                self.focus(k, ctx);
                Outcome::Used
            }
            (MouseEventKind::ScrollUp | MouseEventKind::ScrollDown, Some(k)) if k < PANELS && k != WAVE => {
                self.movement(k, if event.kind == MouseEventKind::ScrollUp { -1 } else { 1 }, 0);
                Outcome::Used
            }
            _ => Outcome::Ignored,
        }
    }

    fn lands_inside(&self, _narrow: bool) -> Option<usize> {
        Some(SONG)
    }

    fn next_wake(&self) -> Option<Instant> {
        let a = self.audio.as_ref()?;
        Some(if self.playing() { Instant::now() + FRAME } else { a.last_sound + IDLE_RELEASE })
    }

    fn tick(&mut self, now: Instant, _visible: bool, _ctx: &mut Ctx) {
        let playing = self.playing();
        if let Some(a) = &mut self.audio {
            if playing {
                a.last_sound = now;
            } else if now.duration_since(a.last_sound) > IDLE_RELEASE {
                self.audio = None; // closes the player and the MIDI port
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn view() -> TrackerView {
        let cfg = crate::config::parse("url = \"http://h:7000\"\ntoken = \"ody_x\"", std::path::Path::new("test.toml"))
            .unwrap();
        TrackerView::new(&cfg)
    }

    #[test]
    fn context_follows_the_cursor() {
        let tv = view();
        assert_eq!(tv.chain_id(), Some(0));
        assert_eq!(tv.phrase_id(), Some(0));
        assert_eq!(tv.inst_id(), 0); // the lead
        assert_eq!(tv.table_id(), None);
    }

    #[test]
    fn hex_typing_combines_two_digits() {
        let mut tv = view();
        tv.cur[SONG] = [1, 0];
        tv.type_hex(SONG, 1);
        tv.type_hex(SONG, 0xA);
        assert_eq!(tv.song.rows[1][0], Some(0x1A));
        tv.movement(SONG, 0, 0);
        tv.type_hex(SONG, 3);
        assert_eq!(tv.song.rows[1][0], Some(3));
        tv.undo(true);
        assert_eq!(tv.song.rows[1][0], Some(0x1A));
    }

    #[test]
    fn nudging_notes_and_commands() {
        let mut tv = view();
        tv.cur[PHRASE] = [1, 0]; // an empty step
        tv.nudge(PHRASE, 16);
        let st = &tv.song.phrases[&0][1];
        assert_eq!((st.note, st.inst), (Some(72), Some(0)));
        tv.cur[PHRASE] = [1, 2];
        tv.nudge(PHRASE, -1);
        assert_eq!(tv.song.phrases[&0][1].cmd, "V"); // -1 from nothing wraps to the end
        tv.nudge(PHRASE, 1);
        assert_eq!(tv.song.phrases[&0][1].cmd, "W");
        tv.clear(PHRASE);
        assert_eq!(tv.song.phrases[&0][1].cmd, "");
    }

    #[test]
    fn new_chain_copy_and_paste() {
        let mut tv = view();
        tv.cur[SONG] = [1, 0];
        tv.new_thing(SONG, false);
        let new = tv.song.rows[1][0].unwrap();
        assert!(new > 2);
        tv.cur[SONG] = [0, 0];
        tv.copy_cell(SONG, false);
        tv.cur[SONG] = [2, 3];
        tv.paste(SONG, false);
        assert_eq!(tv.song.rows[2][3], Some(0));
        tv.cur[SONG] = [0, 0];
        tv.copy_cell(CHAIN, true);
        tv.cur[SONG] = [1, 0];
        tv.paste(CHAIN, true);
        assert_eq!(tv.song.chains[&new], tv.song.chains[&0]);
    }

    #[test]
    fn piano_writes_and_advances() {
        let mut tv = view();
        tv.cur[PHRASE] = [1, 0];
        assert!(tv.piano('z'));
        assert_eq!(tv.song.phrases[&0][1].note, Some(60));
        assert_eq!(tv.cur[PHRASE][0], 2);
        assert!(tv.piano('ö')); // D# on a Swedish keyboard
        assert_eq!(tv.song.phrases[&0][2].note, Some(60 + 15));
        assert!(tv.piano('='));
        assert_eq!(tv.song.phrases[&0][3].note, Some(tk::NOTE_OFF));
        assert!(!tv.piano('k'));
        tv.audio = None;
    }

    #[test]
    fn song_card_counts_the_demo() {
        let dir = std::env::temp_dir().join(format!("trk-card-{}", std::process::id()));
        let path = dir.join("demo.mtk");
        let mut song = tk::demo();
        tk::save(&mut song, Some(&path)).unwrap();
        let pal = crate::palette::Palette::base();
        let text: Vec<String> = song_card(&path, 40, 20, &pal).iter().map(|l| l.to_string()).collect();
        assert!(text[1].contains("first light"));
        assert!(text[3].contains("3 chains · 3 phrases"));
        assert!(text.iter().any(|l| l.contains("00 00 01 02 --")));
        let _ = std::fs::remove_dir_all(dir);
    }
}
