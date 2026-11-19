//! TRACKER's cells: what can be under the cursor (`Target`), its value (`V`), and the editing
//! that changes it: moving, nudging, typing hex, clearing, new and cloned chains/phrases,
//! copy and paste.

use super::*;

/// A cell's value: a (maybe empty) number or a word (kind, out, cmd).
#[derive(Clone, Debug, PartialEq)]
pub(super) enum V {
    /// A number; None is an empty cell (shown as `--`).
    N(Option<i64>),
    /// A word: an instrument's kind or out, a command letter.
    T(String),
}

impl V {
    /// The number, if it is one.
    pub(super) fn num(&self) -> Option<i64> {
        match self {
            V::N(n) => *n,
            V::T(_) => None,
        }
    }

    /// The word, if it is one ("" otherwise).
    pub(super) fn text(self) -> String {
        match self {
            V::T(t) => t,
            V::N(_) => String::new(),
        }
    }
}

/// What the cursor edits.
#[derive(Clone, Copy, Debug, PartialEq)]
pub(super) enum Target {
    /// row, track
    Song(usize, usize),
    /// chain, step, column (phrase, transpose)
    Chain(i64, usize, usize),
    /// phrase, step, column (note, inst, cmd, val)
    Phrase(i64, usize, usize),
    /// instrument, field
    Inst(i64, usize),
    /// table, row, column (vol, transpose, cmd, val)
    Table(i64, usize, usize),
    /// wave, sample (0-31)
    Wave(i64, usize),
}

/// What kind of value a target holds ("note", "inst", "cmd", "volume"…): decides how
/// nudging, typing and clearing treat it, and whether a copied value may be pasted there.
pub(super) fn attr(t: Target) -> &'static str {
    match t {
        Target::Song(..) => "song",
        Target::Chain(_, _, c) => ["phrase", "transpose"][c.min(1)],
        Target::Phrase(_, _, c) => ["note", "inst", "cmd", "val"][c.min(3)],
        Target::Inst(_, f) => INST_FIELDS[f].0,
        Target::Table(_, _, c) => ["vol", "transpose", "cmd", "val"][c.min(3)],
        Target::Wave(..) => "wave",
    }
}

/// An instrument's field by name (the names in `INST_FIELDS`).
pub(super) fn inst_get(i: &Instrument, field: &str) -> V {
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

/// Set an instrument's field by name.
pub(super) fn inst_set(i: &mut Instrument, field: &str, v: V) {
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

/// An instrument's field as INSTRUMENT shows it (hex, with words where they help: "80 centre").
pub(super) fn inst_value(i: &Instrument, field: &str) -> String {
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
pub(super) enum Clip {
    /// One value, with its kind (`attr`): it only pastes into the same kind.
    Val(&'static str, V),
    Step(Step),
    Row(TableRow),
}

/// Y/P: a whole chain, phrase or table.
#[derive(Clone)]
pub(super) enum Block {
    Chain(Vec<ChainStep>),
    Phrase(Vec<Step>),
    Table(Vec<TableRow>),
}

/// A panel's name in messages ("copied the phrase").
pub(super) fn kind_name(kind: usize) -> &'static str {
    ["song", "chain", "phrase", "instrument", "table", "wave"][kind.min(5)]
}

impl TrackerView {
    /// How many columns a panel's cursor can move across.
    pub(super) fn cols(&self, kind: usize) -> usize {
        [self.song.tracks, 2, 4, 1, 4, 32][kind]
    }

    /// How many rows it can move down.
    pub(super) fn rows(&self, kind: usize) -> usize {
        match kind {
            SONG => tk::ROWS,
            INST => INST_FIELDS.len(),
            WAVE => 1,
            _ => tk::STEPS,
        }
    }

    /// Move a panel's cursor by `dr` rows and `dc` columns, staying inside. Moving ends
    /// two-digit hex entry.
    pub(super) fn movement(&mut self, kind: usize, dr: i64, dc: i64) {
        let [r, c] = self.cur[kind];
        let clamp = |v: usize, d: i64, n: usize| (v as i64 + d).clamp(0, n.saturating_sub(1) as i64) as usize;
        self.cur[kind] = [clamp(r, dr, self.rows(kind)), clamp(c, dc, self.cols(kind))];
        self.typed = None;
    }

    /// What a panel's cursor is on. None when there's nothing there yet (an empty SONG cell
    /// has no chain to show, so CHAIN has nothing to edit).
    pub(super) fn target(&self, kind: usize) -> Option<Target> {
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

    /// Read a target's value. Missing chains/phrases/tables read as empty.
    pub(super) fn get(&self, t: Target) -> V {
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

    /// Write a target's value, making the chain/phrase/table if it doesn't exist yet.
    /// (Callers call `push` first, so it can be undone.)
    pub(super) fn set(&mut self, t: Target, v: V) {
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

    /// +/- (step ±1) and </> (±16): change the value under the cursor. Notes move by a
    /// semitone (or an octave for ±16) and play; words (kind, out, commands) cycle through
    /// their choices; numbers wrap within a byte.
    pub(super) fn nudge(&mut self, kind: usize, step: i64) {
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

    /// A hex digit typed on a number: a second digit within 2 s on the same cell makes a byte
    /// ("4" then "2" = 0x42). The MIDI channel is decimal 1-16; a wave sample is one digit.
    pub(super) fn type_hex(&mut self, kind: usize, digit: i64) {
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

    /// x: empty the cell (numbers that can't be empty go to 0; a note clears the whole step).
    pub(super) fn clear(&mut self, kind: usize) {
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
    pub(super) fn new_thing(&mut self, kind: usize, clone: bool) {
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

    /// y: copy the value under the cursor (on a note or a table's first column: the whole
    /// step/row). Y (`whole`): copy the whole chain, phrase or table.
    pub(super) fn copy_cell(&mut self, kind: usize, whole: bool) {
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

    /// p / P: paste what y / Y copied, if it fits here (a phrase into a phrase, a note into a
    /// note column…).
    pub(super) fn paste(&mut self, kind: usize, whole: bool) {
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
}
