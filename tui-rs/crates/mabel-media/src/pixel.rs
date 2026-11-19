//! PIXEL's model: a sprite document with layers and animation frames, drawing, and files.
//! No drawing to the screen here, so it's easy to test; pixelview.rs draws it.
//!
//! A document is w×h pixels, one or more layers, one or more frames (each with a duration in ms
//! and one cel per layer) and tags naming frame ranges. A pixel is a colour or nothing
//! (transparent); layers stack bottom to top, the top non-transparent pixel wins.
//!
//! Files (the same as the Python client's, so both open each other's work):
//! - .mpx   the native format, JSON (layers, frames, tags, palette)
//! - .png   one image, or a horizontal strip of square frames (opened as frames, saved the same way)
//! - an avatar folder (avatar.json + one strip per state, like assets/avatar): each state is a
//!   tag; saving writes every tag back as its strip and the frame timings into avatar.json

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, anyhow, bail};
use serde_json::{Map, Value, json};

/// A pixel: an RGB colour, or None for transparent.
pub type Px = Option<[u8; 3]>;
pub type Grid = Vec<Vec<Px>>;

/// Colours a palette holds at most, a picture's biggest side, and undo steps kept.
pub const MAX_PALETTE: usize = 64;
pub const MAX_SIDE: usize = 256;
const UNDO_DEPTH: usize = 100;

/// The built-in palettes (:palette NAME).
pub const PALETTES: [(&str, &[&str]); 6] = [
    (
        "pico8",
        &[
            "#000000", "#1d2b53", "#7e2553", "#008751", "#ab5236", "#5f574f", "#c2c3c7", "#fff1e8", "#ff004d",
            "#ffa300", "#ffec27", "#00e436", "#29adff", "#83769c", "#ff77a8", "#ffccaa",
        ],
    ),
    (
        "db16",
        &[
            "#140c1c", "#442434", "#30346d", "#4e4a4e", "#854c30", "#346524", "#d04648", "#757161", "#597dce",
            "#d27d2c", "#8595a1", "#6daa2c", "#d2aa99", "#6dc2ca", "#dad45e", "#deeed6",
        ],
    ),
    (
        "db32",
        &[
            "#000000", "#222034", "#45283c", "#663931", "#8f563b", "#df7126", "#d9a066", "#eec39a", "#fbf236",
            "#99e550", "#6abe30", "#37946e", "#4b692f", "#524b24", "#323c39", "#3f3f74", "#306082", "#5b6ee1",
            "#639bff", "#5fcde4", "#cbdbfc", "#ffffff", "#9badb7", "#847e87", "#696a6a", "#595652", "#76428a",
            "#ac3232", "#d95763", "#d77bba", "#8f974a", "#8a6f30",
        ],
    ),
    ("gameboy", &["#0f380f", "#306230", "#8bac0f", "#9bbc0f"]),
    ("grayscale", &["#000000", "#242424", "#494949", "#6d6d6d", "#929292", "#b6b6b6", "#dbdbdb", "#ffffff"]),
    (
        "phosphor",
        &[
            "#0c0804", "#3d2a08", "#6b1d0a", "#a86b00", "#ffb000", "#ffd066", "#d9cba4", "#8a7b58", "#3fd6c6",
            "#ff5a36", "#ff8a3d", "#9fd8a8",
        ],
    ),
];

/// "#rgb", "#rrggbb" or "#rrggbbaa" → a colour.
pub fn parse_color(s: &str) -> Px {
    let h = s.strip_prefix('#')?.to_lowercase();
    let h = if h.len() == 3 { h.chars().flat_map(|c| [c, c]).collect() } else { h };
    if !(h.len() == 6 || h.len() == 8) || !h.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let b = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
    Some([b(0)?, b(2)?, b(4)?])
}

/// A colour as "#rrggbb".
pub fn hex(c: [u8; 3]) -> String {
    format!("#{:02x}{:02x}{:02x}", c[0], c[1], c[2])
}

