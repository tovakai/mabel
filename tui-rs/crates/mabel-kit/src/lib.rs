//! mabel-kit: what a DreamTape (a MABEL-OS game, shipped as a `.dtape`) is written against.
//!
//! ```ignore
//! use mabel_kit::*;
//!
//! struct Game { x: f32, hero: Sprite }
//!
//! impl Tape for Game {
//!     fn init() -> Self {
//!         music("theme");                               // music/theme.mtk, from TRACKER
//!         Game { x: 10.0, hero: sprite("hero") }       // art/hero.mpx, from PIXEL
//!     }
//!     fn update(&mut self, dt: f32) {
//!         if btn(Btn::Right) { self.x += 40.0 * dt; }
//!         if btnp(Btn::A) { sfx("blips", 0); }          // phrase 00 of music/blips.mtk, once
//!     }
//!     fn draw(&self) {
//!         cls(BG);
//!         spr(&self.hero, self.x as i32, 20);
//!         print(1, 1, "hello from a tape", AMBER);
//!     }
//! }
//!
//! tape!(Game);
//! ```
//!
//! Built for `wasm32-unknown-unknown`, every function here is an import that the
//! TAPES channel (MABEL-OS) provides. Built natively (`cargo run --release`), kit draws
//! the same game in any terminal by itself: the standalone build.
//!
//! **Coordinates:** pixels, (0, 0) top-left. A terminal cell is 1 pixel wide and
//! 2 pixels high, so `print` uses cells: column = x, row = y / 2.

/// A colour as 0xRRGGBB.
pub type Color = u32;

/// Build a colour from red, green and blue (0-255 each).
pub const fn rgb(r: u8, g: u8, b: u8) -> Color {
    (r as u32) << 16 | (g as u32) << 8 | b as u32
}

pub const BLACK: Color = 0x000000;
pub const WHITE: Color = 0xffffff;
/// MABEL-OS's amber, and its dimmer shade.
pub const AMBER: Color = rgb(0xff, 0xb0, 0x00);
pub const AMBER_DIM: Color = rgb(0x8a, 0x5e, 0x00);
/// MABEL-OS's dark brown background.
pub const BG: Color = rgb(0x1b, 0x15, 0x10);
/// In `blit` pictures, a pixel of this value is see-through.
pub const TRANSPARENT: Color = 0xff00_0000;

/// A game. TAPES calls `init` once, then `update` and `draw` every frame.
pub trait Tape {
    fn init() -> Self;
    /// Move the world forward by `dt` seconds.
    fn update(&mut self, _dt: f32) {}
    /// Draw the world. Don't change state here: `update` is for that.
    fn draw(&self);
}

/// The buttons. Keys: arrows / h j k l / w a s d, z x c v for A B X Y,
/// enter for START, backspace for SELECT (a tape can change them in tape.toml).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Btn {
    Left = 0,
    Right = 1,
    Up = 2,
    Down = 3,
    A = 4,
    B = 5,
    X = 6,
    Y = 7,
    Start = 8,
    Select = 9,
}

/// A picture from PIXEL, loaded with [`sprite`].
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Sprite {
    id: i32,
    /// Size in pixels, and how many animation frames it has (0 if it couldn't be loaded).
    pub w: i32,
    pub h: i32,
    pub frames: i32,
}

// ---- drawing --------------------------------------------------------------------------

/// Fill the whole screen with one colour.
pub fn cls(c: Color) {
    sys::cls(c)
}

/// Set one pixel. Off-screen pixels are ignored.
pub fn pset(x: i32, y: i32, c: Color) {
    sys::pset(x, y, c)
}

/// The colour of one pixel (black off-screen).
pub fn pget(x: i32, y: i32) -> Color {
    sys::pget(x, y)
}

/// A straight line, both ends included.
pub fn line(x0: i32, y0: i32, x1: i32, y1: i32, c: Color) {
    sys::line(x0, y0, x1, y1, c)
}

