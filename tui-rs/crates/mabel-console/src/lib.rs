//! The console behind TAPES: what a running DreamTape draws into and how it reaches the terminal.
//!
//! - [`Frame`]: an RGB pixel framebuffer plus the drawing calls kit exposes.
//! - [`Sprite`]: a PIXEL file (`.mpx` or `.png`) as ready-to-draw frames.
//! - [`render`]: half-block output, one terminal cell = 1×2 pixels (top pixel is the
//!   foreground of `▀`, bottom pixel the background), the same trick as the avatar and PIXEL.
//!   Text from `print` goes on top as real characters.
//! - [`input`], [`sound`], [`tapefile`]: buttons, music and sound effects, `tape.toml`.
//! - [`host`] (feature `host`): loads a wasm tape with wasmi and wires kit's imports to all this.
//!
//! Shared by MABEL-OS's TAPES channel and by standalone (native) builds of a game.

use std::path::Path;

use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;

pub use ratatui;

pub mod font;
#[cfg(feature = "host")]
pub mod host;
pub mod input;
pub mod saves;
pub mod sound;
pub mod tapefile;

/// A colour as 0xRRGGBB.
pub type Rgb = u32;

/// In pictures passed to `blit` and in sprites: a see-through pixel.
pub const TRANSPARENT: Rgb = 0xff00_0000;

/// Is this colour see-through (anything in the top byte)?
fn see_through(c: Rgb) -> bool {
    c >> 24 != 0
}

/// The pixel screen a tape draws on.
pub struct Frame {
    pub w: usize,
    pub h: usize,
    pub px: Vec<Rgb>,
}

impl Frame {
    /// A black screen `w`×`h` pixels.
    pub fn new(w: usize, h: usize) -> Self {
        Frame { w, h, px: vec![0; w * h] }
    }

    /// The pixel size that fits a body of `cols`×`rows` terminal cells.
    pub fn for_cells(cols: u16, rows: u16) -> Self {
        Frame::new(cols as usize, rows as usize * 2)
    }

    /// A new size; the picture starts black.
    pub fn resize(&mut self, w: usize, h: usize) {
        if (w, h) != (self.w, self.h) {
            *self = Frame::new(w, h);
        }
    }

    /// Fill the screen with one colour.
    pub fn cls(&mut self, c: Rgb) {
        self.px.fill(c);
    }

    /// Set one pixel (off the screen: nothing happens).
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

    /// A rectangle's outline.
    pub fn rect(&mut self, x: i32, y: i32, w: i32, h: i32, c: Rgb) {
        if w <= 0 || h <= 0 {
            return;
        }
        self.rectfill(x, y, w, 1, c);
        self.rectfill(x, y + h - 1, w, 1, c);
        self.rectfill(x, y, 1, h, c);
        self.rectfill(x + w - 1, y, 1, h, c);
    }

    /// A straight line from (x0, y0) to (x1, y1), both ends included.
    pub fn line(&mut self, mut x0: i32, mut y0: i32, x1: i32, y1: i32, c: Rgb) {
        let (dx, dy) = ((x1 - x0).abs(), -(y1 - y0).abs());
        let (sx, sy) = (if x0 < x1 { 1 } else { -1 }, if y0 < y1 { 1 } else { -1 });
        let mut err = dx + dy;
        for _ in 0..=(dx.max(-dy)).min(4096) {
            self.pset(x0, y0, c);
            if x0 == x1 && y0 == y1 {
                break;
            }
            let e2 = 2 * err;
            if e2 >= dy {
                err += dy;
                x0 += sx;
            }
            if e2 <= dx {
                err += dx;
                y0 += sy;
            }
        }
    }

    /// A circle's outline around (cx, cy).
    pub fn circ(&mut self, cx: i32, cy: i32, r: i32, c: Rgb) {
        self.circle(cx, cy, r, c, false);
    }

    /// A filled circle around (cx, cy).
    pub fn circfill(&mut self, cx: i32, cy: i32, r: i32, c: Rgb) {
        self.circle(cx, cy, r, c, true);
    }

