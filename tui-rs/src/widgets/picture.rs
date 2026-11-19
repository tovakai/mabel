//! A picture drawn in half-blocks: one cell = 1×2 pixels (▀, top pixel as the colour, bottom as
//! the background). Pixel art on purpose, and it works in every terminal, the deck's included.
//! The scaled copy is kept until the space it's drawn in changes.

use image::imageops::FilterType;
use image::{DynamicImage, RgbaImage};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;

const MAX_KEPT: u32 = 320;

pub struct Picture {
    source: DynamicImage,
    /// The copy scaled for (width, height) in cells.
    scaled: Option<((u16, u16), RgbaImage)>,
}

impl Picture {
    pub fn decode(bytes: &[u8]) -> Option<Self> {
        let source = image::load_from_memory(bytes).ok()?;
        // No screen shows more than ~320×200 half-block pixels: keep a copy that size, not the
        // original (a 1024² picture is 4 MB of pixels; this is about 0.25 MB).
        let source = if source.width() > MAX_KEPT || source.height() > MAX_KEPT {
            source.resize(MAX_KEPT, MAX_KEPT, FilterType::Triangle)
        } else {
            source
        };
        Some(Picture { source, scaled: None })
    }

    /// Cells the picture takes when fitted into `area` (keeping its proportions).
    pub fn fit(&self, max_w: u16, max_h: u16) -> (u16, u16) {
        let (w, h) = (self.source.width().max(1) as f64, self.source.height().max(1) as f64);
        // In pixels: max_w × (2 · max_h).
        let scale = (max_w as f64 / w).min(2.0 * max_h as f64 / h);
        let pw = (w * scale).floor().max(1.0) as u16;
        let ph = (h * scale).floor().max(2.0) as u16;
        (pw.min(max_w), ph.div_ceil(2).min(max_h))
    }

    /// Draw centred horizontally at the top of `area`, fitted into `max_h` rows, leaving out the
    /// first `skip` rows (scrolled away). Returns the rows drawn.
    pub fn render(&mut self, area: Rect, buf: &mut Buffer, max_h: u16, skip: u16) -> u16 {
        if area.width == 0 || area.height == 0 || max_h == 0 {
            return 0;
        }
        let (cw, ch) = self.fit(area.width, max_h);
        let fresh = !matches!(&self.scaled, Some((size, _)) if *size == (cw, ch));
        if fresh {
            let img = self.source.resize_exact(cw as u32, ch as u32 * 2, FilterType::Triangle).to_rgba8();
            self.scaled = Some(((cw, ch), img));
        }
        let Some((_, img)) = &self.scaled else { return 0 };
        let x0 = area.x + (area.width - cw) / 2;
        let rgb = |p: &image::Rgba<u8>| Color::Rgb(p[0], p[1], p[2]);
        let rows = ch.saturating_sub(skip).min(area.height);
        for drawn in 0..rows {
            let row = drawn + skip;
            for col in 0..cw {
                let top = img.get_pixel(col as u32, row as u32 * 2);
                let cell = &mut buf[(x0 + col, area.y + drawn)];
                cell.set_symbol("▀").set_fg(rgb(top));
                if let Some(bottom) = img.get_pixel_checked(col as u32, row as u32 * 2 + 1) {
                    cell.set_bg(rgb(bottom));
                }
            }
        }
        rows
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fits_keeping_proportions() {
        let img = DynamicImage::new_rgba8(100, 50);
        let p = Picture { source: img, scaled: None };
        assert_eq!(p.fit(40, 14), (40, 10)); // 40×20 px
        assert_eq!(p.fit(100, 5), (20, 5)); // limited by height: 10 px tall
    }
}