/// A rectangle's outline, `w` wide and `h` high, top-left corner at (x, y).
pub fn rect(x: i32, y: i32, w: i32, h: i32, c: Color) {
    sys::rect(x, y, w, h, c)
}

/// A filled rectangle, `w` wide and `h` high, top-left corner at (x, y).
pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: Color) {
    sys::rectfill(x, y, w, h, c)
}

/// A circle's outline around (x, y) with radius `r`.
pub fn circ(x: i32, y: i32, r: i32, c: Color) {
    sys::circ(x, y, r, c)
}

/// A filled circle around (x, y) with radius `r`.
pub fn circfill(x: i32, y: i32, r: i32, c: Color) {
    sys::circfill(x, y, r, c)
}

/// Copy a whole picture (`w`×`h` colours, row by row) to (x, y) in one call.
/// Much faster than one `pset` per pixel. `TRANSPARENT` pixels are skipped.
pub fn blit(x: i32, y: i32, w: u32, h: u32, pixels: &[Color]) {
    assert!(pixels.len() >= (w * h) as usize, "blit: the picture has fewer than w*h pixels");
    sys::blit(x, y, w, h, pixels)
}

/// Text as real terminal characters, at cell `col`, `row` (row = y / 2). Crisp, but
/// snapped to cells, and drawn over the pixels after `draw` returns.
pub fn print(col: i32, row: i32, text: &str, c: Color) {
    sys::print(col, row, text, c)
}

/// Text in the little pixel font (3×5 letters, 4 pixels apart, lines 6 apart), with its
/// top-left corner at any pixel. `\n` starts a new line. Lowercase draws as small capitals.
pub fn pprint(x: i32, y: i32, text: &str, c: Color) {
    sys::pprint(x, y, text, c)
}

/// How wide `pprint` draws `text`, in pixels (for centring: `(width() - text_width(s)) / 2`).
pub fn text_width(text: &str) -> i32 {
    text.lines().map(|l| l.chars().count() as i32 * 4 - 1).max().unwrap_or(0).max(0)
}

// ---- saves ----------------------------------------------------------------------------

/// Remember `value` under `key`, even after the tape stops: high scores, levels, settings.
/// Numbers too: `save("best", &score.to_string())`. All of a tape's saves together are at
/// most 64 KB.
pub fn save(key: &str, value: &str) {
    sys::save(key, value)
}

/// What was saved under `key`, if anything. For a number:
/// `let best: u32 = load("best").and_then(|s| s.parse().ok()).unwrap_or(0);`
pub fn load(key: &str) -> Option<String> {
    sys::load(key)
}

// ---- sprites --------------------------------------------------------------------------

/// Load `art/<name>.mpx` (or `.png`). Do this once, in `init`, and keep the Sprite.
/// If the file is missing, the Sprite draws nothing and the console says why.
pub fn sprite(name: &str) -> Sprite {
    sys::sprite(name)
}

/// Draw a sprite's first frame with its top-left corner at (x, y).
pub fn spr(s: &Sprite, x: i32, y: i32) {
    sys::spr(s.id, 0, x, y, 0)
}

/// Draw animation frame `frame` (it wraps round), optionally mirrored.
pub fn spr_ex(s: &Sprite, frame: i32, x: i32, y: i32, flip_x: bool, flip_y: bool) {
    sys::spr(s.id, frame, x, y, flip_x as u32 | (flip_y as u32) << 1)
}

// ---- input ----------------------------------------------------------------------------

/// Is the button held down this frame?
pub fn btn(b: Btn) -> bool {
    sys::btn(b as i32)
}

/// Was the button pressed since the last frame? (Once per press: menus, jumps, shots.)
pub fn btnp(b: Btn) -> bool {
    sys::btnp(b as i32)
}

// ---- sound ----------------------------------------------------------------------------