    /// A circle, outline or filled (the midpoint algorithm: integers only).
    fn circle(&mut self, cx: i32, cy: i32, r: i32, c: Rgb, fill: bool) {
        if r < 0 {
            return;
        }
        let r = r.min(4096);
        let (mut x, mut y, mut err) = (r, 0, 1 - r);
        while x >= y {
            if fill {
                self.rectfill(cx - x, cy + y, 2 * x + 1, 1, c);
                self.rectfill(cx - x, cy - y, 2 * x + 1, 1, c);
                self.rectfill(cx - y, cy + x, 2 * y + 1, 1, c);
                self.rectfill(cx - y, cy - x, 2 * y + 1, 1, c);
            } else {
                for (px, py) in [(x, y), (y, x), (-y, x), (-x, y), (-x, -y), (-y, -x), (y, -x), (x, -y)] {
                    self.pset(cx + px, cy + py, c);
                }
            }
            y += 1;
            if err < 0 {
                err += 2 * y + 1;
            } else {
                x -= 1;
                err += 2 * (y - x) + 1;
            }
        }
    }

    /// Copy a whole `w`×`h` picture to (x, y), clipped. `TRANSPARENT` pixels are skipped.
    pub fn blit(&mut self, x: i32, y: i32, w: usize, h: usize, src: &[Rgb]) {
        self.blit_ex(x, y, w, h, src, false, false);
    }

    /// `blit`, mirrored left-right and/or upside down.
    #[allow(clippy::too_many_arguments)]
    pub fn blit_ex(&mut self, x: i32, y: i32, w: usize, h: usize, src: &[Rgb], flip_x: bool, flip_y: bool) {
        if w == 0 {
            return;
        }
        for sy in 0..h.min(src.len() / w) {
            let ty = y + if flip_y { (h - 1 - sy) as i32 } else { sy as i32 };
            if ty < 0 || ty as usize >= self.h {
                continue;
            }
            for sx in 0..w {
                let c = src[sy * w + sx];
                if !see_through(c) {
                    let tx = x + if flip_x { (w - 1 - sx) as i32 } else { sx as i32 };
                    self.pset(tx, ty, c);
                }
            }
        }
    }
}

/// A picture from PIXEL, every animation frame flattened (layers merged) and ready to draw.
#[derive(Clone, Debug)]
pub struct Sprite {
    pub w: usize,
    pub h: usize,
    /// One `w*h` picture per frame; empty pixels are `TRANSPARENT`.
    pub frames: Vec<Vec<Rgb>>,
}

impl Sprite {
    /// An `.mpx` (all frames) or a `.png` (one frame).
    pub fn load(path: &Path) -> Result<Sprite, String> {
        let doc = mabel_media::pixel::open_any(path).map_err(|e| format!("{e:#}"))?;
        let frames = (0..doc.frames.len())
            .map(|f| {
                doc.composite(f)
                    .iter()
                    .flat_map(|row| {
                        row.iter().map(|px| match px {
                            Some([r, g, b]) => (*r as Rgb) << 16 | (*g as Rgb) << 8 | *b as Rgb,
                            None => TRANSPARENT,
                        })
                    })
                    .collect()
            })
            .collect();
        Ok(Sprite { w: doc.w, h: doc.h, frames })
    }

    /// Draw frame `frame` (wrapping round) with its top-left corner at (x, y).
    pub fn draw(&self, into: &mut Frame, frame: usize, x: i32, y: i32, flip_x: bool, flip_y: bool) {
        if let Some(px) = self.frames.get(frame % self.frames.len().max(1)) {
            into.blit_ex(x, y, self.w, self.h, px, flip_x, flip_y);
        }
    }
}

/// `print`: real characters on the cell grid, drawn over the pixels.
#[derive(Clone, Debug, PartialEq)]
pub struct Text {
    pub col: i32,
    pub row: i32,
    pub text: String,
    pub color: Rgb,
}

/// Where a tape's screen goes in a body of cells, and how big.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Placement {
    /// The tape's screen in pixels.
    pub w: usize,
    pub h: usize,
    /// Each pixel drawn `scale` times as big (the fixed 128×72 mode on a big terminal).
    pub scale: usize,
    /// The cells it covers, centred in the body.
    pub cells: Rect,
}

/// `fill`: the whole body, 1×2 pixels per cell. `128x72`: centred, scaled by whole numbers.
pub fn place(screen: tapefile::Screen, body: Rect) -> Placement {
    let (w, h, scale) = match screen {
        tapefile::Screen::Fill => (body.width.max(1) as usize, body.height.max(1) as usize * 2, 1),
        tapefile::Screen::Fixed => {
            let (w, h) = tapefile::FIXED;
            (w, h, (body.width as usize / w).min(body.height as usize * 2 / h).max(1))
        }
    };
    let (cols, rows) = ((w * scale) as u16, (h * scale).div_ceil(2) as u16);
    let cells = Rect {
        x: body.x + body.width.saturating_sub(cols) / 2,
        y: body.y + body.height.saturating_sub(rows) / 2,
        width: cols.min(body.width),
        height: rows.min(body.height),
    };
    Placement { w, h, scale, cells }
}

