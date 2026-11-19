//! TRACKER's model: an LSDJ-style song (song → chains → phrases), instruments, tables and waves.
//!
//! - Song: rows × tracks of chain ids (None = empty: the track loops back to the top of its block).
//! - Chain: 16 steps of (phrase id, transpose). A chain ends at its first empty step.
//! - Phrase: 16 steps of (note, instrument, command, value). note −1 = note off.
//! - Instrument: a chip voice (pulse, tri, saw, wave, noise) or MIDI out, with an ADSR in ticks,
//!   an optional table, and where it plays (synth, midi, both).
//! - Table: 16 rows run one per tick under every note of its instrument: (volume, transpose, cmd, val).
//! - Wave: 32 samples of 0–15 for the "wave" kind.
//!
//! Values are shown in hex like LSDJ. Saved as .mtk (JSON), the same file as the Python client's.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{Result, anyhow};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

pub const STEPS: usize = 16;
pub const ROWS: usize = 256;
pub const MAX_TRACKS: usize = 8;
pub const NOTE_OFF: i32 = -1;
pub const KINDS: [&str; 6] = ["pulse", "tri", "saw", "wave", "noise", "midi"];
pub const OUTS: [&str; 3] = ["synth", "midi", "both"];
const NAMES: [&str; 12] = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"];

/// Commands (phrase and table): letter → what the value means. Small and LSDJ-flavoured.
pub const COMMANDS: [(&str, &str); 13] = [
    ("A", "start table XX on this track"),
    ("C", "chord/arpeggio: x and y semitones, cycled per tick"),
    ("D", "delay the note XX ticks"),
    ("E", "volume XX (00-FF) for the playing note"),
    ("H", "hop: end the phrase after this step"),
    ("K", "kill the note after XX ticks"),
    ("L", "slide (portamento) to the note, speed XX"),
    ("O", "pan: 00 left, 80 centre, FF right"),
    ("P", "pitch bend XX per tick (80 = none, below = down)"),
    ("R", "retrigger every XX ticks"),
    ("T", "tempo XX bpm"),
    ("V", "vibrato: x speed, y depth"),
    ("W", "pulse width / wave number XX"),
];

pub fn command_help(cmd: &str) -> Option<&'static str> {
    COMMANDS.iter().find(|(c, _)| *c == cmd).map(|(_, h)| *h)
}

pub fn note_name(n: Option<i32>) -> String {
    match n {
        None => "---".into(),
        Some(NOTE_OFF) => "OFF".into(),
        Some(n) if n >= 12 => format!("{}{}", NAMES[(n % 12) as usize], n / 12 - 1),
        Some(n) => format!("{}0", NAMES[(n.rem_euclid(12)) as usize]),
    }
}

pub fn hx(v: Option<i64>) -> String {
    v.map_or("--".into(), |v| format!("{:02X}", v & 0xFF))
}

