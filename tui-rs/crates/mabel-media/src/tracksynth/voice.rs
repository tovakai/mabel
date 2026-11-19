//! The voices: starting and releasing notes, the commands, and every tick and sample of
//! sound a voice makes (envelope, table, chord, slide, bend, vibrato, then the waveform).

use super::*;

impl Engine {
    /// Note a note on/off for MIDI export (only while exporting).
    pub(super) fn log(&mut self, on: bool, track: usize, note: i64, inst: Option<Instrument>) {
        let samples = self.samples;
        if let Some(e) = &mut self.events {
            e.push((samples, on, track, note, inst));
        }
    }

    /// Start a note on a track's voice with its instrument (keeping the voice's instrument if
    /// none is given). With L's `slide` on the same instrument, glide to it instead.
    pub(super) fn trigger(&mut self, track: usize, note: i64, inst_id: Option<i64>, slide: i64) {
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

    /// Send the note to MIDI too, if the instrument goes there (kind midi, or out = midi/both).
    pub(super) fn midi_on(&mut self, track: usize, inst: &Instrument, note: i64) {
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

    /// Release the track's MIDI note.
    pub(super) fn midi_off(&mut self, track: usize) {
        if let Some((ch, note)) = self.voices[track].midi_note.take() {
            self.midi.note_off(ch, note);
        }
    }

    /// Let go of a track's note: its envelope goes to release, and MIDI gets a note off.
    pub(super) fn release(&mut self, track: usize) {
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

    /// Apply a step's command (A, C, D… see `tracker::COMMANDS`) to a track.
    pub(super) fn command(&mut self, track: usize, cmd: &str, val: i64) {
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
    /// Once a tick per voice: the envelope, table rows, chord steps, slide, bend, vibrato,
    /// retrigger, kill and delayed notes.
    pub(super) fn voice_tick(&mut self, track: usize) {
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

    /// Add a voice's samples (pulse, triangle, saw, wave or noise, panned) into `out`,
    /// ramping volume changes so they don't click.
    pub(super) fn voice_render(&mut self, track: usize, out: &mut [[f64; 2]]) {
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
}
