//! TRACKER's controls: what each key does (diving between panels, piano mode, every panel
//! key), the `:` line and the ctrl+o browser, and mouse clicks.

use super::*;

impl TrackerView {
    /// enter: go one panel deeper (SONG → CHAIN → PHRASE → INSTRUMENT → TABLE → WAVE).
    /// On a PHRASE note it toggles piano mode instead; on PHRASE's instrument column it
    /// goes to that instrument.
    pub(super) fn dive(&mut self, kind: usize, ctx: &mut Ctx) {
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

    /// Put the keys inside panel `kind` (and make it the one space plays from).
    pub(super) fn focus(&mut self, kind: usize, ctx: &mut Ctx) {
        self.panel = kind;
        self.typed = None;
        ctx.focus(kind);
    }

    /// Piano mode: a letter key writes its note (`LOWER` is one octave, `UPPER` the next) into
    /// the step under the cursor, plays it, and moves down. `=` writes a note off.
    /// False if the key isn't a piano key.
    pub(super) fn piano(&mut self, c: char) -> bool {
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
    pub(super) fn panel_key(&mut self, kind: usize, key: &KeyEvent, ctx: &mut Ctx) -> bool {
        self.panel = kind;
        let c = keys::ch(key);
        let code = key.code;
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        // One long if/else chain, first match wins: piano mode first (it takes most letters),
        // then movement, panels, playing, editing. Each branch is one key's job.
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
        // After any edit: the engine plays the changed song, and a step's instrument becomes
        // the one new notes get.
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

    /// Open the `:` line (with `prefill` already typed).
    pub(super) fn open_cmdline(&mut self, prefill: &str) {
        let mut input = Input::new(": command   (:w song.mtk · :bpm 140 · :export wav · :help)");
        input.set(prefill);
        self.cmdline = Some(input);
    }

    /// ctrl+o: the file browser on `tracker_dir`, showing songs (and exported .wav/.mid).
    pub(super) fn browse(&mut self) {
        self.browser =
            Some(crate::channels::creative::browser::Browser::new(crate::channels::creative::browser::Spec {
                title: "TRACKER · SONGS".into(),
                root: self.dir.clone(),
                suffixes: vec!["mtk".into()],
                also_show: vec!["wav".into(), "mid".into()],
                document_dir: None,
                extras: vec![],
                new_suffix: "mtk".into(),
                dirty: self.dirty,
                current: self.song.path.clone(),
                preview: crate::channels::creative::browser::Preview::Song,
            }));
    }

    /// What the browser chose: open a song, or make a new one (saving the current one first
    /// if asked).
    pub(super) fn browsed(&mut self, choice: crate::channels::creative::browser::Choice) {
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

    /// Make `song` the one being edited: stop playing, forget undo, cursors to the top.
    pub(super) fn load(&mut self, song: Song) {
        self.with_engine(|e| e.stop_all());
        self.song = song;
        self.sync();
        self.undo_stack.clear();
        self.redo_stack.clear();
        self.cur = [[0, 0]; PANELS];
        (self.song_top, self.dirty, self.insert) = (0, false, false);
    }

    /// A click at screen (x, y) in panel `kind`: put that panel's cursor on the cell there
    /// (the first line of each panel is its header, hence `y - 1`).
    pub(super) fn click(&mut self, kind: usize, x: u16, y: u16) {
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
