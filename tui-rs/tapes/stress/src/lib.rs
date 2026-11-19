//! Draws a moving pattern over the whole screen every frame, in one of three ways.
use mabel_kit::*;

struct Stress {
    t: u32,
    buf: Vec<Color>,
}

fn pattern(x: i32, y: i32, t: u32) -> Color {
    let (x, y, t) = (x as u32, y as u32, t);
    rgb((x * 2 + t) as u8, ((y * 3) ^ t) as u8, ((x ^ y) + t) as u8)
}

impl Tape for Stress {
    fn init() -> Self {
        log("stress tape up");
        Stress { t: 0, buf: Vec::new() }
    }

    fn update(&mut self, _dt: f32) {
        self.t = self.t.wrapping_add(1);
        if cfg!(feature = "blit") {
            let (w, h) = (width(), height());
            self.buf.resize((w * h) as usize, 0);
            for y in 0..h {
                for x in 0..w {
                    self.buf[(y * w + x) as usize] = pattern(x, y, self.t);
                }
            }
        }
    }

    fn draw(&self) {
        let (w, h) = (width(), height());
        if cfg!(feature = "pset") {
            for y in 0..h {
                for x in 0..w {
                    pset(x, y, pattern(x, y, self.t));
                }
            }
        } else if cfg!(feature = "blit") {
            blit(0, 0, w as u32, h as u32, &self.buf);
        } else {
            cls(rgb(0x1b, 0x15, 0x10));
            for i in 0..300u32 {
                let x = ((i * 37 + self.t) % w as u32) as i32;
                let y = ((i * 11 + self.t / 2) % h as u32) as i32;
                rectfill(x, y, 8, 8, pattern(x, y, i));
            }
        }
    }
}

tape!(Stress);
