//! mabel-kit: what a DreamTape (a MABEL-OS game, shipped as a `.dtape`) is written against.
//!
//! ```ignore
//! use mabel_kit::*;
//!
//! struct Game { x: f32 }
//!
//! impl Tape for Game {
//!     fn init() -> Self { Game { x: 10.0 } }
//!     fn update(&mut self, dt: f32) { self.x += 20.0 * dt; }
//!     fn draw(&self) {
//!         cls(rgb(0x1b, 0x15, 0x10));
//!         rectfill(self.x as i32, 10, 8, 8, AMBER);
//!     }
//! }
//!
//! tape!(Game);
//! ```
//!
//! Built for `wasm32-unknown-unknown`, every function here is an import that
//! PROGRAM (MABEL-OS) provides. Built natively, the same functions draw into
//! kit's own screen, which is how a game will run outside MABEL-OS later.
//!
//! Milestone 0 (2026-11-19): drawing, time and `log` only. Input, sprites,
//! text and sound come in milestone 1.

/// A colour as 0xRRGGBB.
pub type Color = u32;

/// Build a colour from red, green and blue (0-255 each).
pub const fn rgb(r: u8, g: u8, b: u8) -> Color {
    (r as u32) << 16 | (g as u32) << 8 | b as u32
}

/// MABEL-OS's amber.
pub const AMBER: Color = rgb(0xff, 0xb0, 0x00);
/// In `blit` pictures, a pixel of this value is see-through.
pub const TRANSPARENT: Color = 0xff00_0000;

/// A game. PROGRAM calls `init` once, then `update` and `draw` every frame.
pub trait Tape {
    fn init() -> Self;
    /// Move the world forward by `dt` seconds.
    fn update(&mut self, _dt: f32) {}
    /// Draw the world. Don't change state here: `update` is for that.
    fn draw(&self);
}

// ---- the functions a tape calls ---------------------------------------------------------

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

/// A filled rectangle `w` wide and `h` high, with its top-left corner at (x, y).
pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: Color) {
    sys::rectfill(x, y, w, h, c)
}

/// Copy a whole picture (`w`×`h` colours, row by row) to (x, y) in one call.
/// Much faster than one `pset` per pixel. `TRANSPARENT` pixels are skipped.
pub fn blit(x: i32, y: i32, w: u32, h: u32, pixels: &[Color]) {
    assert!(pixels.len() >= (w * h) as usize, "blit: the picture has fewer than w*h pixels");
    sys::blit(x, y, w, h, pixels)
}

/// Screen width in pixels.
pub fn width() -> i32 {
    sys::width()
}

/// Screen height in pixels (two per terminal row).
pub fn height() -> i32 {
    sys::height()
}

/// Seconds since the tape started.
pub fn time() -> f32 {
    sys::time()
}

/// How many frames have been drawn.
pub fn frame() -> u32 {
    sys::frame()
}

/// Write a line to PROGRAM's console (for debugging).
pub fn log(msg: &str) {
    sys::log(msg)
}

// ---- inside a wasm tape: imports from PROGRAM -------------------------------------------

#[cfg(target_arch = "wasm32")]
mod sys {
    use super::Color;

    mod ffi {
        #[link(wasm_import_module = "mabel")]
        unsafe extern "C" {
            pub fn cls(c: u32);
            pub fn pset(x: i32, y: i32, c: u32);
            pub fn pget(x: i32, y: i32) -> u32;
            pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: u32);
            pub fn blit(x: i32, y: i32, w: u32, h: u32, ptr: u32);
            pub fn width() -> i32;
            pub fn height() -> i32;
            pub fn time() -> f32;
            pub fn frame() -> u32;
            pub fn log(ptr: u32, len: u32);
        }
    }

    // The host only reads its arguments, so these calls are safe to wrap.
    pub fn cls(c: Color) {
        unsafe { ffi::cls(c) }
    }
    pub fn pset(x: i32, y: i32, c: Color) {
        unsafe { ffi::pset(x, y, c) }
    }
    pub fn pget(x: i32, y: i32) -> Color {
        unsafe { ffi::pget(x, y) }
    }
    pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: Color) {
        unsafe { ffi::rectfill(x, y, w, h, c) }
    }
    pub fn blit(x: i32, y: i32, w: u32, h: u32, pixels: &[Color]) {
        unsafe { ffi::blit(x, y, w, h, pixels.as_ptr() as u32) }
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

// ---- a native build: kit keeps its own screen -------------------------------------------

#[cfg(not(target_arch = "wasm32"))]
mod sys {
    use super::Color;
    use mabel_console::Frame;
    use std::cell::RefCell;
    use std::time::Instant;

    struct Native {
        frame: Frame,
        frame_no: u32,
        started: Instant,
    }

    thread_local! {
        static NATIVE: RefCell<Native> =
            RefCell::new(Native { frame: Frame::new(128, 72), frame_no: 0, started: Instant::now() });
    }

    fn with<R>(f: impl FnOnce(&mut Native) -> R) -> R {
        NATIVE.with(|n| f(&mut n.borrow_mut()))
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
    pub fn rectfill(x: i32, y: i32, w: i32, h: i32, c: Color) {
        with(|n| n.frame.rectfill(x, y, w, h, c))
    }
    pub fn blit(x: i32, y: i32, w: u32, h: u32, pixels: &[Color]) {
        with(|n| n.frame.blit(x, y, w as usize, h as usize, pixels))
    }
    pub fn width() -> i32 {
        with(|n| n.frame.w as i32)
    }
    pub fn height() -> i32 {
        with(|n| n.frame.h as i32)
    }
    pub fn time() -> f32 {
        with(|n| n.started.elapsed().as_secs_f32())
    }
    pub fn frame() -> u32 {
        with(|n| n.frame_no)
    }
    pub fn log(msg: &str) {
        eprintln!("{msg}");
    }
}

/// The standalone runner (a game in any terminal) arrives in milestone 2.
#[cfg(not(target_arch = "wasm32"))]
pub mod native {
    pub fn run<C: super::Tape>() {
        eprintln!("mabel-kit: the standalone build comes in milestone 2; build this as a tape for now:");
        eprintln!("  cargo build --release --target wasm32-unknown-unknown");
    }
}

// ---- the tape! macro --------------------------------------------------------------------

/// Where `tape!` keeps the game between frames. Not for use in game code.
#[doc(hidden)]
pub struct Slot<T>(core::cell::UnsafeCell<Option<T>>);

// A wasm tape runs on one thread: PROGRAM never calls into it from two at once.
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

/// Turn a type that implements [`Tape`] into a game: the wasm exports PROGRAM
/// calls (`init`, `update`, `draw`), or `main()` in a native build.
#[macro_export]
macro_rules! tape {
    ($game:ty) => {
        #[cfg(target_arch = "wasm32")]
        mod __mabel_tape {
            use super::*;
            static GAME: $crate::Slot<$game> = $crate::Slot::new();

            #[unsafe(no_mangle)]
            pub extern "C" fn init() {
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

        #[cfg(not(target_arch = "wasm32"))]
        pub fn main() {
            $crate::native::run::<$game>()
        }
    };
}
