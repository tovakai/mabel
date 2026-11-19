//! The console behind PROGRAM: what a running tape draws into and how it reaches the terminal.
//!
//! - [`Frame`]: an RGB pixel framebuffer plus the drawing calls kit exposes.
//! - [`render`]: half-block output, one terminal cell = 1×2 pixels (top pixel is the
//!   foreground of `▀`, bottom pixel the background), the same trick as the avatar and PIXEL.
//! - [`host`] (feature `host`): loads a wasm tape with wasmi and wires kit's imports to a Frame.
//!
//! Shared by MABEL-OS's PROGRAM channel and by standalone (native) builds of a game.

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;

#[cfg(feature = "host")]
pub mod host;

/// A colour as 0xRRGGBB.
pub type Rgb = u32;

/// The pixel screen a tape draws on.
pub struct Frame {
    pub w: usize,
    pub h: usize,
    pub px: Vec<Rgb>,
}

impl Frame {
    pub fn new(w: usize, h: usize) -> Self {
        Frame { w, h, px: vec![0; w * h] }
    }

    /// The pixel size that fits a body of `cols`×`rows` terminal cells.
    pub fn for_cells(cols: u16, rows: u16) -> Self {
        Frame::new(cols as usize, rows as usize * 2)
    }

    pub fn cls(&mut self, c: Rgb) {
        self.px.fill(c);
    }

    pub fn pset(&mut self, x: i32, y: i32, c: Rgb) {
        if x >= 0 && y >= 0 && (x as usize) < self.w && (y as usize) < self.h {
            self.px[y as usize * self.w + x as usize] = c;
        }
    }

    /// The colour at (x, y); 0 (black) outside the screen.
    pub fn pget(&self, x: i32, y: i32) -> Rgb {
        if x >= 0 && y >= 0 && (x as usize) < self.w && (y as usize) < self.h {
            self.px[y as usize * self.w + x as usize]
        } else {
            0
        }
    }

    /// A filled rectangle, clipped to the screen.
    pub fn rectfill(&mut self, x: i32, y: i32, w: i32, h: i32, c: Rgb) {
        let (x0, y0) = (x.max(0) as usize, y.max(0) as usize);
        let x1 = (x.saturating_add(w)).clamp(0, self.w as i32) as usize;
        let y1 = (y.saturating_add(h)).clamp(0, self.h as i32) as usize;
        if x0 >= x1 {
            return;
        }
        for row in y0..y1 {
            self.px[row * self.w + x0..row * self.w + x1].fill(c);
        }
    }

    /// Copy a whole `w`×`h` picture to (x, y), clipped. Pixels with the top byte set
    /// (0xFF000000 and up) are transparent, so sprites can have holes.
    pub fn blit(&mut self, x: i32, y: i32, w: usize, h: usize, src: &[Rgb]) {
        for sy in 0..h.min(src.len() / w.max(1)) {
            let ty = y + sy as i32;
            if ty < 0 || ty as usize >= self.h {
                continue;
            }
            for sx in 0..w {
                let c = src[sy * w + sx];
                if c >> 24 == 0 {
                    self.pset(x + sx as i32, ty, c);
                }
            }
        }
    }
}

fn color(c: Rgb) -> Color {
    Color::Rgb((c >> 16) as u8, (c >> 8) as u8, c as u8)
}

/// Draw `frame` into `area` of `buf` as half-blocks, top-left aligned and clipped.
pub fn render(frame: &Frame, buf: &mut Buffer, area: Rect) {
    let rows = (area.height as usize).min(frame.h.div_ceil(2));
    let cols = (area.width as usize).min(frame.w);
    for r in 0..rows {
        for x in 0..cols {
            let top = frame.px[2 * r * frame.w + x];
            let bottom = if 2 * r + 1 < frame.h { frame.px[(2 * r + 1) * frame.w + x] } else { 0 };
            if let Some(cell) = buf.cell_mut((area.x + x as u16, area.y + r as u16)) {
                // Same colour top and bottom: a plain space, so the terminal only gets a background.
                if top == bottom {
                    cell.set_symbol(" ").set_fg(color(top)).set_bg(color(top));
                } else {
                    cell.set_symbol("▀").set_fg(color(top)).set_bg(color(bottom));
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drawing_is_clipped_to_the_screen() {
        let mut f = Frame::new(4, 4);
        f.rectfill(-2, -2, 4, 4, 0x112233);
        f.pset(9, 9, 0xffffff);
        assert_eq!(f.pget(0, 0), 0x112233);
        assert_eq!(f.pget(1, 1), 0x112233);
        assert_eq!(f.pget(2, 2), 0);
        assert_eq!(f.pget(-1, 0), 0);
    }

    #[test]
    fn blit_skips_transparent_pixels() {
        let mut f = Frame::new(3, 1);
        f.cls(0x0000ff);
        f.blit(0, 0, 3, 1, &[0xff0000, 0xff00ff00, 0x00ff00]);
        assert_eq!(f.px, vec![0xff0000, 0x0000ff, 0x00ff00]);
    }

    #[test]
    fn one_cell_holds_two_pixels() {
        let mut f = Frame::new(2, 2);
        f.px = vec![0xff0000, 0x00ff00, 0xff0000, 0x0000ff];
        let mut buf = Buffer::empty(Rect::new(0, 0, 2, 1));
        let area = buf.area;
        render(&f, &mut buf, area);
        assert_eq!(buf[(0, 0)].symbol(), " ");
        assert_eq!(buf[(0, 0)].bg, Color::Rgb(255, 0, 0));
        assert_eq!(buf[(1, 0)].symbol(), "▀");
        assert_eq!((buf[(1, 0)].fg, buf[(1, 0)].bg), (Color::Rgb(0, 255, 0), Color::Rgb(0, 0, 255)));
    }
}