/// A 0xRRGGBB colour as a terminal colour.
fn color(c: Rgb) -> Color {
    Color::Rgb((c >> 16) as u8, (c >> 8) as u8, c as u8)
}

/// Draw `frame` into `area` of `buf` as half-blocks, each pixel `scale`×`scale` cells-worth,
/// top-left aligned and clipped; then the texts on top.
pub fn render(frame: &Frame, texts: &[Text], buf: &mut Buffer, area: Rect, scale: usize) {
    let scale = scale.max(1);
    let rows = (area.height as usize).min((frame.h * scale).div_ceil(2));
    let cols = (area.width as usize).min(frame.w * scale);
    let at = |x: usize, y: usize| -> Rgb {
        let (px, py) = (x / scale, y / scale);
        if py < frame.h { frame.px[py * frame.w + px] } else { 0 }
    };
    for r in 0..rows {
        for x in 0..cols {
            let (top, bottom) = (at(x, 2 * r), at(x, 2 * r + 1));
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
    for t in texts {
        let row = t.row * scale as i32;
        if row < 0 || row as usize >= rows {
            continue;
        }
        for (i, ch) in t.text.chars().enumerate() {
            let col = t.col * scale as i32 + i as i32;
            if col < 0 || col as usize >= cols {
                continue;
            }
            let under = at(col as usize, 2 * row as usize); // text sits on the cell's top pixel colour
            if let Some(cell) = buf.cell_mut((area.x + col as u16, area.y + row as u16)) {
                cell.set_char(ch).set_fg(color(t.color)).set_bg(color(under));
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
    fn blit_skips_transparent_pixels_and_flips() {
        let mut f = Frame::new(3, 1);
        f.cls(0x0000ff);
        f.blit(0, 0, 3, 1, &[0xff0000, TRANSPARENT, 0x00ff00]);
        assert_eq!(f.px, vec![0xff0000, 0x0000ff, 0x00ff00]);
        f.blit_ex(0, 0, 3, 1, &[1, 2, 3], true, false);
        assert_eq!(f.px, vec![3, 2, 1]);
    }

    #[test]
    fn lines_and_circles() {
        let mut f = Frame::new(5, 5);
        f.line(0, 0, 4, 4, 9);
        assert!((0..5).all(|i| f.pget(i, i) == 9));
        let mut f = Frame::new(7, 7);
        f.circfill(3, 3, 2, 5);
        assert_eq!(f.pget(3, 3), 5);
        assert_eq!(f.pget(3, 1), 5);
        assert_eq!(f.pget(0, 0), 0);
        f.rect(0, 0, 7, 7, 1);
        assert_eq!((f.pget(6, 6), f.pget(3, 0), f.pget(3, 3)), (1, 1, 5));
    }

    #[test]
    fn one_cell_holds_two_pixels_and_text_sits_on_top() {
        let mut f = Frame::new(2, 2);
        f.px = vec![0xff0000, 0x00ff00, 0xff0000, 0x0000ff];
        let mut buf = Buffer::empty(Rect::new(0, 0, 2, 1));
        let area = buf.area;
        render(&f, &[], &mut buf, area, 1);
        assert_eq!(buf[(0, 0)].symbol(), " ");
        assert_eq!(buf[(0, 0)].bg, Color::Rgb(255, 0, 0));
        assert_eq!(buf[(1, 0)].symbol(), "▀");
        assert_eq!((buf[(1, 0)].fg, buf[(1, 0)].bg), (Color::Rgb(0, 255, 0), Color::Rgb(0, 0, 255)));
        let texts = [Text { col: 1, row: 0, text: "hi".into(), color: 0xffffff }];
        render(&f, &texts, &mut buf, area, 1);
        assert_eq!(buf[(1, 0)].symbol(), "h");
        assert_eq!(buf[(1, 0)].bg, Color::Rgb(0, 255, 0));
    }

    #[test]
    fn scaled_up_pixels() {
        let mut f = Frame::new(1, 1);
        f.px = vec![0x123456];
        let mut buf = Buffer::empty(Rect::new(0, 0, 3, 3));
        let area = buf.area;
        render(&f, &[], &mut buf, area, 2);
        assert_eq!(buf[(1, 0)].bg, Color::Rgb(0x12, 0x34, 0x56));
        assert_eq!(buf[(2, 0)].symbol(), " "); // past the scaled picture: untouched
        assert_eq!(buf[(2, 0)].bg, Color::Reset);
    }
}