/// Play `music/<name>.mtk` from the start, looping. Replaces the music playing.
pub fn music(name: &str) {
    sys::music(name)
}

/// Stop the music.
pub fn music_stop() {
    sys::music_stop()
}

/// Play phrase `phrase` of `music/<name>.mtk` once, over the music: a sound effect
/// is just a short phrase made in TRACKER. Phrase numbers are TRACKER's (hex: 0x0A).
pub fn sfx(name: &str, phrase: i32) {
    sys::sfx(name, phrase)
}

// ---- screen, time, debugging ----------------------------------------------------------

/// Screen width in pixels.
pub fn width() -> i32 {
    sys::width()
}

/// Screen height in pixels (two per terminal row).
pub fn height() -> i32 {
    sys::height()
}

/// Seconds of play since the tape started (paused time doesn't count).
pub fn time() -> f32 {
    sys::time()
}

/// How many frames have been drawn.
pub fn frame() -> u32 {
    sys::frame()
}

/// Write a line to the console (for debugging; shown under the tape in TAPES).
pub fn log(msg: &str) {
    sys::log(msg)
}

// ---- inside a wasm tape: imports from TAPES ---------------------------------------------

#[cfg(target_arch = "wasm32")]
mod sys {
    use super::{Color, Sprite};

    mod ffi {
        #[link(wasm_import_module = "mabel")]
        unsafe extern "C" {
            pub fn cls(c: u32);
            pub fn pset(x: i32, y: i32, c: u32);
            pub fn pget(x: i32, y: i32) -> u32;
            pub fn line(x0: i32, y0: i32, x1: i32, y1: i32, c: u32);
            pub fn rect(x: i32, y: i32, w: i32, h: i32, c: u32);
            pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: u32);
            pub fn circ(x: i32, y: i32, r: i32, c: u32);
            pub fn circfill(x: i32, y: i32, r: i32, c: u32);
            pub fn blit(x: i32, y: i32, w: u32, h: u32, ptr: u32);
            pub fn print(col: i32, row: i32, ptr: u32, len: u32, c: u32);
            pub fn pprint(x: i32, y: i32, ptr: u32, len: u32, c: u32);
            pub fn save(kp: u32, kl: u32, vp: u32, vl: u32) -> i32;
            pub fn load(kp: u32, kl: u32, out: u32, cap: u32) -> i32;
            pub fn sprite(ptr: u32, len: u32) -> i32;
            pub fn sprite_w(id: i32) -> i32;
            pub fn sprite_h(id: i32) -> i32;
            pub fn sprite_frames(id: i32) -> i32;
            pub fn spr(id: i32, frame: i32, x: i32, y: i32, flags: u32);
            pub fn btn(b: i32) -> i32;
            pub fn btnp(b: i32) -> i32;
            pub fn music(ptr: u32, len: u32);
            pub fn music_stop();
            pub fn sfx(ptr: u32, len: u32, phrase: i32);
            pub fn width() -> i32;
            pub fn height() -> i32;
            pub fn time() -> f32;
            pub fn frame() -> u32;
            pub fn log(ptr: u32, len: u32);
        }
    }

    // The host only reads what it's given (strings by pointer and length), so these are safe.
    pub fn cls(c: Color) {
        unsafe { ffi::cls(c) }
    }
    pub fn pset(x: i32, y: i32, c: Color) {
        unsafe { ffi::pset(x, y, c) }
    }
    pub fn pget(x: i32, y: i32) -> Color {
        unsafe { ffi::pget(x, y) }
    }
    pub fn line(x0: i32, y0: i32, x1: i32, y1: i32, c: Color) {
        unsafe { ffi::line(x0, y0, x1, y1, c) }
    }
    pub fn rect(x: i32, y: i32, w: i32, h: i32, c: Color) {
        unsafe { ffi::rect(x, y, w, h, c) }
    }
    pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: Color) {
        unsafe { ffi::rectfill(x, y, w, h, c) }
    }
    pub fn circ(x: i32, y: i32, r: i32, c: Color) {
        unsafe { ffi::circ(x, y, r, c) }
    }
    pub fn circfill(x: i32, y: i32, r: i32, c: Color) {
        unsafe { ffi::circfill(x, y, r, c) }
    }
    pub fn blit(x: i32, y: i32, w: u32, h: u32, pixels: &[Color]) {
        unsafe { ffi::blit(x, y, w, h, pixels.as_ptr() as u32) }
    }
    pub fn print(col: i32, row: i32, text: &str, c: Color) {
        unsafe { ffi::print(col, row, text.as_ptr() as u32, text.len() as u32, c) }
    }
    pub fn pprint(x: i32, y: i32, text: &str, c: Color) {
        unsafe { ffi::pprint(x, y, text.as_ptr() as u32, text.len() as u32, c) }
    }
    pub fn save(key: &str, value: &str) {
        unsafe { ffi::save(key.as_ptr() as u32, key.len() as u32, value.as_ptr() as u32, value.len() as u32) };
    }
    pub fn load(key: &str) -> Option<String> {
        let mut buf = vec![0u8; 256];
        loop {
            let n =
                unsafe { ffi::load(key.as_ptr() as u32, key.len() as u32, buf.as_mut_ptr() as u32, buf.len() as u32) };
            if n < 0 {
                return None;
            }
            if n as usize <= buf.len() {
                buf.truncate(n as usize);
                return Some(String::from_utf8_lossy(&buf).into_owned());
            }
            buf = vec![0u8; n as usize]; // too small: the host said how big, ask again
        }
    }
    pub fn sprite(name: &str) -> Sprite {
        unsafe {
            let id = ffi::sprite(name.as_ptr() as u32, name.len() as u32);
            Sprite { id, w: ffi::sprite_w(id), h: ffi::sprite_h(id), frames: ffi::sprite_frames(id) }
        }
    }
    pub fn spr(id: i32, frame: i32, x: i32, y: i32, flags: u32) {
        unsafe { ffi::spr(id, frame, x, y, flags) }
    }
    pub fn btn(b: i32) -> bool {
        unsafe { ffi::btn(b) != 0 }
    }
    pub fn btnp(b: i32) -> bool {
        unsafe { ffi::btnp(b) != 0 }
    }
    pub fn music(name: &str) {
        unsafe { ffi::music(name.as_ptr() as u32, name.len() as u32) }
    }
    pub fn music_stop() {
        unsafe { ffi::music_stop() }
    }
    pub fn sfx(name: &str, phrase: i32) {
        unsafe { ffi::sfx(name.as_ptr() as u32, name.len() as u32, phrase) }
    }
    pub fn width() -> i32 {
        unsafe { ffi::width() }
    }
    pub fn height() -> i32 {
        unsafe { ffi::height() }
    }
    pub fn time() -> f32 {
        unsafe { ffi::time() }
    }
    pub fn frame() -> u32 {
        unsafe { ffi::frame() }
    }
    pub fn log(msg: &str) {
        unsafe { ffi::log(msg.as_ptr() as u32, msg.len() as u32) }
    }
}

