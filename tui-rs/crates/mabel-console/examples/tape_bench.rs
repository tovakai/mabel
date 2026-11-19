//! Milestone 0 benchmark: run a tape headless and time each part of a frame.
//!
//!   tape_bench <tape.wasm> [cols rows] [frames]
//!   tape_bench <tape.wasm> live [frames]   (draws on this terminal at full speed, then reports)
//!
//! - tape: `update` + `draw` inside wasmi
//! - render: Frame → half-block cells (ratatui buffer)
//! - flush: ratatui's diff + crossterm escape codes, written to memory (no terminal),
//!   with the bytes per frame a real terminal would have to parse.

use std::cell::Cell;
use std::io::Write;
use std::rc::Rc;
use std::time::{Duration, Instant};

use mabel_console::host::Tape;
use mabel_console::{Frame, render};
use ratatui::backend::CrosstermBackend;
use ratatui::layout::Rect;
use ratatui::{Terminal, TerminalOptions, Viewport};

/// A writer that only counts what the terminal would have received.
#[derive(Clone, Default)]
struct Count(Rc<Cell<usize>>);

impl Write for Count {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.0.set(self.0.get() + b.len());
        Ok(b.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

fn ms(d: Duration) -> f64 {
    d.as_secs_f64() * 1000.0
}

fn stats(name: &str, v: &mut [Duration]) {
    v.sort();
    let avg = v.iter().sum::<Duration>() / v.len() as u32;
    let p95 = v[v.len() * 95 / 100];
    println!("  {name:<7} avg {:6.2} ms   p95 {:6.2} ms   max {:6.2} ms", ms(avg), ms(p95), ms(*v.last().unwrap()));
}

fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let path = args.get(1).expect("usage: tape_bench <tape.wasm> [cols rows] [frames] | live [frames]");
    if args.get(2).map(String::as_str) == Some("live") {
        return live(path, args.get(3).map_or(Ok(300), |s| s.parse())?);
    }
    let cols: u16 = args.get(2).map_or(Ok(128), |s| s.parse())?;
    let rows: u16 = args.get(3).map_or(Ok(36), |s| s.parse())?;
    let frames: usize = args.get(4).map_or(Ok(300), |s| s.parse())?;
    let size = Frame::for_cells(cols, rows);
    let (w, h) = (size.w, size.h);

    let t = Instant::now();
    let mut tape = Tape::load(&std::fs::read(path)?, w, h).map_err(anyhow::Error::msg)?;
    println!("{path}: {w}x{h} px ({cols}x{rows} cells), loaded in {:.1} ms", ms(t.elapsed()));

    let area = Rect::new(0, 0, cols, rows);
    let count = Count::default();
    let mut term = Terminal::with_options(
        CrosstermBackend::new(count.clone()),
        TerminalOptions { viewport: Viewport::Fixed(area) },
    )?;
    let (mut c, mut r, mut f) = (Vec::new(), Vec::new(), Vec::new());
    let start = Instant::now();
    for _ in 0..frames {
        let t0 = Instant::now();
        tape.step(1.0 / 30.0).map_err(anyhow::Error::msg)?;
        let t1 = Instant::now();
        let mut rendered = Duration::ZERO;
        term.draw(|fr| {
            let t = Instant::now();
            render(tape.frame(), fr.buffer_mut(), area);
            rendered = t.elapsed();
        })?;
        let t2 = Instant::now();
        c.push(t1 - t0);
        r.push(rendered);
        f.push(t2 - t1 - rendered);
    }
    let total = start.elapsed();
    let bytes = count.0.get();
    stats("tape", &mut c);
    stats("render", &mut r);
    stats("flush", &mut f);
    println!(
        "  => {:.1} fps flat out, {:.0} KB per frame to the terminal; log: {:?}",
        frames as f64 / total.as_secs_f64(),
        bytes as f64 / frames as f64 / 1024.0,
        tape.take_log()
    );
    Ok(())
}

/// Draw on the real terminal as fast as it accepts output. Writes block when the
/// terminal falls behind, so this fps includes the terminal's own parsing and drawing.
fn live(path: &str, frames: usize) -> anyhow::Result<()> {
    let mut term = ratatui::init();
    let area = term.get_frame().area();
    let size = Frame::for_cells(area.width, area.height);
    let loaded = Tape::load(&std::fs::read(path)?, size.w, size.h).map_err(anyhow::Error::msg);
    let mut tape = match loaded {
        Ok(c) => c,
        Err(e) => {
            ratatui::restore();
            return Err(e);
        }
    };
    let mut times = Vec::new();
    let start = Instant::now();
    let mut result = Ok(());
    for _ in 0..frames {
        let t = Instant::now();
        if let Err(e) = tape.step(1.0 / 30.0) {
            result = Err(anyhow::Error::msg(e));
            break;
        }
        if let Err(e) = term.draw(|fr| render(tape.frame(), fr.buffer_mut(), area)) {
            result = Err(e.into());
            break;
        }
        times.push(t.elapsed());
    }
    let total = start.elapsed();
    ratatui::restore();
    result?;
    println!("{path}: live on {}x{} cells, {} frames", area.width, area.height, times.len());
    stats("frame", &mut times);
    println!("  => {:.1} fps including the terminal", times.len() as f64 / total.as_secs_f64());
    Ok(())
}
