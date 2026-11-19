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

use crate::os::channel::{Channel, Ctx, Outcome, View};
use crate::os::keys;
use crate::os::palette::Palette;
use crate::os::ui::{self, bold};
use crate::tracker::{self as tk, ChainStep, Instrument, Song, Step, TableRow};
use crate::tracksynth::{self as ts, Engine, Midi, Mode, Output};
use crate::widgets::input::{Input, InputResult};

/// The id messages and help use.
const ID: &str = "tracker";

// The six panels, in section order (what `render` returns and `cur` is indexed by).
const SONG: usize = 0;
const CHAIN: usize = 1;
const PHRASE: usize = 2;
const INST: usize = 3;
const TABLE: usize = 4;
const WAVE: usize = 5;
/// How many panels there are.
const PANELS: usize = 6;
/// Piano: C up to E of the next octave, and one octave higher.
const LOWER: &str = "zsxdcvgbhnjm,l.;/";
const UPPER: &str = "q2w3er5t6y7ui9o0p";
/// Close audio + MIDI after this long without sound.
const IDLE_RELEASE: Duration = Duration::from_secs(30);
/// While playing, redraw this often (about 15 fps) so the play markers move.
const FRAME: Duration = Duration::from_millis(66);
/// Samples per audio block: smaller answers faster, larger is safer on the deck.
const BLOCK: usize = 512;
/// The status line while piano mode is on.
const PIANO_HINT: &str = "piano: z s x d c… / q 2 w 3 e…  +/- octave  = off  esc back";
/// The INSTRUMENT panel's rows: (field name in the song file, label shown).
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

// TRACKER's code is split by job; every piece adds to `impl TrackerView`.
mod cells; // what's under the cursor, and editing it
mod commands; // the `:` command line
mod controls;
mod draw; // drawing the panels // what each key and click does

use cells::*;
pub use draw::song_card;

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

/// The TRACKER screen: the song being edited, a cursor per panel, and the live audio.
pub struct TrackerView {
    /// The song being edited (`mabel-media`'s tracker model; saved as .mtk).
    pub song: Song,
    /// The song before each edit (at most 100), for u; redo gets what undo took.
    undo_stack: Vec<Song>,
    redo_stack: Vec<Song>,
    /// Each panel's cursor as [row, column].
    cur: [[usize; 2]; PANELS],
    /// The first SONG row on screen (it scrolls; 256 rows don't fit).
    song_top: usize,
    /// The panel space plays from (the one you're in, or last were).
    panel: usize,
    /// Piano mode (i in PHRASE): letter keys play and write notes.
    insert: bool,
    /// Piano mode's octave (+/- change it).
    octave: i64,
    /// The instrument a new note gets when the step has none.
    last_inst: i64,
    /// y: one copied cell. Y: a whole copied chain, phrase or table.
    clip: Option<Clip>,
    block: Option<Block>,
    /// The status line's message (what just happened).
    message: String,
    /// Changed since the last save (a ● in SONG's title).
    pub dirty: bool,
    /// The engine and audio output, opened on the first sound, closed after 30 s of silence.
    audio: Option<Audio>,
    /// Hex entry: (panel, row, col, when): a second digit within 2 s makes a byte.
    typed: Option<(usize, usize, usize, Instant)>,
    /// The `:` line, while it's open.
    cmdline: Option<Input>,
    /// The ctrl+o file browser, while it's open.
    browser: Option<crate::channels::creative::browser::Browser>,
    /// Where songs are saved (`tracker_dir` in config.toml).
    dir: PathBuf,
    /// The audio sample rate (`tracker_rate`, 44100 by default).
    rate: u32,
    /// Each panel's inside, from the last draw: mouse clicks find their cell with it.
    inner: [Rect; PANELS],
    /// The song open last time has been reopened (done once, on first draw).
    loaded_last: bool,
}