// ---- a native build: the standalone runner ---------------------------------------------
// `cargo run --release` in a tape's folder: the same game, drawn by kit in whatever terminal
// you're in, with sound, buttons and saves, no MABEL-OS needed.

#[cfg(not(target_arch = "wasm32"))]
mod sys {
    use super::{Color, Sprite};
    use mabel_console::input::Buttons;
    use mabel_console::saves::Saves;
    use mabel_console::sound::Sound;
    use mabel_console::{Frame, Sprite as Picture, Text};
    use std::cell::RefCell;
    use std::path::PathBuf;

    pub(crate) struct Native {
        pub frame: Frame,
        pub frame_no: u32,
        pub elapsed: f32,
        pub buttons: Buttons,
        pub texts: Vec<Text>,
        pub log: Vec<String>,
        pub root: PathBuf,
        sprites: Vec<Picture>,
        sound: Option<Sound>,
        saves: Option<Saves>,
    }

    thread_local! {
        static NATIVE: RefCell<Native> = RefCell::new(Native {
            frame: Frame::new(128, 72),
            frame_no: 0,
            elapsed: 0.0,
            buttons: Buttons::default(),
            texts: vec![],
            log: vec![],
            root: std::env::current_dir().unwrap_or_default(),
            sprites: vec![],
            sound: None,
            saves: None,
        });
    }

