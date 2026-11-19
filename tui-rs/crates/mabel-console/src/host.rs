//! Running a wasm DreamTape with wasmi.
//!
//! A tape imports kit's functions from the module `"mabel"` and exports
//! `init()`, `update(dt: f32)` and `draw()`. The imports work on the host's
//! [`Frame`], its buttons, and the tape's own files (`art/`, `music/` under its
//! folder); nothing in a tape can reach anything else.
//!
//! Each frame gets a fuel budget (wasmi counts executed instructions), so a
//! runaway loop ends as an error instead of freezing MABEL-OS.

use std::collections::HashMap;
use std::path::PathBuf;
use std::time::Instant;

use wasmi::{Caller, Config, Engine, Linker, Module, Store, TypedFunc};

use crate::input::Buttons;
use crate::saves::Saves;
use crate::sound::Sound;
use crate::{Frame, Rgb, Sprite, Text};

/// Roughly how many wasm instructions one frame may run.
/// The worst stress test (9,216 pset calls) needs well under 1% of it; a runaway loop
/// stops within a fraction of a second, even on the deck.
pub const FUEL_PER_FRAME: u64 = 50_000_000;
/// The console keeps the newest lines of `log`.
const LOG_KEEP: usize = 200;

/// Where a tape runs.
pub struct Options {
    pub w: usize,
    pub h: usize,
    /// The tape's folder: `art/` and `music/` are read from here.
    pub root: PathBuf,
    /// Sound sample rate.
    pub rate: u32,
    /// Whose saves `save`/`load` use (the tape's folder name).
    pub save_key: String,
}

/// What the tape's imports can see.
pub struct Host {
    pub frame: Frame,
    pub frame_no: u32,
    pub buttons: Buttons,
    pub texts: Vec<Text>,
    /// New `log` lines (and the tape's panic message) since `take_log`.
    pub log: Vec<String>,
    /// Seconds of play (paused time doesn't count).
    elapsed: f32,
    root: PathBuf,
    rate: u32,
    sprites: Vec<Sprite>,
    sprite_ids: HashMap<String, i32>,
    sound: Option<Sound>,
    save_key: String,
    saves: Option<Saves>,
}

impl Host {
    fn saves(&mut self) -> &mut Saves {
        let key = &self.save_key;
        self.saves.get_or_insert_with(|| Saves::open(key))
    }

    fn note(&mut self, line: String) {
        self.log.push(line);
        if self.log.len() > LOG_KEEP {
            self.log.remove(0);
        }
    }

    fn sound(&mut self) -> &mut Sound {
        let (root, rate) = (&self.root, self.rate);
        self.sound.get_or_insert_with(|| Sound::new(root, rate))
    }

    /// `art/<name>.mpx` (or `.png`) as a sprite id; -1 if it can't be read.
    fn sprite(&mut self, name: &str) -> i32 {
        if let Some(id) = self.sprite_ids.get(name) {
            return *id;
        }
        let id = if name.is_empty() || name.contains('/') || name.contains("..") {
            self.note(format!("sprite {name:?}: just the file name, without art/ or .mpx"));
            -1
        } else {
            let art = self.root.join("art");
            let path =
                [art.join(format!("{name}.mpx")), art.join(format!("{name}.png"))].into_iter().find(|p| p.exists());
            match path.map(|p| Sprite::load(&p)) {
                Some(Ok(sprite)) => {
                    self.sprites.push(sprite);
                    self.sprites.len() as i32 - 1
                }
                Some(Err(e)) => {
                    self.note(format!("sprite {name}: {e}"));
                    -1
                }
                None => {
                    self.note(format!("sprite {name}: no art/{name}.mpx or art/{name}.png"));
                    -1
                }
            }
        };
        self.sprite_ids.insert(name.to_string(), id);
        id
    }
}

/// A loaded, running tape.
pub struct Tape {
    store: Store<Host>,
    update: TypedFunc<f32, ()>,
    draw: TypedFunc<(), ()>,
}