impl TrackerView {
    pub fn new(cfg: &crate::os::config::Config) -> Self {
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
            dir: crate::os::config::expand(&cfg.tracker_dir),
            rate: if cfg.tracker_rate > 0 { cfg.tracker_rate as u32 } else { 44100 },
            inner: [Rect::default(); PANELS],
            loaded_last: false,
        }
    }

    /// Where the last opened song's path is kept, so TRACKER reopens it next time.
    fn last_file() -> PathBuf {
        crate::os::config::cache_dir().join("tracker-last")
    }

    /// Write the open song's path to `last_file`.
    fn remember(&self) {
        if let Some(p) = &self.song.path {
            let _ = std::fs::create_dir_all(crate::os::config::cache_dir());
            let _ = std::fs::write(Self::last_file(), p.display().to_string());
        }
    }

    /// A name typed in `:w name` or `:e name` → a full path: in `tracker_dir` unless it's
    /// absolute, with `suffix` added if it has no extension.
    fn path(&self, name: &str, suffix: &str) -> PathBuf {
        let p = crate::os::config::expand(name);
        let mut p = if p.is_absolute() { p } else { self.dir.join(p) };
        if !suffix.is_empty() && p.extension().is_none() {
            p.set_extension(suffix);
        }
        p
    }

    // ── context: each panel follows the one before it ────────────────────
    // SONG's cursor picks a chain, CHAIN's a phrase, PHRASE's step an instrument, which picks
    // a table and a wave. These find what each panel shows right now.

    /// The track (column) SONG's cursor is in.
    fn track(&self) -> usize {
        self.cur[SONG][1].min(self.song.tracks.saturating_sub(1))
    }

    /// The chain under SONG's cursor (None: an empty cell).
    fn chain_id(&self) -> Option<i64> {
        self.song.rows[self.cur[SONG][0]][self.track()]
    }

    /// The phrase under CHAIN's cursor.
    fn phrase_id(&self) -> Option<i64> {
        let chain = self.song.chains.get(&self.chain_id()?)?;
        chain.get(self.cur[CHAIN][0])?.phrase
    }

    /// The instrument of the step under PHRASE's cursor, or the last one used.
    fn inst_id(&self) -> i64 {
        self.phrase_id()
            .and_then(|p| self.song.phrases.get(&p))
            .and_then(|p| p.get(self.cur[PHRASE][0]))
            .and_then(|s| s.inst)
            .unwrap_or(self.last_inst)
    }

    /// That instrument's table.
    fn table_id(&self) -> Option<i64> {
        self.song.instruments.get(&self.inst_id()).map_or(Instrument::default().table, |i| i.table)
    }

    /// That instrument's wave (only "wave" instruments have one; 0 otherwise).
    fn wave_id(&self) -> i64 {
        self.song.instruments.get(&self.inst_id()).filter(|i| i.kind == "wave").map_or(0, |i| i.width)
    }

    // ── audio ────────────────────────────────────────────────────────────

    /// Open the engine and audio output if they aren't open, and give the engine the song as
    /// it is now. Called before anything makes a sound.
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

    /// Do something with the engine (it's shared with the audio thread, so it's locked for the
    /// moment). None when audio isn't open.
    fn with_engine<T>(&self, f: impl FnOnce(&mut Engine) -> T) -> Option<T> {
        let a = self.audio.as_ref()?;
        let mut e = a.engine.lock().ok()?;
        Some(f(&mut e))
    }

    /// Give the engine the edited song, so changes are heard while it plays.
    fn sync(&self) {
        let song = &self.song;
        self.with_engine(|e| e.song = song.clone());
    }

    /// Is anything playing?
    fn playing(&self) -> bool {
        self.with_engine(|e| e.playing()).unwrap_or(false)
    }

    /// Copy what drawing needs out of the engine in one go (see `Snap`).
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

    /// space: stop if playing; otherwise play what you're looking at: the song from SONG's
    /// row, the chain in CHAIN, the phrase elsewhere.
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

    /// Play one note so you hear what you just wrote (not while the song plays).
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

    /// Call before every edit: remembers the song for undo and marks it unsaved.
    fn push(&mut self) {
        self.undo_stack.push(self.song.clone());
        if self.undo_stack.len() > 100 {
            self.undo_stack.remove(0);
        }
        self.redo_stack.clear();
        self.dirty = true;
    }

    /// u (`back`) undoes the last edit; ctrl+r (`!back`) redoes it. The file path stays.
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
}

impl Channel for TrackerView {
    fn id(&self) -> &'static str {
        ID
    }

    fn tab(&self) -> &'static str {
        "CREATIVE"
    }

    /// Six panels: SONG, CHAIN, PHRASE across, INSTRUMENT/TABLE/WAVE stacked on the right;
    /// the status line under them, and the `:` line and the browser when they're open.
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
        // The panel space plays from follows the keyboard.
        match view.focus {
            crate::os::nav::Focus::Inside(i) | crate::os::nav::Focus::Nav(Some(i)) if i < PANELS => self.panel = i,
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

    /// The `:` line and the browser take every key while they're open.
    fn captures_keys(&self) -> bool {
        self.cmdline.is_some() || self.browser.is_some()
    }

    /// Keys go to the browser, then the `:` line, then the panel the keys are inside.
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
                crate::channels::creative::browser::Answer::Open => {}
                crate::channels::creative::browser::Answer::Cancel => self.browser = None,
                crate::channels::creative::browser::Answer::Chosen(choice) => {
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

    /// Keys that work in nav mode too: ctrl+o open, ctrl+s save, `:` commands, space play.
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

    /// Click a cell to put the cursor there; the wheel moves up and down.
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

    /// Redraw often while playing; otherwise wake once when it's time to close the audio.
    fn next_wake(&self) -> Option<Instant> {
        let a = self.audio.as_ref()?;
        Some(if self.playing() { Instant::now() + FRAME } else { a.last_sound + IDLE_RELEASE })
    }

    /// Close the audio output and MIDI after 30 s of silence (the deck has one sound device).
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
        let cfg =
            crate::os::config::parse("url = \"http://h:7000\"\ntoken = \"ody_x\"", std::path::Path::new("test.toml"))
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
        let pal = crate::os::palette::Palette::base();
        let text: Vec<String> = song_card(&path, 40, 20, &pal).iter().map(|l| l.to_string()).collect();
        assert!(text[1].contains("first light"));
        assert!(text[3].contains("3 chains · 3 phrases"));
        assert!(text.iter().any(|l| l.contains("00 00 01 02 --")));
        let _ = std::fs::remove_dir_all(dir);
    }
}