    pub(crate) fn with<R>(f: impl FnOnce(&mut Native) -> R) -> R {
        NATIVE.with(|n| f(&mut n.borrow_mut()))
    }

    impl Native {
        fn sound(&mut self) -> &mut Sound {
            let root = &self.root;
            self.sound.get_or_insert_with(|| Sound::new(root, 44100))
        }
        fn saves(&mut self) -> &mut Saves {
            let key = self.root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            self.saves.get_or_insert_with(|| Saves::open(&key))
        }
        pub fn stop_sound(&mut self) {
            self.sound = None;
        }
    }

    pub fn cls(c: Color) {
        with(|n| n.frame.cls(c))
    }
    pub fn pset(x: i32, y: i32, c: Color) {
        with(|n| n.frame.pset(x, y, c))
    }
    pub fn pget(x: i32, y: i32) -> Color {
        with(|n| n.frame.pget(x, y))
    }
    pub fn line(x0: i32, y0: i32, x1: i32, y1: i32, c: Color) {
        with(|n| n.frame.line(x0, y0, x1, y1, c))
    }
    pub fn rect(x: i32, y: i32, w: i32, h: i32, c: Color) {
        with(|n| n.frame.rect(x, y, w, h, c))
    }
    pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: Color) {
        with(|n| n.frame.rectfill(x, y, w, h, c))
    }
    pub fn circ(x: i32, y: i32, r: i32, c: Color) {
        with(|n| n.frame.circ(x, y, r, c))
    }
    pub fn circfill(x: i32, y: i32, r: i32, c: Color) {
        with(|n| n.frame.circfill(x, y, r, c))
    }
    pub fn blit(x: i32, y: i32, w: u32, h: u32, pixels: &[Color]) {
        with(|n| n.frame.blit(x, y, w as usize, h as usize, pixels))
    }
    pub fn print(col: i32, row: i32, text: &str, color: Color) {
        with(|n| n.texts.push(Text { col, row, text: text.to_string(), color }))
    }
    pub fn pprint(x: i32, y: i32, text: &str, c: Color) {
        with(|n| mabel_console::font::pprint(&mut n.frame, x, y, text, c))
    }
    pub fn save(key: &str, value: &str) {
        with(|n| {
            if let Err(e) = n.saves().set(key, value) {
                n.log.push(e);
            }
        })
    }
    pub fn load(key: &str) -> Option<String> {
        with(|n| n.saves().get(key).map(str::to_string))
    }
    pub fn sprite(name: &str) -> Sprite {
        with(|n| {
            let art = n.root.join("art");
            let path =
                [art.join(format!("{name}.mpx")), art.join(format!("{name}.png"))].into_iter().find(|p| p.exists());
            match path.map(|p| Picture::load(&p)) {
                Some(Ok(p)) => {
                    let s = Sprite {
                        id: n.sprites.len() as i32,
                        w: p.w as i32,
                        h: p.h as i32,
                        frames: p.frames.len() as i32,
                    };
                    n.sprites.push(p);
                    s
                }
                other => {
                    let why = match other {
                        Some(Err(e)) => e,
                        _ => format!("no art/{name}.mpx or art/{name}.png"),
                    };
                    n.log.push(format!("sprite {name}: {why}"));
                    Sprite { id: -1, w: 0, h: 0, frames: 0 }
                }
            }
        })
    }
    pub fn spr(id: i32, frame: i32, x: i32, y: i32, flags: u32) {
        with(|n| {
            if let Some(p) = usize::try_from(id).ok().and_then(|i| n.sprites.get(i)) {
                p.draw(&mut n.frame, frame.max(0) as usize, x, y, flags & 1 != 0, flags & 2 != 0);
            }
        })
    }
    pub fn btn(b: i32) -> bool {
        with(|n| n.buttons.btn(b as usize))
    }
    pub fn btnp(b: i32) -> bool {
        with(|n| n.buttons.btnp(b as usize))
    }
    pub fn music(name: &str) {
        with(|n| {
            if let Err(e) = n.sound().music(name) {
                n.log.push(e);
            }
        })
    }
    pub fn music_stop() {
        with(|n| {
            if let Some(s) = &mut n.sound {
                s.music_stop();
            }
        })
    }
    pub fn sfx(name: &str, phrase: i32) {
        with(|n| {
            if let Err(e) = n.sound().sfx(name, phrase as i64) {
                n.log.push(e);
            }
        })
    }
    pub fn width() -> i32 {
        with(|n| n.frame.w as i32)
    }
    pub fn height() -> i32 {
        with(|n| n.frame.h as i32)
    }
    pub fn time() -> f32 {
        with(|n| n.elapsed)
    }
    pub fn frame() -> u32 {
        with(|n| n.frame_no)
    }
    pub fn log(msg: &str) {
        with(|n| n.log.push(msg.to_string()))
    }
}

