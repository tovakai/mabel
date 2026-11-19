//! Running a wasm tape with wasmi.
//!
//! A tape imports kit's functions from the module `"mabel"` and exports
//! `init()`, `update(dt: f32)` and `draw()`. Every import works on the host's
//! [`Frame`]; nothing in a tape can touch files, the network or MABEL-OS.
//!
//! Each frame gets a fuel budget (wasmi counts executed instructions), so a
//! runaway loop ends as an error instead of freezing MABEL-OS.

use std::time::Instant;

use wasmi::{Caller, Config, Engine, Linker, Module, Store, TypedFunc};

use crate::{Frame, Rgb};

/// Roughly how many wasm instructions one frame may run.
pub const FUEL_PER_FRAME: u64 = 200_000_000;

/// What the tape's imports can see.
pub struct Host {
    pub frame: Frame,
    pub frame_no: u32,
    pub log: Vec<String>,
    started: Instant,
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

/// Bytes `[ptr, ptr+len)` of the tape's memory, or None if out of range.
fn bytes<'a>(caller: &'a mut Caller<'_, Host>, ptr: u32, len: u32) -> Option<(&'a [u8], &'a mut Host)> {
    let mem = caller.get_export("memory")?.into_memory()?;
    let (data, host) = mem.data_and_store_mut(caller);
    let (start, end) = (ptr as usize, ptr as usize + len as usize);
    Some((data.get(start..end)?, host))
}

fn link(linker: &mut Linker<Host>) -> Result<(), String> {
    linker.func_wrap("mabel", "cls", |mut c: Caller<'_, Host>, col: u32| c.data_mut().frame.cls(col)).map_err(err)?;
    linker
        .func_wrap("mabel", "pset", |mut c: Caller<'_, Host>, x: i32, y: i32, col: u32| {
            c.data_mut().frame.pset(x, y, col)
        })
        .map_err(err)?;
    linker.func_wrap("mabel", "pget", |c: Caller<'_, Host>, x: i32, y: i32| c.data().frame.pget(x, y)).map_err(err)?;
    linker
        .func_wrap("mabel", "rectfill", |mut c: Caller<'_, Host>, x: i32, y: i32, w: i32, h: i32, col: u32| {
            c.data_mut().frame.rectfill(x, y, w, h, col)
        })
        .map_err(err)?;
    linker
        .func_wrap("mabel", "blit", |mut c: Caller<'_, Host>, x: i32, y: i32, w: u32, h: u32, ptr: u32| {
            let Some(len) = w.checked_mul(h).and_then(|n| n.checked_mul(4)) else { return };
            if let Some((data, host)) = bytes(&mut c, ptr, len) {
                let px: Vec<Rgb> = data.as_chunks::<4>().0.iter().map(|b| u32::from_le_bytes(*b)).collect();
                host.frame.blit(x, y, w as usize, h as usize, &px);
            }
        })
        .map_err(err)?;
    linker.func_wrap("mabel", "width", |c: Caller<'_, Host>| c.data().frame.w as i32).map_err(err)?;
    linker.func_wrap("mabel", "height", |c: Caller<'_, Host>| c.data().frame.h as i32).map_err(err)?;
    linker.func_wrap("mabel", "time", |c: Caller<'_, Host>| c.data().started.elapsed().as_secs_f32()).map_err(err)?;
    linker.func_wrap("mabel", "frame", |c: Caller<'_, Host>| c.data().frame_no).map_err(err)?;
    linker
        .func_wrap("mabel", "log", |mut c: Caller<'_, Host>, ptr: u32, len: u32| {
            if let Some((data, host)) = bytes(&mut c, ptr, len) {
                let line = String::from_utf8_lossy(data).into_owned();
                host.log.push(line);
            }
        })
        .map_err(err)?;
    Ok(())
}

impl Tape {
    /// Load a tape's `.wasm` bytes and run its `init()` on a `w`×`h` pixel screen.
    pub fn load(wasm: &[u8], w: usize, h: usize) -> Result<Tape, String> {
        let mut config = Config::default();
        config.consume_fuel(true);
        let engine = Engine::new(&config);
        let module = Module::new(&engine, wasm).map_err(err)?;
        let host = Host { frame: Frame::new(w, h), frame_no: 0, log: Vec::new(), started: Instant::now() };
        let mut store = Store::new(&engine, host);
        let mut linker = Linker::new(&engine);
        link(&mut linker)?;
        store.set_fuel(FUEL_PER_FRAME).map_err(err)?;
        let instance = linker.instantiate_and_start(&mut store, &module).map_err(err)?;
        let init = instance.get_typed_func::<(), ()>(&store, "init").map_err(err)?;
        let update = instance.get_typed_func::<f32, ()>(&store, "update").map_err(err)?;
        let draw = instance.get_typed_func::<(), ()>(&store, "draw").map_err(err)?;
        init.call(&mut store, ()).map_err(err)?;
        Ok(Tape { store, update, draw })
    }

    /// One frame: `update(dt)` then `draw()`, within the fuel budget.
    pub fn step(&mut self, dt: f32) -> Result<(), String> {
        self.store.set_fuel(FUEL_PER_FRAME).map_err(err)?;
        let out = self.update.call(&mut self.store, dt).and_then(|_| self.draw.call(&mut self.store, ()));
        self.store.data_mut().frame_no = self.store.data().frame_no.wrapping_add(1);
        out.map_err(|e| {
            if matches!(self.store.get_fuel(), Ok(0)) {
                "the frame took too long (out of fuel)".to_string()
            } else {
                err(e)
            }
        })
    }

    pub fn frame(&self) -> &Frame {
        &self.store.data().frame
    }

    /// The tape's `log` lines since the last call.
    pub fn take_log(&mut self) -> Vec<String> {
        std::mem::take(&mut self.store.data_mut().log)
    }
}
