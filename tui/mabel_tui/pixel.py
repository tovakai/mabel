"""PIXEL's model: a sprite document with layers and animation frames, drawing, and files.

Our own code (TPA, github.com/tlofreso/terminal-pixel-art, carries no licence; only its feature
list was used as a checklist). No Textual here, so it's easy to test; pixelview.py draws it.

A document is w×h pixels, one or more layers, one or more frames (each with a duration in ms
and one cel per layer) and tags naming frame ranges. A pixel is "#rrggbb" or None (transparent);
layers stack bottom to top, the top non-transparent pixel wins.

Files:
- .mpx   the native format, JSON (layers, frames, tags, palette)
- .png   one image, or a horizontal strip of square frames (opened as frames, saved the same way)
- an avatar folder (avatar.json + one strip per state, like assets/avatar): each state is a tag;
  saving writes every tag back as its strip and the frame timings into avatar.json
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

Color = str | None
Grid = list[list[Color]]

PALETTES: dict[str, list[str]] = {
    "pico8": ["#000000", "#1d2b53", "#7e2553", "#008751", "#ab5236", "#5f574f", "#c2c3c7", "#fff1e8",
              "#ff004d", "#ffa300", "#ffec27", "#00e436", "#29adff", "#83769c", "#ff77a8", "#ffccaa"],
    "db16": ["#140c1c", "#442434", "#30346d", "#4e4a4e", "#854c30", "#346524", "#d04648", "#757161",
             "#597dce", "#d27d2c", "#8595a1", "#6daa2c", "#d2aa99", "#6dc2ca", "#dad45e", "#deeed6"],
    "db32": ["#000000", "#222034", "#45283c", "#663931", "#8f563b", "#df7126", "#d9a066", "#eec39a",
             "#fbf236", "#99e550", "#6abe30", "#37946e", "#4b692f", "#524b24", "#323c39", "#3f3f74",
             "#306082", "#5b6ee1", "#639bff", "#5fcde4", "#cbdbfc", "#ffffff", "#9badb7", "#847e87",
             "#696a6a", "#595652", "#76428a", "#ac3232", "#d95763", "#d77bba", "#8f974a", "#8a6f30"],
    "gameboy": ["#0f380f", "#306230", "#8bac0f", "#9bbc0f"],
    "grayscale": ["#000000", "#242424", "#494949", "#6d6d6d", "#929292", "#b6b6b6", "#dbdbdb", "#ffffff"],
    "phosphor": ["#0c0804", "#3d2a08", "#6b1d0a", "#a86b00", "#ffb000", "#ffd066", "#d9cba4", "#8a7b58",
                 "#3fd6c6", "#ff5a36", "#ff8a3d", "#9fd8a8"],
}
MAX_PALETTE = 64
MAX_SIDE = 256
UNDO_DEPTH = 100


def blank(w: int, h: int) -> Grid:
    return [[None] * w for _ in range(h)]


@dataclass
class Layer:
    name: str
    visible: bool = True


@dataclass
class Frame:
    cels: list[Grid]                      # one per layer, bottom first
    ms: int = 100


@dataclass
class Tag:
    name: str
    start: int
    end: int                              # inclusive
    extra: dict = field(default_factory=dict)     # e.g. an avatar state's "hold"


@dataclass
class Doc:
    w: int
    h: int
    layers: list[Layer]
    frames: list[Frame]
    palette: list[str]
    tags: list[Tag] = field(default_factory=list)
    path: Path | None = None
    kind: str = "mpx"                     # mpx | png | avatar: how :w saves it

    @classmethod
    def new(cls, w: int = 32, h: int = 32, palette: str = "db32") -> "Doc":
        w, h = _side(w), _side(h)
        return cls(w, h, [Layer("layer 1")], [Frame([blank(w, h)])], list(PALETTES.get(palette, PALETTES["db32"])))

    # --- pixels -----------------------------------------------------------------------------

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def get(self, frame: int, layer: int, x: int, y: int) -> Color:
        return self.frames[frame].cels[layer][y][x] if self.inside(x, y) else None

    def put(self, frame: int, layer: int, points, color: Color, mirror: bool = False) -> int:
        """Set pixels on one cel; with mirror also their reflection across the vertical centre line."""
        cel, n = self.frames[frame].cels[layer], 0
        for x, y in points:
            for px in ((x, self.w - 1 - x) if mirror else (x,)):
                if self.inside(px, y) and cel[y][px] != color:
                    cel[y][px] = color
                    n += 1
        return n

    def composite(self, frame: int) -> Grid:
        out = blank(self.w, self.h)
        for layer, cel in zip(self.layers, self.frames[frame].cels):
            if not layer.visible:
                continue
            for y, row in enumerate(cel):
                for x, c in enumerate(row):
                    if c is not None:
                        out[y][x] = c
        return out

    def colors_used(self) -> list[str]:
        seen: dict[str, None] = {}
        for f in self.frames:
            for cel in f.cels:
                for row in cel:
                    for c in row:
                        if c is not None:
                            seen.setdefault(c, None)
        return list(seen)

    def adopt_colors(self) -> None:
        """Add the colours the picture uses to the palette (opened PNGs), up to MAX_PALETTE."""
        for c in self.colors_used():
            if c not in self.palette and len(self.palette) < MAX_PALETTE:
                self.palette.append(c)

    # --- structure --------------------------------------------------------------------------

    def add_layer(self, above: int) -> int:
        at = above + 1
        self.layers.insert(at, Layer(f"layer {len(self.layers) + 1}"))
        for f in self.frames:
            f.cels.insert(at, blank(self.w, self.h))
        return at

    def remove_layer(self, i: int) -> int:
        if len(self.layers) == 1:
            return i
        del self.layers[i]
        for f in self.frames:
            del f.cels[i]
        return min(i, len(self.layers) - 1)

    def move_layer(self, i: int, step: int) -> int:
        j = i + step
        if not 0 <= j < len(self.layers):
            return i
        self.layers[i], self.layers[j] = self.layers[j], self.layers[i]
        for f in self.frames:
            f.cels[i], f.cels[j] = f.cels[j], f.cels[i]
        return j

    def add_frame(self, after: int, copy_current: bool = True) -> int:
        src = self.frames[after]
        new = Frame([copy.deepcopy(c) if copy_current else blank(self.w, self.h) for c in src.cels], src.ms)
        self.frames.insert(after + 1, new)
        for t in self.tags:                         # a frame added inside a tag grows it
            if t.start > after:
                t.start += 1
            if t.end >= after:
                t.end += 1
        return after + 1

    def remove_frame(self, i: int) -> int:
        if len(self.frames) == 1:
            return i
        del self.frames[i]
        for t in list(self.tags):
            if t.start > i:
                t.start -= 1
            if t.end >= i:
                t.end -= 1
            if t.end < t.start:
                self.tags.remove(t)
        return min(i, len(self.frames) - 1)

    def move_frame(self, i: int, step: int) -> int:
        j = i + step
        if not 0 <= j < len(self.frames):
            return i
        self.frames[i], self.frames[j] = self.frames[j], self.frames[i]
        return j

    def tag_at(self, frame: int) -> Tag | None:
        return next((t for t in self.tags if t.start <= frame <= t.end), None)

    # --- whole picture ----------------------------------------------------------------------

    def _each_cel(self, fn) -> None:
        for f in self.frames:
            f.cels = [fn(c) for c in f.cels]

    def flip(self, axis: str) -> None:
        self._each_cel((lambda c: [row[::-1] for row in c]) if axis == "h" else (lambda c: c[::-1]))

    def rotate(self, clockwise: bool = True) -> None:
        if clockwise:
            self._each_cel(lambda c: [list(row) for row in zip(*c[::-1])])
        else:
            self._each_cel(lambda c: [list(row) for row in zip(*c)][::-1])
        self.w, self.h = self.h, self.w

    def resize(self, w: int, h: int) -> None:
        """Change the canvas size, keeping the picture at the top left (crop or add transparent)."""
        w, h = _side(w), _side(h)
        self._each_cel(lambda c: [(c[y][:w] + [None] * max(0, w - self.w)) if y < self.h else [None] * w
                                  for y in range(h)])
        self.w, self.h = w, h


def _side(n) -> int:
    return max(1, min(MAX_SIDE, int(n)))


# --- drawing --------------------------------------------------------------------------------

def line(x0: int, y0: int, x1: int, y1: int) -> list[tuple[int, int]]:
    """Bresenham."""
    points, dx, dy = [], abs(x1 - x0), -abs(y1 - y0)
    sx, sy, err = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1), dx + dy
    while True:
        points.append((x0, y0))
        if x0 == x1 and y0 == y1:
            return points
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def rect(x0: int, y0: int, x1: int, y1: int, filled: bool = False) -> list[tuple[int, int]]:
    xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    if filled:
        return [(x, y) for y in range(ya, yb + 1) for x in range(xa, xb + 1)]
    return [(x, y) for y in range(ya, yb + 1) for x in range(xa, xb + 1)
            if x in (xa, xb) or y in (ya, yb)]


def ellipse(x0: int, y0: int, x1: int, y1: int, filled: bool = False) -> list[tuple[int, int]]:
    """The ellipse inside the box from (x0, y0) to (x1, y1), corners included in the box."""
    xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    cx, cy, rx, ry = (xa + xb) / 2, (ya + yb) / 2, (xb - xa) / 2, (yb - ya) / 2
    if rx == 0 or ry == 0:
        return line(xa, ya, xb, yb)

    def inside(x, y):
        return ((x - cx) / (rx + 0.5)) ** 2 + ((y - cy) / (ry + 0.5)) ** 2 <= 1
    area = {(x, y) for y in range(ya, yb + 1) for x in range(xa, xb + 1) if inside(x, y)}
    if filled:
        return sorted(area)
    return sorted(p for p in area                       # the edge: a 4-neighbour falls outside
                  if any((p[0] + dx, p[1] + dy) not in area for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))))


def flood(cel: Grid, x: int, y: int) -> list[tuple[int, int]]:
    """The 4-connected area of the colour at (x, y)."""
    h, w = len(cel), len(cel[0]) if cel else 0
    if not (0 <= x < w and 0 <= y < h):
        return []
    target, seen, stack = cel[y][x], set(), [(x, y)]
    while stack:
        px, py = stack.pop()
        if (px, py) in seen or not (0 <= px < w and 0 <= py < h) or cel[py][px] != target:
            continue
        seen.add((px, py))
        stack += [(px + 1, py), (px - 1, py), (px, py + 1), (px, py - 1)]
    return sorted(seen)


class History:
    """Undo/redo by snapshots of the document's pictures and structure (small sprites: cheap)."""

    def __init__(self) -> None:
        self.undo: list[dict] = []
        self.redo: list[dict] = []

    @staticmethod
    def snapshot(doc: Doc) -> dict:
        return copy.deepcopy({"w": doc.w, "h": doc.h, "layers": doc.layers, "frames": doc.frames,
                              "tags": doc.tags, "palette": doc.palette})

    @staticmethod
    def restore(doc: Doc, snap: dict) -> None:
        for k, v in copy.deepcopy(snap).items():
            setattr(doc, k, v)

    def push(self, doc: Doc) -> None:
        self.undo.append(self.snapshot(doc))
        del self.undo[:-UNDO_DEPTH]
        self.redo.clear()

    def step(self, doc: Doc, back: bool) -> bool:
        src, dst = (self.undo, self.redo) if back else (self.redo, self.undo)
        if not src:
            return False
        dst.append(self.snapshot(doc))
        self.restore(doc, src.pop())
        return True