/// The standalone runner: `tape!` makes `main()` call this in a native build.
#[cfg(not(target_arch = "wasm32"))]
pub mod native {
    use std::time::{Duration, Instant};

    use mabel_console::input::{Keymap, key_name};
    use mabel_console::ratatui;
    use mabel_console::ratatui::crossterm::event::{
        self, Event, KeyCode, KeyEventKind, KeyModifiers, KeyboardEnhancementFlags, PopKeyboardEnhancementFlags,
        PushKeyboardEnhancementFlags,
    };
    use mabel_console::ratatui::crossterm::execute;
    use mabel_console::tapefile::TapeInfo;

    use super::sys::with;

    /// Play the tape in this terminal until ctrl+q (or ctrl+c, ctrl+]).
    pub fn run<T: super::Tape>() {
        let root = std::env::current_dir().unwrap_or_default();
        let info = match TapeInfo::read(&root) {
            Ok(i) => i,
            Err(e) => {
                eprintln!("{e}");
                return;
            }
        };
        let keymap = Keymap::new(&info.keys);
        let period = Duration::from_secs_f64(1.0 / info.fps as f64);

        let mut term = ratatui::init(); // restores the terminal on a panic too
        let kitty = ratatui::crossterm::terminal::supports_keyboard_enhancement().unwrap_or(false);
        if kitty {
            let flags =
                KeyboardEnhancementFlags::DISAMBIGUATE_ESCAPE_CODES | KeyboardEnhancementFlags::REPORT_EVENT_TYPES;
            let _ = execute!(std::io::stdout(), PushKeyboardEnhancementFlags(flags));
        }
        let mut place = mabel_console::place(info.screen, term.get_frame().area());
        with(|n| n.frame.resize(place.w, place.h));
        let mut game = T::init();

        let (mut next, mut last) = (Instant::now(), Instant::now());
        'play: loop {
            // Keys until the next frame is due.
            while let Ok(true) = event::poll(next.saturating_duration_since(Instant::now())) {
                match event::read() {
                    Ok(Event::Key(k)) => {
                        let ctrl = k.modifiers.contains(KeyModifiers::CONTROL);
                        if ctrl && matches!(k.code, KeyCode::Char('q' | 'c' | ']' | '5')) {
                            break 'play;
                        }
                        if let Some(b) = key_name(&k).and_then(|name| keymap.button(&name)) {
                            with(|n| match k.kind {
                                KeyEventKind::Release => n.buttons.release(b),
                                kind => n.buttons.press(b, kind == KeyEventKind::Repeat, Instant::now()),
                            });
                        }
                    }
                    Ok(Event::Resize(..)) => {
                        let _ = term.autoresize();
                        place = mabel_console::place(info.screen, term.get_frame().area());
                        with(|n| n.frame.resize(place.w, place.h));
                    }
                    _ => {}
                }
            }
            let now = Instant::now();
            let dt = now.duration_since(last).as_secs_f32().min(0.25);
            (last, next) = (now, (next + period).max(now));
            with(|n| {
                n.buttons.begin_frame(now);
                n.texts.clear();
                n.elapsed += dt;
            });
            game.update(dt);
            game.draw();
            let _ = term.draw(|f| {
                with(|n| mabel_console::render(&n.frame, &n.texts, f.buffer_mut(), place.cells, place.scale));
            });
            with(|n| n.frame_no = n.frame_no.wrapping_add(1));
        }
        if kitty {
            let _ = execute!(std::io::stdout(), PopKeyboardEnhancementFlags);
        }
        ratatui::restore();
        with(|n| {
            n.stop_sound();
            for line in n.log.drain(..) {
                eprintln!("{line}");
            }
        });
    }
}