fn err(e: impl std::fmt::Display) -> String {
    e.to_string()
}

/// A string from the tape's memory, and the host.
fn text<'a>(caller: &'a mut Caller<'_, Host>, ptr: u32, len: u32) -> Option<(String, &'a mut Host)> {
    let mem = caller.get_export("memory")?.into_memory()?;
    let (data, host) = mem.data_and_store_mut(caller);
    let bytes = data.get(ptr as usize..ptr as usize + len as usize)?;
    Some((String::from_utf8_lossy(bytes).into_owned(), host))
}

macro_rules! wrap {
    ($linker:ident, $name:literal, $f:expr) => {
        $linker.func_wrap("mabel", $name, $f).map_err(err)?;
    };
}

fn link(linker: &mut Linker<Host>) -> Result<(), String> {
    // ── drawing ──
    wrap!(linker, "cls", |mut c: Caller<'_, Host>, col: u32| c.data_mut().frame.cls(col));
    wrap!(linker, "pset", |mut c: Caller<'_, Host>, x: i32, y: i32, col: u32| c.data_mut().frame.pset(x, y, col));
    wrap!(linker, "pget", |c: Caller<'_, Host>, x: i32, y: i32| c.data().frame.pget(x, y));
    wrap!(linker, "rectfill", |mut c: Caller<'_, Host>, x: i32, y: i32, w: i32, h: i32, col: u32| {
        c.data_mut().frame.rectfill(x, y, w, h, col)
    });
    wrap!(linker, "rect", |mut c: Caller<'_, Host>, x: i32, y: i32, w: i32, h: i32, col: u32| {
        c.data_mut().frame.rect(x, y, w, h, col)
    });
    wrap!(linker, "line", |mut c: Caller<'_, Host>, x0: i32, y0: i32, x1: i32, y1: i32, col: u32| {
        c.data_mut().frame.line(x0, y0, x1, y1, col)
    });
    wrap!(linker, "circ", |mut c: Caller<'_, Host>, x: i32, y: i32, r: i32, col: u32| {
        c.data_mut().frame.circ(x, y, r, col)
    });
    wrap!(linker, "circfill", |mut c: Caller<'_, Host>, x: i32, y: i32, r: i32, col: u32| {
        c.data_mut().frame.circfill(x, y, r, col)
    });
    wrap!(linker, "blit", |mut c: Caller<'_, Host>, x: i32, y: i32, w: u32, h: u32, ptr: u32| {
        let Some(len) = w.checked_mul(h).and_then(|n| n.checked_mul(4)) else { return };
        let Some(mem) = c.get_export("memory").and_then(|e| e.into_memory()) else { return };
        let (data, host) = mem.data_and_store_mut(&mut c);
        if let Some(bytes) = data.get(ptr as usize..ptr as usize + len as usize) {
            let px: Vec<Rgb> = bytes.as_chunks::<4>().0.iter().map(|b| u32::from_le_bytes(*b)).collect();
            host.frame.blit(x, y, w as usize, h as usize, &px);
        }
    });
    wrap!(linker, "print", |mut c: Caller<'_, Host>, col: i32, row: i32, ptr: u32, len: u32, color: u32| {
        if let Some((s, host)) = text(&mut c, ptr, len) {
            host.texts.push(Text { col, row, text: s, color });
        }
    });
    wrap!(linker, "pprint", |mut c: Caller<'_, Host>, x: i32, y: i32, ptr: u32, len: u32, color: u32| {
        if let Some((s, host)) = text(&mut c, ptr, len) {
            crate::font::pprint(&mut host.frame, x, y, &s, color);
        }
    });
    // ── saves ──
    wrap!(linker, "save", |mut c: Caller<'_, Host>, kp: u32, kl: u32, vp: u32, vl: u32| {
        let Some((key, _)) = text(&mut c, kp, kl) else { return -1 };
        let Some((value, host)) = text(&mut c, vp, vl) else { return -1 };
        match host.saves().set(&key, &value) {
            Ok(()) => 0,
            Err(e) => {
                host.note(e);
                -1
            }
        }
    });
    // load: copies the value into the tape's buffer [out, out+cap) and returns its length
    // (-1 when there's no such key). A length over `cap` means: call again with a bigger buffer.
    wrap!(linker, "load", |mut c: Caller<'_, Host>, kp: u32, kl: u32, out: u32, cap: u32| -> i32 {
        let Some((key, host)) = text(&mut c, kp, kl) else { return -1 };
        let Some(value) = host.saves().get(&key).map(|v| v.as_bytes().to_vec()) else { return -1 };
        if value.len() <= cap as usize
            && let Some(mem) = c.get_export("memory").and_then(|e| e.into_memory())
            && let Some(dst) = mem.data_mut(&mut c).get_mut(out as usize..out as usize + value.len())
        {
            dst.copy_from_slice(&value);
        }
        value.len() as i32
    });
    // ── sprites ──
    wrap!(linker, "sprite", |mut c: Caller<'_, Host>, ptr: u32, len: u32| {
        text(&mut c, ptr, len).map_or(-1, |(name, host)| host.sprite(&name))
    });
    wrap!(linker, "sprite_w", |c: Caller<'_, Host>, id: i32| c
        .data()
        .sprites
        .get(id as usize)
        .map_or(0, |s| s.w as i32));
    wrap!(linker, "sprite_h", |c: Caller<'_, Host>, id: i32| c
        .data()
        .sprites
        .get(id as usize)
        .map_or(0, |s| s.h as i32));
    wrap!(linker, "sprite_frames", |c: Caller<'_, Host>, id: i32| {
        c.data().sprites.get(id as usize).map_or(0, |s| s.frames.len() as i32)
    });
    wrap!(linker, "spr", |mut c: Caller<'_, Host>, id: i32, frame: i32, x: i32, y: i32, flags: u32| {
        let host = c.data_mut();
        if id >= 0
            && let Some(sprite) = host.sprites.get(id as usize)
        {
            sprite.draw(&mut host.frame, frame.max(0) as usize, x, y, flags & 1 != 0, flags & 2 != 0);
        }
    });
    // ── input ──
    wrap!(linker, "btn", |c: Caller<'_, Host>, b: i32| c.data().buttons.btn(b as usize) as i32);
    wrap!(linker, "btnp", |c: Caller<'_, Host>, b: i32| c.data().buttons.btnp(b as usize) as i32);
    // ── sound ──
    wrap!(linker, "music", |mut c: Caller<'_, Host>, ptr: u32, len: u32| {
        if let Some((name, host)) = text(&mut c, ptr, len)
            && let Err(e) = host.sound().music(&name)
        {
            host.note(e);
        }
    });
    wrap!(linker, "music_stop", |mut c: Caller<'_, Host>| {
        if let Some(s) = &mut c.data_mut().sound {
            s.music_stop();
        }
    });
    wrap!(linker, "sfx", |mut c: Caller<'_, Host>, ptr: u32, len: u32, phrase: i32| {
        if let Some((name, host)) = text(&mut c, ptr, len)
            && let Err(e) = host.sound().sfx(&name, phrase as i64)
        {
            host.note(e);
        }
    });
    // ── screen, time, debugging ──
    wrap!(linker, "width", |c: Caller<'_, Host>| c.data().frame.w as i32);
    wrap!(linker, "height", |c: Caller<'_, Host>| c.data().frame.h as i32);
    wrap!(linker, "time", |c: Caller<'_, Host>| c.data().elapsed);
    wrap!(linker, "frame", |c: Caller<'_, Host>| c.data().frame_no);
    wrap!(linker, "log", |mut c: Caller<'_, Host>, ptr: u32, len: u32| {
        if let Some((line, host)) = text(&mut c, ptr, len) {
            host.note(line);
        }
    });
    Ok(())
}

impl Tape {
    /// Load a tape's `.wasm` bytes and run its `init()`.
    pub fn load(wasm: &[u8], opts: Options) -> Result<Tape, String> {
        let mut config = Config::default();
        config.consume_fuel(true);
        let engine = Engine::new(&config);
        let module = Module::new(&engine, wasm).map_err(err)?;
        let host = Host {
            frame: Frame::new(opts.w, opts.h),
            frame_no: 0,
            buttons: Buttons::default(),
            texts: vec![],
            log: vec![],
            elapsed: 0.0,
            root: opts.root,
            rate: opts.rate,
            sprites: vec![],
            sprite_ids: HashMap::new(),
            sound: None,
            save_key: opts.save_key,
            saves: None,
        };
        let mut store = Store::new(&engine, host);
        let mut linker = Linker::new(&engine);
        link(&mut linker)?;
        store.set_fuel(FUEL_PER_FRAME).map_err(err)?;
        let instance = linker.instantiate_and_start(&mut store, &module).map_err(err)?;
        let func = |name: &str| format!("the tape has no {name}() (is tape!(…) at the end of main.rs?)");
        let init = instance.get_typed_func::<(), ()>(&store, "init").map_err(|_| func("init"))?;
        let update = instance.get_typed_func::<f32, ()>(&store, "update").map_err(|_| func("update"))?;
        let draw = instance.get_typed_func::<(), ()>(&store, "draw").map_err(|_| func("draw"))?;
        let mut tape = Tape { store, update, draw };
        let out = init.call(&mut tape.store, ());
        out.map_err(|e| tape.explain(e))?;
        Ok(tape)
    }

    /// Why a call failed, in words: out of fuel, or the tape's own panic message.
    fn explain(&mut self, e: wasmi::Error) -> String {
        if e.as_trap_code() == Some(wasmi::TrapCode::OutOfFuel) || matches!(self.store.get_fuel(), Ok(0)) {
            return "a frame took too long (an endless loop?)".to_string();
        }
        let panic = self.store.data().log.iter().rev().find_map(|l| l.strip_prefix("panic: ").map(str::to_string));
        panic.unwrap_or_else(|| err(e))
    }

    /// One frame: `update(dt)` then `draw()`, within the fuel budget.
    pub fn step(&mut self, dt: f32, now: Instant) -> Result<(), String> {
        self.store.set_fuel(FUEL_PER_FRAME).map_err(err)?;
        {
            let host = self.store.data_mut();
            host.buttons.begin_frame(now);
            host.texts.clear();
            host.elapsed += dt;
        }
        let out = self.update.call(&mut self.store, dt).and_then(|_| self.draw.call(&mut self.store, ()));
        let host = self.store.data_mut();
        host.frame_no = host.frame_no.wrapping_add(1);
        out.map_err(|e| self.explain(e))
    }

    pub fn frame(&self) -> &Frame {
        &self.store.data().frame
    }

    pub fn texts(&self) -> &[Text] {
        &self.store.data().texts
    }

    pub fn buttons(&mut self) -> &mut Buttons {
        &mut self.store.data_mut().buttons
    }

    /// A new screen size (the terminal was resized). The tape reads it with width()/height().
    pub fn resize(&mut self, w: usize, h: usize) {
        self.store.data_mut().frame.resize(w, h);
    }

    /// Paused: silent, and every button up.
    pub fn pause(&mut self, paused: bool) {
        let host = self.store.data_mut();
        host.buttons.clear();
        if let Some(s) = &mut host.sound {
            s.pause(paused);
        }
    }

    /// The tape's `log` lines since the last call.
    pub fn take_log(&mut self) -> Vec<String> {
        std::mem::take(&mut self.store.data_mut().log)
    }
}
