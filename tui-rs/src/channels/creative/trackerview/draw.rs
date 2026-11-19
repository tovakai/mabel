//! Drawing TRACKER: each panel's lines, the status line, and the browser's song preview.

use super::*;

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

impl TrackerView {
    /// One cell's text in the right style: the cursor (inverted when the keys are inside the
    /// panel, underlined when not), the step playing now (teal), empty cells (dim).
    pub(super) fn cell(
        text: String,
        cursor: bool,
        focused: bool,
        play: bool,
        dim: bool,
        pal: &Palette,
    ) -> Span<'static> {
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

    /// SONG: a header of track numbers (M = muted, S = solo) and the rows of chain ids that
    /// fit, scrolled so the cursor stays on screen.
    pub(super) fn song_lines(&mut self, focused: bool, height: usize, s: &Snap, pal: &Palette) -> Vec<Line<'static>> {
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

    /// CHAIN: the 16 steps of the chain under SONG's cursor (phrase, transpose).
    pub(super) fn chain_lines(&self, focused: bool, s: &Snap, pal: &Palette) -> Vec<Line<'static>> {
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

    /// PHRASE: the 16 steps of the phrase under CHAIN's cursor (note, instrument, command, value).
    pub(super) fn phrase_lines(&self, focused: bool, s: &Snap, pal: &Palette) -> Vec<Line<'static>> {
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

    /// INSTRUMENT: one line per field in `INST_FIELDS`.
    pub(super) fn inst_lines(&self, focused: bool, pal: &Palette) -> Vec<Line<'static>> {
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

    /// TABLE: the instrument's table, 16 rows (volume, transpose, command, value).
    pub(super) fn table_lines(&self, focused: bool, height: usize, pal: &Palette) -> Vec<Line<'static>> {
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

    /// WAVE: the 32 samples (0-15) drawn as a little bar graph.
    pub(super) fn wave_lines(&self, focused: bool, height: usize, pal: &Palette) -> Vec<Line<'static>> {
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

    /// The line under the panels: what's playing and where, the tempo, the audio output and
    /// MIDI port, and the last message.
    pub(super) fn status_line(&self, s: &Snap, pal: &Palette) -> Line<'static> {
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
}