# --- files ----------------------------------------------------------------------------------

def to_json(doc: Doc) -> dict:
    return {"version": 1, "w": doc.w, "h": doc.h, "palette": doc.palette,
            "layers": [{"name": l.name, "visible": l.visible} for l in doc.layers],
            "frames": [{"ms": f.ms, "cels": f.cels} for f in doc.frames],
            "tags": [{"name": t.name, "start": t.start, "end": t.end, **t.extra} for t in doc.tags]}


def from_json(data: dict, path: Path | None = None) -> Doc:
    w, h = _side(data["w"]), _side(data["h"])
    layers = [Layer(str(l.get("name") or f"layer {i + 1}"), bool(l.get("visible", True)))
              for i, l in enumerate(data.get("layers") or [{}])]
    frames = []
    for f in data.get("frames") or []:
        cels = [[[_color(c) for c in (row + [None] * w)[:w]] for row in (cel + [[]] * h)[:h]]
                for cel in (f.get("cels") or [])]
        cels = (cels + [blank(w, h) for _ in layers])[:len(layers)]
        frames.append(Frame(cels, int(f.get("ms") or 100)))
    tags = [Tag(str(t["name"]), int(t["start"]), int(t["end"]),
                {k: v for k, v in t.items() if k not in ("name", "start", "end")}) for t in data.get("tags") or []]
    palette = [c for c in map(_color, data.get("palette") or []) if c] or list(PALETTES["db32"])
    return Doc(w, h, layers, frames or [Frame([blank(w, h) for _ in layers])], palette, tags, path, "mpx")