/// A byte as LSDJ shows transpose: 00–7F up, 80–FF down (FF = −1).
pub fn signed(v: i64) -> i64 {
    let v = v & 0xFF;
    if v >= 0x80 { v - 256 } else { v }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Step {
    pub note: Option<i32>,
    pub inst: Option<i64>,
    pub cmd: String,
    pub val: i64,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct ChainStep {
    pub phrase: Option<i64>,
    /// A byte; `signed` reads it.
    pub transpose: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Instrument {
    pub name: String,
    pub kind: String,
    pub volume: i64,
    /// Ticks to full volume.
    pub attack: i64,
    /// Ticks from full to sustain.
    pub decay: i64,
    /// Level while held.
    pub sustain: i64,
    /// Ticks to silence after note off.
    pub release: i64,
    /// Pulse width (80 = square) / wave number for "wave".
    pub width: i64,
    /// A byte, signed.
    pub transpose: i64,
    pub table: Option<i64>,
    pub pan: i64,
    /// synth | midi | both.
    pub out: String,
    /// 1–16 (on Pocket Miku 1 is Miku's voice).
    pub midi_channel: i64,
    pub midi_program: Option<i64>,
}

impl Default for Instrument {
    fn default() -> Self {
        Instrument {
            name: "inst".into(),
            kind: "pulse".into(),
            volume: 0xC0,
            attack: 0,
            decay: 8,
            sustain: 0x80,
            release: 6,
            width: 0x80,
            transpose: 0,
            table: None,
            pan: 0x80,
            out: "synth".into(),
            midi_channel: 2,
            midi_program: None,
        }
    }
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct TableRow {
    /// None = leave the volume.
    pub vol: Option<i64>,
    /// A byte, signed, added to the note.
    pub transpose: i64,
    pub cmd: String,
    pub val: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Song {
    pub title: String,
    pub tracks: usize,
    pub bpm: i64,
    /// Ticks per step, cycled.
    pub groove: Vec<i64>,
    pub rows: Vec<[Option<i64>; MAX_TRACKS]>,
    pub chains: BTreeMap<i64, Vec<ChainStep>>,
    pub phrases: BTreeMap<i64, Vec<Step>>,
    pub instruments: BTreeMap<i64, Instrument>,
    pub tables: BTreeMap<i64, Vec<TableRow>>,
    pub waves: BTreeMap<i64, Vec<i64>>,
    /// A rawmidi device (/dev/snd/midiC2D0); "" = find one.
    pub midi_port: String,
    pub path: Option<PathBuf>,
}

#[allow(clippy::too_many_arguments)]
fn inst(
    name: &str,
    kind: &str,
    volume: i64,
    attack: i64,
    decay: i64,
    sustain: i64,
    release: i64,
    width: i64,
) -> Instrument {
    Instrument {
        name: name.into(),
        kind: kind.into(),
        volume,
        attack,
        decay,
        sustain,
        release,
        width,
        ..Default::default()
    }
}

pub fn default_instruments() -> BTreeMap<i64, Instrument> {
    BTreeMap::from([
        (0, inst("lead", "pulse", 0xC0, 0, 10, 0x90, 8, 0x80)),
        (1, inst("thin", "pulse", 0xA0, 0, 6, 0x60, 6, 0x30)),
        (2, inst("bass", "tri", 0xF0, 0, 4, 0xE0, 4, 0x80)),
        (3, inst("wave", "wave", 0xB0, 1, 12, 0x80, 10, 0)),
        (4, Instrument { table: Some(0), ..inst("kick", "tri", 0xFF, 0, 5, 0x00, 1, 0x80) }),
        (5, inst("hat", "noise", 0x70, 0, 2, 0x00, 1, 0x80)),
        (6, inst("snare", "noise", 0xC0, 0, 6, 0x00, 2, 0x80)),
        (7, Instrument { out: "midi".into(), midi_channel: 2, ..inst("miku", "midi", 0xC0, 0, 8, 0x80, 6, 0x80) }),
    ])
}

pub fn sine_wave() -> Vec<i64> {
    (0..32).map(|i| (7.5 + 7.5 * (i as f64 / 32.0 * std::f64::consts::TAU).sin()).round() as i64).collect()
}

fn blank<T: Default + Clone>() -> Vec<T> {
    vec![T::default(); STEPS]
}

impl Default for Song {
    fn default() -> Self {
        Song {
            title: "untitled".into(),
            tracks: 4,
            bpm: 128,
            groove: vec![6, 6],
            rows: vec![[None; MAX_TRACKS]; ROWS],
            chains: BTreeMap::new(),
            phrases: BTreeMap::new(),
            instruments: default_instruments(),
            tables: BTreeMap::new(),
            waves: BTreeMap::from([(0, sine_wave())]),
            midi_port: String::new(),
            path: None,
        }
    }
}

impl Song {
    // ── lookup that creates on demand (editing an empty id makes it) ─────

    pub fn chain(&mut self, i: i64) -> &mut Vec<ChainStep> {
        self.chains.entry(i).or_insert_with(blank)
    }

    pub fn phrase(&mut self, i: i64) -> &mut Vec<Step> {
        self.phrases.entry(i).or_insert_with(blank)
    }

    pub fn instrument(&mut self, i: i64) -> &mut Instrument {
        self.instruments.entry(i).or_insert_with(|| Instrument { name: format!("inst{i:02X}"), ..Default::default() })
    }

    pub fn table(&mut self, i: i64) -> &mut Vec<TableRow> {
        self.tables.entry(i).or_insert_with(blank)
    }

    pub fn wave(&mut self, i: i64) -> &mut Vec<i64> {
        self.waves.entry(i).or_insert_with(|| vec![8; 32])
    }

    // ── ids ──────────────────────────────────────────────────────────────

    pub fn used_chains(&self) -> std::collections::BTreeSet<i64> {
        let mut used: std::collections::BTreeSet<i64> =
            self.chains.iter().filter(|(_, c)| c.iter().any(|s| s.phrase.is_some())).map(|(k, _)| *k).collect();
        used.extend(self.rows.iter().flat_map(|r| r.iter().flatten().copied()));
        used
    }

    pub fn used_phrases(&self) -> std::collections::BTreeSet<i64> {
        let mut used: std::collections::BTreeSet<i64> = self
            .phrases
            .iter()
            .filter(|(_, p)| p.iter().any(|s| s.note.is_some() || !s.cmd.is_empty()))
            .map(|(k, _)| *k)
            .collect();
        used.extend(self.chains.values().flat_map(|c| c.iter().filter_map(|s| s.phrase)));
        used
    }

    fn free(used: &std::collections::BTreeSet<i64>) -> Option<i64> {
        (0..=0xFF).find(|i| !used.contains(i))
    }

    pub fn new_chain(&mut self) -> Option<i64> {
        let i = Self::free(&self.used_chains())?;
        self.chains.insert(i, blank());
        Some(i)
    }

    pub fn new_phrase(&mut self) -> Option<i64> {
        let i = Self::free(&self.used_phrases())?;
        self.phrases.insert(i, blank());
        Some(i)
    }

    pub fn clone_chain(&mut self, i: i64) -> Option<i64> {
        let src = self.chain(i).clone();
        let j = self.new_chain()?;
        self.chains.insert(j, src);
        Some(j)
    }

    pub fn clone_phrase(&mut self, i: i64) -> Option<i64> {
        let src = self.phrase(i).clone();
        let j = self.new_phrase()?;
        self.phrases.insert(j, src);
        Some(j)
    }

    /// The last song row that has anything in it.
    pub fn last_row(&self) -> Option<usize> {
        (0..ROWS).rev().find(|r| self.rows[*r][..self.tracks].iter().any(|c| c.is_some()))
    }
}

/// A tiny starter so there's something to press play on.
pub fn demo() -> Song {
    let mut song = Song { title: "first light".into(), ..Default::default() };
    let mut kick: Vec<TableRow> =
        [24, 12, 5, 0].iter().map(|t| TableRow { transpose: *t, ..Default::default() }).collect();
    kick.resize(STEPS, TableRow::default());
    song.tables.insert(0, kick);
    let lead = [
        Some(69),
        None,
        Some(72),
        None,
        Some(76),
        None,
        Some(72),
        None,
        Some(74),
        None,
        Some(71),
        None,
        Some(67),
        None,
        None,
        Some(NOTE_OFF),
    ];
    for (i, n) in lead.iter().enumerate() {
        if let Some(n) = n {
            song.phrase(0)[i] = Step { note: Some(*n), inst: (*n != NOTE_OFF).then_some(0), ..Default::default() };
        }
    }
    for i in (0..16).step_by(4) {
        song.phrase(1)[i] = Step { note: Some(if i < 8 { 45 } else { 43 }), inst: Some(2), ..Default::default() };
    }
    for i in 0..16 {
        let (note, ins) = match i % 8 {
            0 => (36, 4),
            4 => (60, 6),
            _ if i % 2 == 0 => (84, 5),
            _ => continue,
        };
        song.phrase(2)[i] = Step { note: Some(note), inst: Some(ins), ..Default::default() };
    }
    for (c, p) in [(0, 0), (1, 1), (2, 2)] {
        song.chain(c)[0] = ChainStep { phrase: Some(p), transpose: 0 };
        song.chain(c)[1] = ChainStep { phrase: Some(p), transpose: if c < 2 { 5 } else { 0 } };
    }
    song.rows[0][..3].copy_from_slice(&[Some(0), Some(1), Some(2)]);
    song
}

// ── files ────────────────────────────────────────────────────────────────

fn dense<T: Serialize>(map: &BTreeMap<i64, Vec<T>>, empty: impl Fn(&[T]) -> bool) -> Value {
    Value::Object(map.iter().filter(|(_, v)| !empty(v)).map(|(k, v)| (format!("{k:02X}"), json!(v))).collect())
}

pub fn to_json(song: &Song) -> Value {
    let rows: Map<String, Value> = song
        .rows
        .iter()
        .enumerate()
        .filter(|(_, r)| r.iter().any(|c| c.is_some()))
        .map(|(i, r)| (format!("{i:02X}"), json!(r[..song.tracks].to_vec())))
        .collect();
    json!({
        "version": 1,
        "title": song.title,
        "tracks": song.tracks,
        "bpm": song.bpm,
        "groove": song.groove,
        "midi_port": song.midi_port,
        "rows": rows,
        "chains": dense(&song.chains, |c| c.iter().all(|s| s.phrase.is_none())),
        "phrases": dense(&song.phrases, |p| p.iter().all(|s| s.note.is_none() && s.cmd.is_empty())),
        "instruments": Value::Object(song.instruments.iter().map(|(k, v)| (format!("{k:02X}"), json!(v))).collect()),
        "tables": dense(&song.tables, |t| t.iter().all(|r| r.vol.is_none() && r.transpose == 0 && r.cmd.is_empty())),
        "waves": Value::Object(song.waves.iter().map(|(k, v)| (format!("{k:02X}"), json!(v))).collect()),
    })
}

fn ids(v: &Value) -> Vec<(i64, &Value)> {
    v.as_object().into_iter().flatten().filter_map(|(k, v)| Some((i64::from_str_radix(k, 16).ok()?, v))).collect()
}

fn steps<T: for<'de> Deserialize<'de> + Default + Clone>(v: &Value) -> Vec<T> {
    let mut items: Vec<T> = v
        .as_array()
        .into_iter()
        .flatten()
        .take(STEPS)
        .map(|s| serde_json::from_value(s.clone()).unwrap_or_default())
        .collect();
    items.resize(STEPS, T::default());
    items
}

pub fn from_json(data: &Value, path: Option<PathBuf>) -> Song {
    let mut song = Song {
        title: data["title"].as_str().unwrap_or("untitled").to_string(),
        tracks: data["tracks"].as_u64().unwrap_or(4).clamp(1, MAX_TRACKS as u64) as usize,
        bpm: data["bpm"].as_i64().unwrap_or(128),
        groove: data["groove"]
            .as_array()
            .map(|g| g.iter().filter_map(|x| x.as_i64()).collect())
            .filter(|g: &Vec<i64>| !g.is_empty())
            .unwrap_or(vec![6, 6]),
        midi_port: data["midi_port"].as_str().unwrap_or("").to_string(),
        path,
        ..Default::default()
    };
    for (r, row) in ids(&data["rows"]) {
        if (0..ROWS as i64).contains(&r) {
            for (t, c) in row.as_array().into_iter().flatten().take(MAX_TRACKS).enumerate() {
                song.rows[r as usize][t] = c.as_i64();
            }
        }
    }
    song.chains = ids(&data["chains"]).into_iter().map(|(k, v)| (k, steps(v))).collect();
    song.phrases = ids(&data["phrases"]).into_iter().map(|(k, v)| (k, steps(v))).collect();
    let insts: BTreeMap<i64, Instrument> = ids(&data["instruments"])
        .into_iter()
        .map(|(k, v)| (k, serde_json::from_value(v.clone()).unwrap_or_default()))
        .collect();
    if !insts.is_empty() {
        song.instruments = insts;
    }
    song.tables = ids(&data["tables"]).into_iter().map(|(k, v)| (k, steps(v))).collect();
    let waves: BTreeMap<i64, Vec<i64>> = ids(&data["waves"])
        .into_iter()
        .map(|(k, v)| {
            let mut w: Vec<i64> = v.as_array().into_iter().flatten().map(|x| x.as_i64().unwrap_or(8) & 15).collect();
            w.resize(32, 8);
            (k, w)
        })
        .collect();
    if !waves.is_empty() {
        song.waves = waves;
    }
    song
}

pub fn save(song: &mut Song, path: Option<&Path>) -> Result<PathBuf> {
    let mut path = path
        .map(Path::to_path_buf)
        .or_else(|| song.path.clone())
        .ok_or_else(|| anyhow!("no file name yet: :w name.mtk"))?;
    if path.extension().is_none() {
        path.set_extension("mtk");
    }
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("mtk.tmp");
    std::fs::write(&tmp, serde_json::to_string(&to_json(song))?)?;
    std::fs::rename(&tmp, &path)?;
    song.path = Some(path.clone());
    Ok(path)
}

pub fn load(path: &Path) -> Result<Song> {
    let text = std::fs::read_to_string(path)?;
    Ok(from_json(&serde_json::from_str(&text)?, Some(path.to_path_buf())))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_and_bytes() {
        assert_eq!(note_name(Some(69)), "A-4");
        assert_eq!(note_name(Some(NOTE_OFF)), "OFF");
        assert_eq!(note_name(None), "---");
        assert_eq!(signed(0xFF), -1);
        assert_eq!(hx(Some(10)), "0A");
    }

    #[test]
    fn demo_round_trip_and_ids() {
        let mut d = demo();
        assert_eq!(d.last_row(), Some(0));
        assert_eq!(d.new_chain(), Some(3));
        let back = from_json(&to_json(&d), None);
        assert_eq!(back.phrases[&0][0].note, Some(69));
        assert_eq!(back.chains[&1][1].transpose, 5);
        assert_eq!(back.instruments[&4].table, Some(0));
        assert_eq!(back.waves[&0], sine_wave());
        assert_eq!(back.rows[0][..3], [Some(0), Some(1), Some(2)]);
    }

    /// TRACKER_PY=<python demo json> cargo test parity -- --ignored: the same file as Python's.
    #[test]
    #[ignore]
    fn parity_with_python() {
        let Ok(path) = std::env::var("TRACKER_PY") else { return };
        let python: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
        assert_eq!(to_json(&demo()), python);
    }
}