// ---- the tape! macro --------------------------------------------------------------------

/// Where `tape!` keeps the game between frames. Not for use in game code.
#[doc(hidden)]
pub struct Slot<T>(core::cell::UnsafeCell<Option<T>>);

// A wasm tape runs on one thread: TAPES never calls into it from two at once.
unsafe impl<T> Sync for Slot<T> {}

impl<T> Slot<T> {
    pub const fn new() -> Self {
        Slot(core::cell::UnsafeCell::new(None))
    }
    /// Run `f` with the game.
    ///
    /// # Safety
    /// Only from the single tape thread, and never from inside another `with`.
    pub unsafe fn with<R>(&self, f: impl FnOnce(&mut Option<T>) -> R) -> R {
        f(unsafe { &mut *self.0.get() })
    }
}

impl<T> Default for Slot<T> {
    fn default() -> Self {
        Self::new()
    }
}

/// A panic inside a tape: its message goes to the console before the tape stops, so the
/// error card in TAPES can say what happened ("panic: index out of bounds…").
#[doc(hidden)]
pub fn install_panic_hook() {
    std::panic::set_hook(Box::new(|info| log(&format!("panic: {info}"))));
}

/// Turn a type that implements [`Tape`] into a game: the wasm exports TAPES calls
/// (`init`, `update`, `draw`), or `main()` in a native build.
#[macro_export]
macro_rules! tape {
    ($game:ty) => {
        #[cfg(target_arch = "wasm32")]
        mod __mabel_tape {
            use super::*;
            static GAME: $crate::Slot<$game> = $crate::Slot::new();

            #[unsafe(no_mangle)]
            pub extern "C" fn init() {
                $crate::install_panic_hook();
                let game = <$game as $crate::Tape>::init();
                unsafe { GAME.with(|g| *g = Some(game)) }
            }
            #[unsafe(no_mangle)]
            pub extern "C" fn update(dt: f32) {
                unsafe { GAME.with(|g| g.as_mut().map(|g| $crate::Tape::update(g, dt))) };
            }
            #[unsafe(no_mangle)]
            pub extern "C" fn draw() {
                unsafe { GAME.with(|g| g.as_ref().map(|g| $crate::Tape::draw(g))) };
            }
        }

        // A tape is a normal program (src/main.rs): TAPES never calls this one.
        #[cfg(target_arch = "wasm32")]
        pub fn main() {}

        #[cfg(not(target_arch = "wasm32"))]
        pub fn main() {
            $crate::native::run::<$game>()
        }
    };
}