/// A built-in palette's colours (db32 if the name isn't known).
pub fn palette(name: &str) -> Vec<[u8; 3]> {
    let colors = PALETTES.iter().find(|(n, _)| *n == name).map_or(PALETTES[2].1, |(_, c)| *c);
    colors.iter().filter_map(|c| parse_color(c)).collect()
}

/// A transparent `w`×`h` grid.
pub fn blank(w: usize, h: usize) -> Grid {
    vec![vec![None; w]; h]
}

/// A side length kept within 1..=`MAX_SIDE`.
fn side(n: i64) -> usize {
    n.clamp(1, MAX_SIDE as i64) as usize
}

#[derive(Clone, Debug, PartialEq)]
pub struct Layer {
    pub name: String,
    pub visible: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Frame {
    /// One per layer, bottom first.
    pub cels: Vec<Grid>,
    pub ms: u32,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Tag {
    pub name: String,
    pub start: usize,
    /// Inclusive.
    pub end: usize,
    /// e.g. an avatar state's "hold".
    pub extra: Map<String, Value>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    Mpx,
    Png,
    Avatar,
}

#[derive(Clone, Debug)]
pub struct Doc {
    pub w: usize,
    pub h: usize,
    pub layers: Vec<Layer>,
    pub frames: Vec<Frame>,
    pub palette: Vec<[u8; 3]>,
    pub tags: Vec<Tag>,
    pub path: Option<PathBuf>,
    /// How :w saves it.
    pub kind: Kind,
}

impl Doc {
    /// A new transparent picture: one layer, one frame.
    pub fn new(w: i64, h: i64, palette_name: &str) -> Self {
        let (w, h) = (side(w), side(h));
        Doc {
            w,
            h,
            layers: vec![Layer { name: "layer 1".into(), visible: true }],
            frames: vec![Frame { cels: vec![blank(w, h)], ms: 100 }],
            palette: palette(palette_name),
            tags: vec![],
            path: None,
            kind: Kind::Mpx,
        }
    }

    // ── pixels ───────────────────────────────────────────────────────────

    /// Is (x, y) on the picture?
    pub fn inside(&self, x: i64, y: i64) -> bool {
        x >= 0 && y >= 0 && (x as usize) < self.w && (y as usize) < self.h
    }

    /// One pixel of one layer of one frame (transparent off the picture).
    pub fn get(&self, frame: usize, layer: usize, x: i64, y: i64) -> Px {
        if self.inside(x, y) { self.frames[frame].cels[layer][y as usize][x as usize] } else { None }
    }

    /// Set pixels on one cel; with `mirror` also their reflection across the vertical centre.
    pub fn put(&mut self, frame: usize, layer: usize, points: &[(i64, i64)], color: Px, mirror: bool) -> usize {
        let w = self.w as i64;
        let mut n = 0;
        let mut targets = vec![];
        for &(x, y) in points {
            targets.push((x, y));
            if mirror {
                targets.push((w - 1 - x, y));
            }
        }
        for (x, y) in targets {
            if self.inside(x, y) {
                let cell = &mut self.frames[frame].cels[layer][y as usize][x as usize];
                if *cell != color {
                    *cell = color;
                    n += 1;
                }
            }
        }
        n
    }

    /// A frame with its visible layers merged, bottom to top.
    pub fn composite(&self, frame: usize) -> Grid {
        let mut out = blank(self.w, self.h);
        for (layer, cel) in self.layers.iter().zip(&self.frames[frame].cels) {
            if !layer.visible {
                continue;
            }
            for (y, row) in cel.iter().enumerate() {
                for (x, c) in row.iter().enumerate() {
                    if c.is_some() {
                        out[y][x] = *c;
                    }
                }
            }
        }
        out
    }

    /// Every colour the picture uses, in first-seen order.
    pub fn colors_used(&self) -> Vec<[u8; 3]> {
        let mut seen = vec![];
        let mut set = BTreeSet::new();
        for f in &self.frames {
            for cel in &f.cels {
                for c in cel.iter().flatten().flatten() {
                    if set.insert(*c) {
                        seen.push(*c);
                    }
                }
            }
        }
        seen
    }

    /// Add the colours the picture uses to the palette (opened PNGs), up to MAX_PALETTE.
    pub fn adopt_colors(&mut self) {
        for c in self.colors_used() {
            if !self.palette.contains(&c) && self.palette.len() < MAX_PALETTE {
                self.palette.push(c);
            }
        }
    }

    // ── structure ────────────────────────────────────────────────────────

    /// A new empty layer above `above` (in every frame). Returns its index.
    pub fn add_layer(&mut self, above: usize) -> usize {
        let at = above + 1;
        let n = self.layers.len() + 1;
        self.layers.insert(at, Layer { name: format!("layer {n}"), visible: true });
        let (w, h) = (self.w, self.h);
        for f in &mut self.frames {
            f.cels.insert(at, blank(w, h));
        }
        at
    }

    /// Delete layer `i` (never the last one). Returns the layer to select.
    pub fn remove_layer(&mut self, i: usize) -> usize {
        if self.layers.len() == 1 {
            return i;
        }
        self.layers.remove(i);
        for f in &mut self.frames {
            f.cels.remove(i);
        }
        i.min(self.layers.len() - 1)
    }

    /// Move layer `i` up or down. Returns where it went.
    pub fn move_layer(&mut self, i: usize, step: isize) -> usize {
        let j = i as isize + step;
        if j < 0 || j as usize >= self.layers.len() {
            return i;
        }
        let j = j as usize;
        self.layers.swap(i, j);
        for f in &mut self.frames {
            f.cels.swap(i, j);
        }
        j
    }

    /// A new frame after `after` (empty, or a copy), growing a tag it lands inside. Returns its index.
    pub fn add_frame(&mut self, after: usize, copy_current: bool) -> usize {
        let src = &self.frames[after];
        let cels =
            if copy_current { src.cels.clone() } else { src.cels.iter().map(|_| blank(self.w, self.h)).collect() };
        let new = Frame { cels, ms: src.ms };
        self.frames.insert(after + 1, new);
        for t in &mut self.tags {
            // A frame added inside a tag grows it.
            if t.start > after {
                t.start += 1;
            }
            if t.end >= after {
                t.end += 1;
            }
        }
        after + 1
    }

    /// Delete frame `i` (never the last one), shrinking tags. Returns the frame to select.
    pub fn remove_frame(&mut self, i: usize) -> usize {
        if self.frames.len() == 1 {
            return i;
        }
        self.frames.remove(i);
        self.tags.retain_mut(|t| {
            if t.start > i {
                t.start -= 1;
            }
            if t.end >= i {
                if t.end == 0 {
                    return false;
                }
                t.end -= 1;
            }
            t.end >= t.start
        });
        i.min(self.frames.len() - 1)
    }

    /// Move frame `i` left or right. Returns where it went.
    pub fn move_frame(&mut self, i: usize, step: isize) -> usize {
        let j = i as isize + step;
        if j < 0 || j as usize >= self.frames.len() {
            return i;
        }
        self.frames.swap(i, j as usize);
        j as usize
    }

    /// The tag (named animation range) a frame is in.
    pub fn tag_at(&self, frame: usize) -> Option<&Tag> {
        self.tags.iter().find(|t| t.start <= frame && frame <= t.end)
    }

    // ── whole picture ────────────────────────────────────────────────────

    /// Change every layer of every frame the same way (flip, rotate, resize).
    fn each_cel(&mut self, f: impl Fn(&Grid) -> Grid) {
        for frame in &mut self.frames {
            frame.cels = frame.cels.iter().map(&f).collect();
        }
    }

    /// :flip h|v: mirror the whole picture.
    pub fn flip(&mut self, horizontal: bool) {
        if horizontal {
            self.each_cel(|c| c.iter().map(|row| row.iter().rev().copied().collect()).collect());
        } else {
            self.each_cel(|c| c.iter().rev().cloned().collect());
        }
    }

    /// :rotate cw|ccw: turn the whole picture a quarter (width and height swap).
    pub fn rotate(&mut self, clockwise: bool) {
        let (w, h) = (self.w, self.h);
        self.each_cel(|c| {
            (0..w)
                .map(|nx| (0..h).map(|ny| if clockwise { c[h - 1 - ny][nx] } else { c[ny][w - 1 - nx] }).collect())
                .collect()
        });
        (self.w, self.h) = (h, w);
    }

    /// Change the canvas size, keeping the picture at the top left (crop or add transparent).
    pub fn resize(&mut self, w: i64, h: i64) {
        let (w, h) = (side(w), side(h));
        let old_h = self.h;
        self.each_cel(|c| {
            (0..h)
                .map(
                    |y| {
                        if y < old_h { (0..w).map(|x| c[y].get(x).copied().flatten()).collect() } else { vec![None; w] }
                    },
                )
                .collect()
        });
        (self.w, self.h) = (w, h);
    }
}

// ── drawing ──────────────────────────────────────────────────────────────

/// Bresenham.
pub fn line(mut x0: i64, mut y0: i64, x1: i64, y1: i64) -> Vec<(i64, i64)> {
    let (dx, dy) = ((x1 - x0).abs(), -(y1 - y0).abs());
    let (sx, sy) = (if x0 < x1 { 1 } else { -1 }, if y0 < y1 { 1 } else { -1 });
    let mut err = dx + dy;
    let mut points = vec![];
    loop {
        points.push((x0, y0));
        if x0 == x1 && y0 == y1 {
            return points;
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

/// The pixels of a rectangle between two corners (outline, or filled).
pub fn rect(x0: i64, y0: i64, x1: i64, y1: i64, filled: bool) -> Vec<(i64, i64)> {
    let (xa, xb, ya, yb) = (x0.min(x1), x0.max(x1), y0.min(y1), y0.max(y1));
    let mut out = vec![];
    for y in ya..=yb {
        for x in xa..=xb {
            if filled || x == xa || x == xb || y == ya || y == yb {
                out.push((x, y));
            }
        }
    }
    out
}

/// The ellipse inside the box from (x0, y0) to (x1, y1), corners included in the box.
pub fn ellipse(x0: i64, y0: i64, x1: i64, y1: i64, filled: bool) -> Vec<(i64, i64)> {
    let (xa, xb, ya, yb) = (x0.min(x1), x0.max(x1), y0.min(y1), y0.max(y1));
    let (cx, cy, rx, ry) =
        ((xa + xb) as f64 / 2.0, (ya + yb) as f64 / 2.0, (xb - xa) as f64 / 2.0, (yb - ya) as f64 / 2.0);
    if rx == 0.0 || ry == 0.0 {
        return line(xa, ya, xb, yb);
    }
    let inside =
        |x: i64, y: i64| ((x as f64 - cx) / (rx + 0.5)).powi(2) + ((y as f64 - cy) / (ry + 0.5)).powi(2) <= 1.0;
    let area: BTreeSet<(i64, i64)> =
        (ya..=yb).flat_map(|y| (xa..=xb).map(move |x| (x, y))).filter(|&(x, y)| inside(x, y)).collect();
    let mut out: Vec<(i64, i64)> = if filled {
        area.iter().copied().collect()
    } else {
        // The edge: a 4-neighbour falls outside.
        area.iter()
            .copied()
            .filter(|(x, y)| {
                [(1, 0), (-1, 0), (0, 1), (0, -1)].iter().any(|(dx, dy)| !area.contains(&(x + dx, y + dy)))
            })
            .collect()
    };
    out.sort();
    out
}

/// The 4-connected area of the colour at (x, y).
pub fn flood(cel: &Grid, x: i64, y: i64) -> Vec<(i64, i64)> {
    let (h, w) = (cel.len() as i64, cel.first().map_or(0, |r| r.len()) as i64);
    if !(0..w).contains(&x) || !(0..h).contains(&y) {
        return vec![];
    }
    let target = cel[y as usize][x as usize];
    let mut seen = BTreeSet::new();
    let mut stack = vec![(x, y)];
    while let Some((px, py)) = stack.pop() {
        if seen.contains(&(px, py))
            || !(0..w).contains(&px)
            || !(0..h).contains(&py)
            || cel[py as usize][px as usize] != target
        {
            continue;
        }
        seen.insert((px, py));
        stack.extend([(px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)]);
    }
    seen.into_iter().collect()
}

/// Undo/redo by snapshots of the document (small sprites: cheap).
#[derive(Default)]
pub struct History {
    undo: Vec<Doc>,
    redo: Vec<Doc>,
}

impl History {
    /// Remember the picture before a change (clears redo).
    pub fn push(&mut self, doc: &Doc) {
        self.undo.push(doc.clone());
        if self.undo.len() > UNDO_DEPTH {
            self.undo.remove(0);
        }
        self.redo.clear();
    }

    /// Undo (back) or redo; the path and kind stay as they are.
    pub fn step(&mut self, doc: &mut Doc, back: bool) -> bool {
        let (src, dst) = if back { (&mut self.undo, &mut self.redo) } else { (&mut self.redo, &mut self.undo) };
        let Some(mut snap) = src.pop() else { return false };
        dst.push(doc.clone());
        snap.path = doc.path.clone();
        snap.kind = doc.kind;
        *doc = snap;
        true
    }
}

// ── files ────────────────────────────────────────────────────────────────

/// A picture as .mpx JSON (the same as the Python client wrote).
pub fn to_json(doc: &Doc) -> Value {
    let px = |c: &Px| c.map_or(Value::Null, |c| Value::String(hex(c)));
    json!({
        "version": 1,
        "w": doc.w,
        "h": doc.h,
        "palette": doc.palette.iter().map(|c| hex(*c)).collect::<Vec<_>>(),
        "layers": doc.layers.iter().map(|l| json!({"name": l.name, "visible": l.visible})).collect::<Vec<_>>(),
        "frames": doc.frames.iter().map(|f| json!({"ms": f.ms, "cels": f.cels.iter().map(|cel| cel.iter().map(|row| row.iter().map(px).collect::<Vec<_>>()).collect::<Vec<_>>()).collect::<Vec<_>>()})).collect::<Vec<_>>(),
        "tags": doc.tags.iter().map(|t| {
            let mut m = t.extra.clone();
            m.insert("name".into(), json!(t.name));
            m.insert("start".into(), json!(t.start));
            m.insert("end".into(), json!(t.end));
            Value::Object(m)
        }).collect::<Vec<_>>(),
    })
}

/// A picture from .mpx JSON (missing parts get defaults).
pub fn from_json(data: &Value, path: Option<PathBuf>) -> Doc {
    let (w, h) = (side(data["w"].as_i64().unwrap_or(32)), side(data["h"].as_i64().unwrap_or(32)));
    let mut layers: Vec<Layer> = data["layers"]
        .as_array()
        .map(|ls| {
            ls.iter()
                .enumerate()
                .map(|(i, l)| Layer {
                    name: l["name"].as_str().filter(|n| !n.is_empty()).map_or(format!("layer {}", i + 1), String::from),
                    visible: l["visible"].as_bool().unwrap_or(true),
                })
                .collect()
        })
        .unwrap_or_default();
    if layers.is_empty() {
        layers.push(Layer { name: "layer 1".into(), visible: true });
    }
    let cell = |v: &Value| v.as_str().and_then(parse_color);
    let mut frames: Vec<Frame> = data["frames"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|f| {
            let mut cels: Vec<Grid> = f["cels"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|cel| (0..h).map(|y| (0..w).map(|x| cell(&cel[y][x])).collect()).collect())
                .collect();
            cels.resize(layers.len(), blank(w, h));
            Frame { cels, ms: f["ms"].as_u64().filter(|m| *m > 0).unwrap_or(100) as u32 }
        })
        .collect();
    if frames.is_empty() {
        frames.push(Frame { cels: layers.iter().map(|_| blank(w, h)).collect(), ms: 100 });
    }
    let tags = data["tags"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|t| {
            let extra = t
                .as_object()?
                .iter()
                .filter(|(k, _)| !matches!(k.as_str(), "name" | "start" | "end"))
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect();
            Some(Tag {
                name: t["name"].as_str()?.to_string(),
                start: t["start"].as_u64()? as usize,
                end: t["end"].as_u64()? as usize,
                extra,
            })
        })
        .collect();
    let mut pal: Vec<[u8; 3]> = data["palette"].as_array().into_iter().flatten().filter_map(cell).collect();
    if pal.is_empty() {
        pal = palette("db32");
    }
    Doc { w, h, layers, frames, palette: pal, tags, path, kind: Kind::Mpx }
}

/// The frames of a PNG: a strip of square frames, else one.
fn image_frames(path: &Path, frame_w: Option<usize>) -> Result<Vec<Grid>> {
    let img = image::open(path).with_context(|| format!("{}", path.display()))?.to_rgba8();
    let (iw, ih) = (img.width() as usize, img.height() as usize);
    let fw = frame_w.unwrap_or(if ih > 0 && iw % ih == 0 { ih } else { iw });
    Ok((0..(iw / fw.max(1)).max(1))
        .map(|f| {
            (0..ih)
                .map(|y| {
                    (0..fw)
                        .map(|x| {
                            let p = img.get_pixel((f * fw + x) as u32, y as u32);
                            (p[3] >= 128).then_some([p[0], p[1], p[2]])
                        })
                        .collect()
                })
                .collect()
        })
        .collect())
}

/// Frames side by side as RGBA, scaled up with square pixels.
fn image(grids: &[Grid], scale: usize) -> image::RgbaImage {
    let (h, w) = (grids[0].len(), grids[0][0].len());
    let s = scale.max(1);
    let mut img = image::RgbaImage::new((w * grids.len() * s) as u32, (h * s) as u32);
    for (f, grid) in grids.iter().enumerate() {
        for (y, row) in grid.iter().enumerate() {
            for (x, c) in row.iter().enumerate() {
                if let Some(c) = c {
                    for dy in 0..s {
                        for dx in 0..s {
                            img.put_pixel(
                                ((f * w + x) * s + dx) as u32,
                                (y * s + dy) as u32,
                                image::Rgba([c[0], c[1], c[2], 255]),
                            );
                        }
                    }
                }
            }
        }
    }
    img
}

/// Open a picture: an avatar folder, a .png (a strip of frames), or an .mpx.
pub fn open_any(path: &Path) -> Result<Doc> {
    if path.is_dir() {
        return load_avatar(path);
    }
    if path.extension().is_some_and(|e| e.eq_ignore_ascii_case("png")) {
        let grids = image_frames(path, None)?;
        let (h, w) = (grids[0].len(), grids[0][0].len());
        let mut doc = Doc {
            w,
            h,
            layers: vec![Layer { name: "layer 1".into(), visible: true }],
            frames: grids.into_iter().map(|g| Frame { cels: vec![g], ms: 100 }).collect(),
            palette: vec![],
            tags: vec![],
            path: Some(path.to_path_buf()),
            kind: Kind::Png,
        };
        doc.adopt_colors();
        return Ok(doc);
    }
    let text = std::fs::read_to_string(path).with_context(|| format!("{}", path.display()))?;
    Ok(from_json(&serde_json::from_str(&text)?, Some(path.to_path_buf())))
}

/// Save the way it was opened (or as `path`'s type): .mpx, .png strip, or the avatar folder.
pub fn save(doc: &mut Doc, path: Option<&Path>) -> Result<PathBuf> {
    let mut path = path
        .map(Path::to_path_buf)
        .or_else(|| doc.path.clone())
        .ok_or_else(|| anyhow!("no file name yet: :w name.mpx"))?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    if doc.kind == Kind::Avatar && path.is_dir() {
        save_avatar(doc, &path)?;
    } else if path.extension().is_some_and(|e| e.eq_ignore_ascii_case("png")) {
        let grids: Vec<Grid> = (0..doc.frames.len()).map(|i| doc.composite(i)).collect();
        image(&grids, 1).save(&path)?;
        doc.kind = Kind::Png;
    } else {
        if path.extension().is_none() {
            path.set_extension("mpx");
        }
        let tmp = path.with_extension("mpx.tmp");
        std::fs::write(&tmp, serde_json::to_string(&to_json(doc))?)?;
        std::fs::rename(&tmp, &path)?;
        doc.kind = Kind::Mpx;
    }
    doc.path = Some(path.clone());
    Ok(path)
}

/// :export's kinds.
pub enum Export {
    /// One frame.
    Png(usize),
    /// Every frame side by side.
    Strip,
    /// A grid of `columns` per row.
    Sheet(usize),
}

/// Export as PNG: one frame, a strip, or a sheet, each pixel `scale` times bigger.
pub fn export(doc: &Doc, path: &Path, how: Export, scale: usize) -> Result<PathBuf> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    match how {
        Export::Png(frame) => image(&[doc.composite(frame)], scale).save(path)?,
        Export::Strip => {
            image(&(0..doc.frames.len()).map(|i| doc.composite(i)).collect::<Vec<_>>(), scale).save(path)?
        }
        Export::Sheet(columns) => {
            let columns = columns.max(1);
            let grids: Vec<Grid> = (0..doc.frames.len()).map(|i| doc.composite(i)).collect();
            let strip = image(&grids, scale);
            let (fw, fh) = ((doc.w * scale.max(1)) as u32, (doc.h * scale.max(1)) as u32);
            let rows = grids.len().div_ceil(columns) as u32;
            let mut sheet = image::RgbaImage::new(fw * columns.min(grids.len()) as u32, fh * rows);
            for i in 0..grids.len() as u32 {
                let tile = image::imageops::crop_imm(&strip, i * fw, 0, fw, fh).to_image();
                image::imageops::replace(
                    &mut sheet,
                    &tile,
                    ((i % columns as u32) * fw) as i64,
                    ((i / columns as u32) * fh) as i64,
                );
            }
            sheet.save(path)?;
        }
    }
    Ok(path.to_path_buf())
}

/// Mabel's avatar folder (avatar.json + a PNG strip per state) as one picture, states as tags.
pub fn load_avatar(folder: &Path) -> Result<Doc> {
    let manifest: Value = serde_json::from_str(&std::fs::read_to_string(folder.join("avatar.json"))?)?;
    let (mut frames, mut tags, mut size) = (vec![], vec![], None);
    for (name, spec) in manifest.as_object().into_iter().flatten() {
        let Some(file) = spec["file"].as_str() else { continue };
        let grids = image_frames(&folder.join(file), None)?;
        size = size.or(Some((grids[0][0].len(), grids[0].len())));
        let ms: Vec<u32> =
            spec["ms"].as_array().into_iter().flatten().filter_map(|m| m.as_u64()).map(|m| m as u32).collect();
        let start = frames.len();
        for (i, g) in grids.into_iter().enumerate() {
            let t = ms.get(i).or(ms.last()).copied().unwrap_or(100);
            frames.push(Frame { cels: vec![g], ms: t });
        }
        let extra = spec
            .as_object()
            .into_iter()
            .flatten()
            .filter(|(k, _)| *k != "file" && *k != "ms")
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        tags.push(Tag { name: name.clone(), start, end: frames.len() - 1, extra });
    }
    let Some((w, h)) = size else { bail!("{}/avatar.json lists no strips", folder.display()) };
    let mut doc = Doc {
        w,
        h,
        layers: vec![Layer { name: "layer 1".into(), visible: true }],
        frames,
        palette: vec![],
        tags,
        path: Some(folder.to_path_buf()),
        kind: Kind::Avatar,
    };
    doc.adopt_colors();
    Ok(doc)
}

/// Each tag back to its strip (`<tag>.png` unless the manifest names the file) and its timings.
pub fn save_avatar(doc: &Doc, folder: &Path) -> Result<()> {
    let path = folder.join("avatar.json");
    let mut manifest: Map<String, Value> =
        std::fs::read_to_string(&path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    for tag in &doc.tags {
        let spec = manifest.get(&tag.name).and_then(|s| s.as_object()).cloned().unwrap_or_default();
        let file = spec.get("file").and_then(|f| f.as_str()).map_or(format!("{}.png", tag.name), String::from);
        let grids: Vec<Grid> = (tag.start..=tag.end).map(|i| doc.composite(i)).collect();
        image(&grids, 1).save(folder.join(&file))?;
        let mut new = spec;
        new.insert("file".into(), json!(file));
        new.insert("ms".into(), json!((tag.start..=tag.end).map(|i| doc.frames[i].ms).collect::<Vec<_>>()));
        for (k, v) in &tag.extra {
            new.insert(k.clone(), v.clone());
        }
        manifest.insert(tag.name.clone(), Value::Object(new));
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(&Value::Object(manifest))? + "\n")?;
    std::fs::rename(&tmp, &path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shapes() {
        assert_eq!(line(0, 0, 3, 1), vec![(0, 0), (1, 0), (2, 1), (3, 1)]);
        assert_eq!(rect(0, 0, 2, 2, false).len(), 8);
        assert_eq!(rect(0, 0, 2, 2, true).len(), 9);
        let e = ellipse(0, 0, 6, 4, false);
        assert!(e.contains(&(3, 0)) && e.contains(&(0, 2)) && !e.contains(&(3, 2)));
        let mut cel = blank(4, 4);
        cel[1][1] = Some([1, 2, 3]);
        assert_eq!(flood(&cel, 0, 0).len(), 15);
    }

    #[test]
    fn layers_frames_tags_and_undo() {
        let mut d = Doc::new(4, 3, "pico8");
        d.put(0, 0, &[(0, 0)], Some([255, 0, 0]), true);
        assert_eq!(d.get(0, 0, 3, 0), Some([255, 0, 0]), "mirrored");
        d.tags.push(Tag { name: "idle".into(), start: 0, end: 0, extra: Map::new() });
        let f = d.add_frame(0, true);
        assert_eq!((f, d.tags[0].end), (1, 1), "a frame added inside a tag grows it");
        let l = d.add_layer(0);
        d.put(1, l, &[(1, 1)], Some([0, 0, 255]), false);
        assert_eq!(d.composite(1)[1][1], Some([0, 0, 255]));
        let mut h = History::default();
        h.push(&d);
        d.rotate(true);
        assert_eq!((d.w, d.h), (3, 4));
        assert!(h.step(&mut d, true));
        assert_eq!((d.w, d.h), (4, 3));
        d.resize(2, 5);
        assert_eq!((d.frames[0].cels[0].len(), d.frames[0].cels[0][0].len()), (5, 2));
    }

    #[test]
    fn mpx_and_png_round_trips() {
        let dir = std::env::temp_dir().join(format!("mabel-pixel-{}", std::process::id()));
        let mut d = Doc::new(3, 2, "gameboy");
        d.put(0, 0, &[(2, 1)], Some([1, 2, 3]), false);
        let p = save(&mut d, Some(&dir.join("a"))).unwrap();
        assert_eq!(p.extension().unwrap(), "mpx");
        let back = open_any(&p).unwrap();
        assert_eq!(back.get(0, 0, 2, 1), Some([1, 2, 3]));
        d.add_frame(0, false);
        let png = dir.join("strip.png");
        save(&mut d, Some(&png)).unwrap();
        let strip = open_any(&png).unwrap();
        assert_eq!((strip.frames.len(), strip.w), (3, 2), "a 6×2 strip opens as square frames, as in Python");
        assert!(strip.palette.contains(&[1, 2, 3]));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
