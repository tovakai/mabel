//! Walking the song: which chain and phrase each track is on, moving to the next step,
//! looping, and the tick that drives it all.

use super::*;

impl Engine {
    /// The chain track `t` plays now (the looped one in chain mode).
    pub(super) fn cell(&self, t: usize) -> Option<i64> {
        if self.mode == Some(Mode::Chain) && t == self.focus.0 {
            return Some(self.focus.1);
        }
        self.song.rows.get(self.tracks[t].row).and_then(|r| r[t])
    }

    /// The first row of the block of filled rows that `row` belongs to (or ends just before):
    /// where a track loops back to when its block ends.
    pub(super) fn block_start(&self, t: usize, row: usize) -> Option<usize> {
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

    /// The chain step track `t` is on (in phrase mode, the looped phrase).
    pub(super) fn chain_step(&self, t: usize) -> Option<tk::ChainStep> {
        if self.mode == Some(Mode::Phrase) {
            return Some(tk::ChainStep { phrase: Some(self.focus.1), transpose: 0 });
        }
        self.song.chains.get(&self.cell(t)?).map(|c| c[self.tracks[t].cstep].clone())
    }

    /// Move track `t` to its next step: next phrase step, next chain step, next song row,
    /// looping where each runs out.
    pub(super) fn advance(&mut self, t: usize) {
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

    /// Play track `t`'s current step: its note (or note off) and its command.
    pub(super) fn step(&mut self, t: usize) {
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

    /// One tick: each running track whose step is up moves on and plays its next step (timed by
    /// the groove), preview notes run out, and every voice ticks.
    pub(super) fn tick(&mut self) {
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
}