def _color(c) -> Color:
    if not isinstance(c, str) or not c.startswith("#"):
        return None
    c = c.lower()
    if len(c) == 4:
        c = "#" + "".join(ch * 2 for ch in c[1:])
    return c[:7] if len(c) in (7, 9) and all(ch in "0123456789abcdef" for ch in c[1:]) else None


def _image_frames(path: Path, frame_w: int | None = None) -> list[Grid]:
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    if frame_w is None:
        frame_w = im.height if im.width % im.height == 0 else im.width
    frames = []
    for f in range(max(1, im.width // frame_w)):
        px = im.crop((f * frame_w, 0, (f + 1) * frame_w, im.height)).load()
        frames.append([["#%02x%02x%02x" % px[x, y][:3] if px[x, y][3] >= 128 else None
                        for x in range(frame_w)] for y in range(im.height)])
    return frames


def _image(grids: list[Grid], scale: int = 1):
    from PIL import Image
    h, w = len(grids[0]), len(grids[0][0])
    im = Image.new("RGBA", (w * len(grids), h), (0, 0, 0, 0))
    px = im.load()
    for f, grid in enumerate(grids):
        for y, row in enumerate(grid):
            for x, c in enumerate(row):
                if c is not None:
                    px[f * w + x, y] = (int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16), 255)
    return im.resize((im.width * scale, im.height * scale), Image.NEAREST) if scale > 1 else im


def open_any(path: Path) -> Doc:
    path = Path(path).expanduser()
    if path.is_dir():
        return load_avatar(path)
    if path.suffix.lower() == ".png":
        grids = _image_frames(path)
        h, w = len(grids[0]), len(grids[0][0])
        doc = Doc(w, h, [Layer("layer 1")], [Frame([g]) for g in grids], [], [], path, "png")
        doc.adopt_colors()
        return doc
    return from_json(json.loads(path.read_text(encoding="utf-8")), path)


def save(doc: Doc, path: Path | None = None) -> Path:
    """Save the way it was opened (or as ``path``'s type): .mpx, .png strip, or the avatar folder."""
    path = Path(path).expanduser() if path else doc.path
    if path is None:
        raise ValueError("no file name yet: :w name.mpx")
    path.parent.mkdir(parents=True, exist_ok=True)
    if doc.kind == "avatar" and path.is_dir():
        save_avatar(doc, path)
    elif path.suffix.lower() == ".png":
        _image([doc.composite(i) for i in range(len(doc.frames))]).save(path)
        doc.kind = "png"
    else:
        if not path.suffix:
            path = path.with_suffix(".mpx")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(to_json(doc), separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
        doc.kind = "mpx"
    doc.path = path
    return path


def export(doc: Doc, path: Path, *, how: str = "png", frame: int = 0, scale: int = 1, columns: int = 0) -> Path:
    """png: one frame · strip: every frame side by side · sheet: a grid of ``columns`` per row."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    grids = [doc.composite(frame)] if how == "png" else [doc.composite(i) for i in range(len(doc.frames))]
    if how == "sheet" and columns > 0:
        from PIL import Image
        strip = _image(grids, scale)
        fw, fh = doc.w * scale, doc.h * scale
        rows = -(-len(grids) // columns)
        sheet = Image.new("RGBA", (fw * min(columns, len(grids)), fh * rows), (0, 0, 0, 0))
        for i in range(len(grids)):
            sheet.paste(strip.crop((i * fw, 0, (i + 1) * fw, fh)), ((i % columns) * fw, (i // columns) * fh))
        sheet.save(path)
    else:
        _image(grids, scale).save(path)
    return path


def load_avatar(folder: Path) -> Doc:
    manifest = json.loads((folder / "avatar.json").read_text(encoding="utf-8"))
    frames, tags, size = [], [], None
    for name, spec in manifest.items():
        if not (isinstance(spec, dict) and spec.get("file")):
            continue
        grids = _image_frames(folder / spec["file"])
        size = size or (len(grids[0][0]), len(grids[0]))
        ms = [int(m) for m in spec.get("ms") or []]
        start = len(frames)
        for i, g in enumerate(grids):
            frames.append(Frame([g], ms[i] if i < len(ms) else (ms[-1] if ms else 100)))
        tags.append(Tag(name, start, len(frames) - 1, {k: v for k, v in spec.items() if k not in ("file", "ms")}))
    if not frames:
        raise ValueError(f"{folder}/avatar.json lists no strips")
    doc = Doc(size[0], size[1], [Layer("layer 1")], frames, [], tags, folder, "avatar")
    doc.adopt_colors()
    return doc


def save_avatar(doc: Doc, folder: Path) -> None:
    """Each tag back to its strip (<tag>.png unless the manifest names the file) and its timings."""
    path = folder / "avatar.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for tag in doc.tags:
        spec = manifest.get(tag.name) if isinstance(manifest.get(tag.name), dict) else {}
        name = spec.get("file") or f"{tag.name}.png"
        _image([doc.composite(i) for i in range(tag.start, tag.end + 1)]).save(folder / name)
        manifest[tag.name] = {**spec, "file": name, "ms": [doc.frames[i].ms for i in range(tag.start, tag.end + 1)],
                              **tag.extra}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
