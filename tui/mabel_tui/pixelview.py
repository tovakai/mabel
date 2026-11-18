"""PIXEL (CREATIVE's first app): a keyboard-first sprite editor. The model is pixel.py.

Left: TOOLS (with fg/bg and toggles) and PALETTE. Centre: the CANVAS. Right: PREVIEW (actual
size, plays the animation), LAYERS and FRAMES. Everything has a key on the canvas, so you rarely
leave it; rarer things are `:` commands (vim-like). Zoom 1 is exact half-blocks (1×2 pixels per
cell, like Mabel's avatar); zoom 2+ draws square pixels (2n×n cells) with a cursor box.

Canvas keys: hjkl/arrows move (HJKL ×4) · space/enter use the tool · t pen down (draw while
moving) · b pencil e eraser f fill n line r rect o ellipse c pick · F filled shapes · m mirror ·
g grid · O onion skin · +/- zoom (0 fits) · P palette · , . or [ ] colour · x swap fg/bg · # hex colour · PgUp/PgDn or { } layer · A add
layer · V hide layer · < > frame · N new frame · p play · u undo · ctrl+r redo · ctrl+s save · : command
"""
from __future__ import annotations

import time
from pathlib import Path

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.geometry import Region, Size
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Static

from . import pixel as px
from .config import CACHE
from .palette import P

TOOLS = [("b", "pencil"), ("e", "eraser"), ("f", "fill"), ("n", "line"), ("r", "rect"), ("o", "ellipse"),
         ("c", "pick")]
SHAPES = {"line", "rect", "ellipse"}
CHECKER = ("#1b1510", "#251d15")
LAST_FILE = CACHE / "pixel-last"
SWATCHES_PER_ROW = 8

COMMANDS = """:new W H [palette]   :w [file]   :e file   :avatar   :e! / :new!  (drop changes)
:export png|strip|sheet [scale] [file] [columns]   :palette NAME   (pico8 db16 db32 gameboy grayscale phosphor)
:flip h|v   :rotate cw|ccw   :resize W H   :ms N   :ms all N   :tag NAME FROM TO   :untag NAME
:layer name NAME   :layer del   :layer up|down   :frame del   :frame left|right   :hex #rrggbb"""


def _rgb(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _contrast(c: str | None) -> str:
    if c is None:
        return P.amber_hi
    r, g, b = _rgb(c)
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#ffffff"


def _dim(c: str, toward: str = "#140e07", amount: float = 0.6) -> str:
    a, b = _rgb(c), _rgb(toward)
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * amount) for x, y in zip(a, b))


def _checker(x: int, y: int) -> str:
    return CHECKER[(x // 2 + y // 2) % 2]


class Canvas(ScrollView, can_focus=True):
    BINDINGS = [
        Binding("up,k", "move(0,-1)", show=False), Binding("down,j", "move(0,1)", show=False),
        Binding("left,h", "move(-1,0)", show=False), Binding("right,l", "move(1,0)", show=False),
        Binding("shift+up,K", "move(0,-4)", show=False), Binding("shift+down,J", "move(0,4)", show=False),
        Binding("shift+left,H", "move(-4,0)", show=False), Binding("shift+right,L", "move(4,0)", show=False),
        Binding("space,enter", "act", "draw"), Binding("t", "pen", "pen", show=False),
        *[Binding(k, f"tool('{name}')", show=False) for k, name in TOOLS],
        Binding("F", "toggle('filled')", show=False), Binding("m", "toggle('mirror')", show=False),
        Binding("g", "toggle('grid')", show=False), Binding("O", "toggle('onion')", show=False),
        Binding("plus,equals_sign", "zoom(1)", "zoom", show=False), Binding("minus", "zoom(-1)", show=False),
        Binding("0", "fit", show=False),
        Binding("left_square_bracket,comma", "colour(-1)", show=False),
        Binding("right_square_bracket,full_stop", "colour(1)", show=False), Binding("x", "swap", show=False),
        Binding("number_sign", "ask_hex", show=False),
        Binding("left_curly_bracket,pagedown", "layer(-1)", show=False),
        Binding("right_curly_bracket,pageup", "layer(1)", show=False),
        Binding("A", "add_layer", show=False), Binding("V", "hide_layer", show=False),
        Binding("less_than_sign", "frame(-1)", show=False), Binding("greater_than_sign", "frame(1)", show=False),
        Binding("N", "add_frame", show=False), Binding("p", "play", "play"),
        Binding("P", "palette", "palette", show=False),
        Binding("u", "undo", "undo"), Binding("ctrl+r", "redo", show=False),
        Binding("ctrl+s", "save", "save"), Binding("colon", "command", ":cmd"),
        Binding("escape", "escape", show=False),
    ]

    # Keys by the character typed, whatever name the terminal gives them: on a Swedish layout
    # [ ] { } are AltGr+8/9/7/0, which kitty may not report as left_square_bracket etc.
    CHARS = {"[": ("colour", -1), "]": ("colour", 1), "{": ("layer", -1), "}": ("layer", 1),
             "<": ("frame", -1), ">": ("frame", 1), "#": ("ask_hex",), "+": ("zoom", 1)}

    def __init__(self, editor: "PixelView", **kw):
        super().__init__(**kw)
        self.ed = editor
        self._rows: dict[int, Strip] = {}
        self.blink = True

    async def _on_key(self, event: events.Key) -> None:
        action = self.CHARS.get(event.character or "")
        if action:
            event.stop()
            event.prevent_default()
            getattr(self, f"action_{action[0]}")(*action[1:])
            return
        await super()._on_key(event)

    def on_mount(self) -> None:
        self.set_interval(0.5, self._blink)
        self.redraw()

    def _blink(self) -> None:
        self.blink = not self.blink
        if self.has_focus and self.ed.zoom == 1:
            self.redraw(keep_scroll=True)

    # geometry: zoom 1 = 1 col × ½ row per pixel; zoom z ≥ 2 = 2(z-1) cols × (z-1) rows
    def cell(self) -> tuple[int, int]:
        z = self.ed.zoom
        return (1, 0) if z == 1 else (2 * (z - 1), z - 1)

    def redraw(self, keep_scroll: bool = False) -> None:
        doc, (pw, ph) = self.ed.doc, self.cell()
        self.virtual_size = Size(doc.w, (doc.h + 1) // 2) if ph == 0 else Size(doc.w * pw, doc.h * ph)
        self._rows.clear()
        self._grid = self.ed.view_grid()
        self._preview = self.ed.preview_points()
        if not keep_scroll:
            self.follow()
        self.refresh()

    def follow(self) -> None:
        pw, ph = self.cell()
        x, y = self.ed.cx, self.ed.cy
        region = Region(x, y // 2, 1, 1) if ph == 0 else Region(x * pw, y * ph, pw, ph)
        self.scroll_to_region(region, animate=False, immediate=True)

    def _pixel(self, x: int, y: int) -> tuple[str, bool]:
        """Colour shown at a pixel and whether it's under the cursor."""
        if (x, y) in self._preview:
            c = self._preview[(x, y)]
        else:
            c = self._grid[y][x]
        return (c if c is not None else _checker(x, y)), (x == self.ed.cx and y == self.ed.cy)

    def _row(self, vy: int) -> Strip:
        if vy in self._rows:
            return self._rows[vy]
        doc, (pw, ph) = self.ed.doc, self.cell()
        segs: list[Segment] = []
        focused = self.has_focus or self.ed.anchor is not None
        if ph == 0:                                               # half-blocks
            y0, y1 = vy * 2, vy * 2 + 1
            for x in range(doc.w):
                top, cur_t = self._pixel(x, y0)
                if y1 < doc.h:
                    bottom, cur_b = self._pixel(x, y1)
                else:
                    bottom, cur_b = P.bg, False
                if focused and self.blink:
                    top = _contrast(top) if cur_t else top
                    bottom = _contrast(bottom) if cur_b else bottom
                segs.append(Segment("▀", Style(color=top, bgcolor=bottom)))
        else:
            y, sub = divmod(vy, ph)
            for x in range(doc.w):
                c, cur = self._pixel(x, y)
                if cur and focused:
                    mid = sub == (ph - 1) // 2
                    text = ("[" + " " * (pw - 2) + "]") if mid else " " * pw
                    segs.append(Segment(text, Style(color=_contrast(c), bgcolor=c, bold=True)))
                elif self.ed.grid and pw >= 4:
                    segs.append(Segment("▏" + " " * (pw - 1), Style(color=_dim(c, "#000000", 0.35), bgcolor=c)))
                else:
                    segs.append(Segment(" " * pw, Style(bgcolor=c)))
        strip = Strip(Segment.simplify(segs))
        self._rows[vy] = strip
        return strip

    def render_line(self, y: int) -> Strip:
        sx, sy = self.scroll_offset
        vy = y + sy
        if vy >= self.virtual_size.height:
            return Strip.blank(self.size.width, Style(bgcolor=P.bg))
        return self._row(vy).crop(sx, sx + self.size.width).extend_cell_length(self.size.width,
                                                                               Style(bgcolor=P.bg))

    # mouse: left button uses the tool with fg, right with bg; drags draw for pencil/eraser
    def _to_pixel(self, event) -> tuple[int, int] | None:
        pw, ph = self.cell()
        sx, sy = self.scroll_offset
        x, y = event.x + sx, event.y + sy
        px_, py_ = (x, y * 2) if ph == 0 else (x // pw, y // ph)
        return (px_, py_) if self.ed.doc.inside(px_, py_) else None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        self.focus()
        spot = self._to_pixel(event)
        if spot:
            self.capture_mouse()
            self.ed.cx, self.ed.cy = spot
            self.ed.act(use_bg=event.button == 3, stroke_start=True)

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if event.button and self.ed.tool in ("pencil", "eraser"):
            spot = self._to_pixel(event)
            if spot and spot != (self.ed.cx, self.ed.cy):
                self.ed.stroke_to(*spot, use_bg=event.button == 3)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.release_mouse()

    # keys → the editor
    def action_move(self, dx: int, dy: int) -> None: self.ed.move(dx, dy)
    def action_act(self) -> None: self.ed.act()
    def action_pen(self) -> None: self.ed.toggle_pen()
    def action_tool(self, name: str) -> None: self.ed.set_tool(name)
    def action_toggle(self, what: str) -> None: self.ed.toggle(what)
    def action_zoom(self, step: int) -> None: self.ed.set_zoom(self.ed.zoom + step)
    def action_fit(self) -> None: self.ed.fit_zoom(say=True)
    def action_colour(self, step: int) -> None: self.ed.step_colour(step)
    def action_swap(self) -> None: self.ed.swap()
    def action_ask_hex(self) -> None: self.ed.open_command("hex #")
    def action_layer(self, step: int) -> None: self.ed.step_layer(step)
    def action_add_layer(self) -> None: self.ed.add_layer()
    def action_hide_layer(self) -> None: self.ed.hide_layer()
    def action_frame(self, step: int) -> None: self.ed.step_frame(step)
    def action_add_frame(self) -> None: self.ed.add_frame()
    def action_play(self) -> None: self.ed.toggle_play()
    def action_palette(self) -> None: self.ed.query_one(Palette).focus()
    def action_undo(self) -> None: self.ed.undo(True)
    def action_redo(self) -> None: self.ed.undo(False)
    def action_save(self) -> None: self.ed.run_command("w")
    def action_command(self) -> None: self.ed.open_command("")

    def action_escape(self) -> None:
        if not self.ed.cancel():
            self.app.action_nav_out()

    def on_focus(self) -> None:
        self.redraw(keep_scroll=True)

    def on_blur(self) -> None:
        self.redraw(keep_scroll=True)


class Palette(Static, can_focus=True):
    """The swatches: hjkl/arrows move, enter or space takes it as FG, b as BG, esc back out.
    A click takes a swatch too (right click: BG)."""
    BINDINGS = [Binding("left,h", "move(-1)", show=False), Binding("right,l", "move(1)", show=False),
                Binding("up,k", "move(-8)", show=False), Binding("down,j", "move(8)", show=False),
                Binding("enter,space", "take('fg')", "pick", show=False), Binding("b", "take('bg')", show=False),
                Binding("x", "take('clear')", show=False), Binding("escape", "back", show=False)]

    def __init__(self, editor: "PixelView", **kw):
        super().__init__(**kw)
        self.ed = editor
        self.index = 0

    def on_focus(self) -> None:
        pal = self.ed.doc.palette
        self.index = pal.index(self.ed.fg) if self.ed.fg in pal else 0
        self.ed._palette()

    def on_blur(self) -> None:
        self.ed._palette()

    def action_move(self, step: int) -> None:
        n = len(self.ed.doc.palette)
        self.index = max(0, min(n - 1, self.index + step))
        self.ed._palette()

    def action_take(self, which: str) -> None:
        c = self.ed.doc.palette[self.index] if which != "clear" else None
        if which == "bg" or which == "clear":
            self.ed.bg = c
        else:
            self.ed.fg = c
        self.ed.changed(pixels=False, say=f"{'BG' if which != 'fg' else 'FG'} {c or 'clear'}")
        self.action_back()

    def action_back(self) -> None:
        self.ed.canvas.focus()

    def on_click(self, event: events.Click) -> None:
        i = (event.y - 0) * SWATCHES_PER_ROW + event.x // 2
        if 0 <= event.x < SWATCHES_PER_ROW * 2 and 0 <= i < len(self.ed.doc.palette):
            self.index = i
            self.action_take("bg" if event.button == 3 else "fg")


class CommandLine(Input):
    BINDINGS = [Binding("escape", "close", show=False)]

    def action_close(self) -> None:
        self.display = False
        self.screen.query_one(Canvas).focus()


class Preview(Widget):
    """The frame at actual size (half-blocks), scaled down to fit if the sprite is wider."""

    def __init__(self, editor: "PixelView", **kw):
        super().__init__(**kw)
        self.ed = editor
        self.frame = 0

    def render_line(self, y: int) -> Strip:
        doc = self.ed.doc
        step = max(1, -(-doc.w // max(1, self.size.width)), -(-doc.h // max(2, self.size.height * 2)))
        grid = self.ed.composite_cached(self.frame)
        rows, cols = range(0, doc.h, step), range(0, doc.w, step)
        y0, y1 = y * 2, y * 2 + 1
        blank = Style(bgcolor=P.panel)
        if y0 >= len(rows):
            return Strip.blank(self.size.width, blank)
        segs = []
        for x in cols:
            top = grid[rows[y0]][x]
            bottom = grid[rows[y1]][x] if y1 < len(rows) else None
            if top is None and bottom is None:
                segs.append(Segment(" ", blank))
            elif top is None:
                segs.append(Segment("▄", Style(color=bottom, bgcolor=P.panel)))
            else:
                segs.append(Segment("▀", Style(color=top, bgcolor=bottom or P.panel)))
        return Strip(Segment.simplify(segs)).crop(0, self.size.width).extend_cell_length(self.size.width, blank)


class PixelView(Vertical):
    """The editor: holds the document and the editing state; the widgets draw it."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.doc = px.Doc.new(32, 32)
        self.hist = px.History()
        self.cx = self.cy = 0
        self.tool, self.prev_tool = "pencil", "pencil"
        self.fg: str | None = self.doc.palette[4]
        self.bg: str | None = None
        self.zoom, self.lay, self.frame = 3, 0, 0
        self.filled = self.mirror = self.onion = False
        self.grid = True
        self.anchor: tuple[int, int] | None = None
        self.pen = False
        self.playing = False
        self.dirty = False
        self.message = ""
        self._composites: dict[int, px.Grid] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="pixel-main"):
            with Vertical(id="pixel-left"):
                tools = Static(id="pixel-tools", classes="panel section")
                tools.border_title = "TOOLS"
                yield tools
                palette = Palette(self, id="pixel-palette", classes="panel section")
                palette.border_title = "PALETTE"
                yield palette
            canvas = Canvas(self, id="pixel-canvas", classes="panel section")
            canvas.border_title = "CANVAS"
            yield canvas
            with Vertical(id="pixel-right"):
                preview = Vertical(Preview(self, id="pixel-preview-img"), id="pixel-preview", classes="panel section")
                preview.border_title = "PREVIEW"
                yield preview
                layers = Static(id="pixel-layers", classes="panel section")
                layers.border_title = "LAYERS"
                yield layers
                frames = Static(id="pixel-frames", classes="panel section")
                frames.border_title = "FRAMES"
                yield frames
        yield Static(id="pixel-status")
        yield CommandLine(id="pixel-cmd", placeholder=": command   (try :new 28 28, :avatar, :export strip 4)")

    def on_mount(self) -> None:
        self.query_one("#pixel-cmd").display = False
        self.query_one("#pixel-palette").section_primary = None          # enter goes into the swatches
        for sid in ("#pixel-tools", "#pixel-layers", "#pixel-frames"):
            self.query_one(sid).section_primary = lambda: self.canvas.focus()
        self.query_one("#pixel-preview").section_primary = lambda: self.canvas.focus()
        self.canvas.section_primary = None
        try:
            last = Path(LAST_FILE.read_text(encoding="utf-8").strip())
            if last.exists():
                self._open(last)
        except OSError:
            pass
        self.refresh_all()

    @property
    def canvas(self) -> Canvas:
        return self.query_one(Canvas)

    def focus_canvas(self) -> None:
        self.canvas.focus()

    # --- what the canvas shows ------------------------------------------------------------------

    def composite_cached(self, frame: int) -> px.Grid:
        if frame not in self._composites:
            self._composites[frame] = self.doc.composite(min(frame, len(self.doc.frames) - 1))
        return self._composites[frame]

    def view_grid(self) -> px.Grid:
        grid = [row[:] for row in self.composite_cached(self.frame)]
        if self.onion and self.frame > 0:
            ghost = self.composite_cached(self.frame - 1)
            for y, row in enumerate(grid):
                for x, c in enumerate(row):
                    if c is None and ghost[y][x] is not None:
                        row[x] = _dim(ghost[y][x])
        return grid

    def preview_points(self) -> dict[tuple[int, int], str | None]:
        if self.anchor is None:
            return {}
        pts = self._shape(*self.anchor, self.cx, self.cy)
        out = {}
        for x, y in pts:
            for qx in ((x, self.doc.w - 1 - x) if self.mirror else (x,)):
                if self.doc.inside(qx, y):
                    out[(qx, y)] = self.fg
        return out

    def _shape(self, x0, y0, x1, y1):
        if self.tool == "line":
            return px.line(x0, y0, x1, y1)
        if self.tool == "rect":
            return px.rect(x0, y0, x1, y1, self.filled)
        return px.ellipse(x0, y0, x1, y1, self.filled)

    # --- editing --------------------------------------------------------------------------------

    def changed(self, *, pixels: bool = True, say: str = "") -> None:
        if pixels:
            self.dirty = True
            self._composites.clear()
        if say:
            self.message = say
        self.refresh_all()

    def refresh_all(self) -> None:
        if not self.is_mounted:
            return
        self.cx, self.cy = min(self.cx, self.doc.w - 1), min(self.cy, self.doc.h - 1)
        self.lay = min(self.lay, len(self.doc.layers) - 1)
        self.frame = min(self.frame, len(self.doc.frames) - 1)
        self.canvas.redraw()
        self._panels()

    def move(self, dx: int, dy: int) -> None:
        nx = max(0, min(self.doc.w - 1, self.cx + dx))
        ny = max(0, min(self.doc.h - 1, self.cy + dy))
        if self.pen and self.tool in ("pencil", "eraser"):
            self.stroke_to(nx, ny)
            return
        self.cx, self.cy = nx, ny
        self.message = ""
        self.canvas.redraw()
        self._status()

    def stroke_to(self, x: int, y: int, use_bg: bool = False) -> None:
        color = None if self.tool == "eraser" else (self.bg if use_bg else self.fg)
        self.doc.put(self.frame, self.lay, px.line(self.cx, self.cy, x, y), color, self.mirror)
        self.cx, self.cy = x, y
        self.changed()

    def act(self, use_bg: bool = False, stroke_start: bool = False) -> None:
        color = self.bg if use_bg else self.fg
        x, y = self.cx, self.cy
        if self.tool == "pick":
            picked = self.composite_cached(self.frame)[y][x]
            if use_bg:
                self.bg = picked
            else:
                self.fg = picked
            self.tool = self.prev_tool
            self.changed(pixels=False, say=f"picked {picked or 'transparent'}")
            return
        if self.tool in SHAPES:
            if self.anchor is None:
                self.anchor = (x, y)
                self.changed(pixels=False, say=f"{self.tool} from {x},{y}: move, then space · esc cancels")
                return
            self.hist.push(self.doc)
            self.doc.put(self.frame, self.lay, self._shape(*self.anchor, x, y), color, self.mirror)
            self.anchor = None
            self.changed(say="")
            return
        self.hist.push(self.doc)
        if self.tool == "fill":
            area = px.flood(self.doc.frames[self.frame].cels[self.lay], x, y)
            self.doc.put(self.frame, self.lay, area, color, self.mirror)
        else:
            self.doc.put(self.frame, self.lay, [(x, y)], None if self.tool == "eraser" else color, self.mirror)
        self.changed()

    def cancel(self) -> bool:
        if self.anchor is not None or self.pen or self.playing:
            self.anchor, self.pen = None, False
            if self.playing:
                self.toggle_play()
            self.changed(pixels=False, say="")
            return True
        return False

    def toggle_pen(self) -> None:
        if self.tool not in ("pencil", "eraser"):
            self.set_tool("pencil")
        self.pen = not self.pen
        if self.pen:
            self.hist.push(self.doc)
            self.doc.put(self.frame, self.lay, [(self.cx, self.cy)],
                         None if self.tool == "eraser" else self.fg, self.mirror)
            self.changed(say="pen down: moving draws · t or esc lifts it")
        else:
            self.changed(pixels=False, say="pen up")

    def set_tool(self, name: str) -> None:
        if name == "pick" and self.tool != "pick":
            self.prev_tool = self.tool
        self.tool, self.anchor = name, None
        if name not in ("pencil", "eraser"):
            self.pen = False
        self.changed(pixels=False, say="")

    def toggle(self, what: str) -> None:
        setattr(self, what, not getattr(self, what))
        self.changed(pixels=False, say=f"{what} {'on' if getattr(self, what) else 'off'}")

    def fit_zoom(self, say: bool = False) -> None:
        """The biggest zoom at which the whole sprite fits the canvas (1 = half-blocks if nothing does)."""
        cw, ch = self.canvas.size
        if not cw:
            self.call_after_refresh(self.fit_zoom, say=say)
            return
        self.zoom = next((z for z in range(8, 1, -1) if self.doc.w * 2 * (z - 1) <= cw and self.doc.h * (z - 1) <= ch), 1)
        self.changed(pixels=False, say=f"zoom {self.zoom} (fits)" if say else self.message)

    def set_zoom(self, z: int) -> None:
        self.zoom = max(1, min(8, z))
        self.changed(pixels=False, say=f"zoom {self.zoom}")

    def step_colour(self, step: int) -> None:
        pal = self.doc.palette
        i = pal.index(self.fg) if self.fg in pal else -1
        self.fg = pal[(i + step) % len(pal)]
        self.changed(pixels=False)

    def swap(self) -> None:
        self.fg, self.bg = self.bg, self.fg
        self.changed(pixels=False)

    def step_layer(self, step: int) -> None:
        self.lay = max(0, min(len(self.doc.layers) - 1, self.lay + step))
        self.changed(pixels=False, say=f"layer {self.doc.layers[self.lay].name}")

    def add_layer(self) -> None:
        self.hist.push(self.doc)
        self.lay = self.doc.add_layer(self.lay)
        self.changed(say=f"added {self.doc.layers[self.lay].name}")

    def hide_layer(self) -> None:
        layer = self.doc.layers[self.lay]
        layer.visible = not layer.visible
        self.changed(say=f"{layer.name} {'shown' if layer.visible else 'hidden'}")

    def step_frame(self, step: int) -> None:
        self.frame = (self.frame + step) % len(self.doc.frames)
        self.changed(pixels=False)

    def add_frame(self) -> None:
        self.hist.push(self.doc)
        self.frame = self.doc.add_frame(self.frame)
        self.changed(say=f"frame {self.frame + 1} (a copy)")

    def undo(self, back: bool) -> None:
        if self.hist.step(self.doc, back):
            self.changed(say="undone" if back else "redone")
        else:
            self.changed(pixels=False, say="nothing to " + ("undo" if back else "redo"))

    # --- the preview's animation ------------------------------------------------------------------

    def toggle_play(self) -> None:
        self.playing = not self.playing
        if self.playing:
            self._play_frames = self._loop_frames()
            self._play_i = 0
            self._play_step()
        self._panels()

    def _loop_frames(self) -> list[int]:
        tag = self.doc.tag_at(self.frame)
        if tag:
            frames = list(range(tag.start, tag.end + 1))
            if tag.extra.get("direction") == "pingpong" and len(frames) > 2:
                frames += frames[-2:0:-1]
            return frames
        return list(range(len(self.doc.frames)))

    def _play_step(self) -> None:
        preview = self.query_one(Preview)
        if not self.playing:
            preview.frame = self.frame
            preview.refresh()
            return
        f = self._play_frames[self._play_i % len(self._play_frames)]
        preview.frame = f
        preview.refresh()
        self._play_i += 1
        self.set_timer(max(0.02, self.doc.frames[f].ms / 1000), self._play_step)

    # --- panels -------------------------------------------------------------------------------------

    def _panels(self) -> None:
        self._tools()
        self._palette()
        self._layers()
        self._frames()
        self._status()
        preview = self.query_one(Preview)
        if not self.playing:
            preview.frame = self.frame
        preview.refresh()
        name = self.doc.path.name if self.doc.path else "untitled"
        self.canvas.border_title = f"CANVAS · {name}{' ●' if self.dirty else ''}"
        self.canvas.border_subtitle = f"{self.doc.w}×{self.doc.h} · zoom {self.zoom}"

    def _swatch(self, c: str | None) -> Text:
        return Text("██", style=c) if c else Text("░░", style=P.amber_dim)

    def _tools(self) -> None:
        t = Text()
        for key, name in TOOLS:
            on = name == self.tool
            t.append(("▸ " if on else "  ") + key + " ", style=f"bold {P.amber_hi}" if on else P.amber_dim)
            t.append(name + "\n", style=f"bold {P.amber_hi}" if on else P.amber)
        t.append("\nFG ", style=P.amber_dim)
        t.append_text(self._swatch(self.fg))
        t.append(f" {self.fg or 'clear'}\n", style=P.amber)
        t.append("BG ", style=P.amber_dim)
        t.append_text(self._swatch(self.bg))
        t.append(f" {self.bg or 'clear'}\n\n", style=P.amber)
        for key, what in (("F", "filled"), ("m", "mirror"), ("g", "grid"), ("O", "onion")):
            on = getattr(self, what)
            t.append(f"{key} {what:<7}", style=P.amber_dim)
            t.append(("on" if on else "off") + "\n", style=f"bold {P.teal}" if on else P.amber_dim)
        t.append("t pen     ", style=P.amber_dim)
        t.append(("DOWN" if self.pen else "up") + "\n", style=f"bold {P.red}" if self.pen else P.amber_dim)
        self.query_one("#pixel-tools", Static).update(t)

    def _palette(self) -> None:
        t = Text()
        pal_w = self.query_one(Palette)
        for i, c in enumerate(self.doc.palette):
            if pal_w.has_focus and i == pal_w.index:
                t.append("▐▌", style=Style(color=_contrast(c), bgcolor=c, bold=True, blink=False))
            elif c == self.fg:
                t.append("[]", style=Style(color=_contrast(c), bgcolor=c, bold=True))
            elif c == self.bg:
                t.append("<>", style=Style(color=_contrast(c), bgcolor=c))
            else:
                t.append("██", style=c)
            if i % SWATCHES_PER_ROW == SWATCHES_PER_ROW - 1:
                t.append("\n")
        if pal_w.has_focus:
            t.append("\nenter FG · b BG · x clear BG · esc", style=P.sand)
        else:
            t.append("\nP pick · , . or [ ] step\nx swap · # hex", style=P.amber_dim)
        self.query_one("#pixel-palette", Static).update(t)

    def _layers(self) -> None:
        t = Text()
        for i in reversed(range(len(self.doc.layers))):
            layer, on = self.doc.layers[i], i == self.lay
            t.append("● " if layer.visible else "○ ", style=P.teal if layer.visible else P.amber_dim)
            t.append(f"{layer.name}\n", style=f"bold reverse {P.amber}" if on else P.amber)
        t.append("{ } pick · A add · V hide", style=P.amber_dim)
        self.query_one("#pixel-layers", Static).update(t)

    def _frames(self) -> None:
        t = Text()
        for i, f in enumerate(self.doc.frames):
            on = i == self.frame
            t.append(f"{i + 1:02d}", style=f"bold reverse {P.amber}" if on else P.amber)
            t.append(" ")
            if i % 8 == 7:
                t.append("\n")
        t.append(f"\n{self.doc.frames[self.frame].ms} ms", style=P.amber)
        tag = self.doc.tag_at(self.frame)
        if tag:
            t.append(f" · {tag.name} {tag.start + 1}–{tag.end + 1}", style=P.teal)
        if self.playing:
            t.append(" · ▶", style=f"bold {P.teal}")
        t.append("\n< > pick · N new · p play", style=P.amber_dim)
        self.query_one("#pixel-frames", Static).update(t)

    def _status(self) -> None:
        c = self.composite_cached(self.frame)[self.cy][self.cx]
        t = Text()
        t.append(f" x {self.cx:>3} y {self.cy:>3} ", style=f"bold {P.amber_hi}")
        t.append("│ ", style=P.amber_dim)
        t.append_text(self._swatch(c))
        t.append(f" {c or 'clear':<8}", style=P.amber)
        t.append(f"│ {self.tool} ", style=P.amber)
        t.append(f"│ layer {self.lay + 1}/{len(self.doc.layers)} │ frame {self.frame + 1}/{len(self.doc.frames)} ",
                 style=P.amber_dim)
        if self.message:
            t.append(f"│ {self.message}", style=f"bold {P.sand}")
        self.query_one("#pixel-status", Static).update(t)

    # --- : commands -------------------------------------------------------------------------------

    def open_command(self, prefill: str) -> None:
        cmd = self.query_one("#pixel-cmd", Input)
        cmd.display = True
        cmd.value = prefill
        cmd.cursor_position = len(prefill)
        cmd.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "pixel-cmd":
            return
        event.stop()
        event.input.display = False
        self.canvas.focus()
        self.run_command(event.value.strip().lstrip(":"))

    def _dir(self) -> Path:
        return Path(getattr(self.app.cfg, "pixel_dir", "~/Pictures/MABEL-OS")).expanduser()

    def _path(self, name: str) -> Path:
        p = Path(name).expanduser()
        return p if p.is_absolute() else self._dir() / p

    def _open(self, path: Path) -> None:
        self.doc = px.open_any(path)
        self.hist = px.History()
        self.frame = self.lay = 0
        self.anchor, self.pen, self.dirty = None, False, False
        self.fit_zoom()
        if self.fg not in self.doc.palette and self.doc.palette:
            self.fg = self.doc.palette[0]
        self._remember()

    def _remember(self) -> None:
        try:
            if self.doc.path:
                LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
                LAST_FILE.write_text(str(self.doc.path), encoding="utf-8")
        except OSError:
            pass

    def run_command(self, line: str) -> None:
        try:
            self.message = self._command(line.split()) or ""
        except Exception as exc:
            self.message = f"✖ {exc}"
        self._composites.clear()
        self.refresh_all()

    def _command(self, args: list[str]) -> str | None:
        if not args:
            return None
        cmd, rest = args[0], args[1:]
        force = cmd.endswith("!")
        cmd = cmd.rstrip("!")
        if cmd in ("new", "e", "avatar") and self.dirty and not force:
            return f"unsaved changes: :w first, or :{cmd}! to drop them"
        if cmd == "new":
            w, h = (int(rest[0]), int(rest[1])) if len(rest) >= 2 else (32, 32)
            self.doc = px.Doc.new(w, h, rest[2] if len(rest) > 2 else "db32")
            self.hist, self.frame, self.lay, self.dirty = px.History(), 0, 0, False
            self.fg = self.doc.palette[min(4, len(self.doc.palette) - 1)]
            self.cx = self.cy = 0
            self.fit_zoom()
            return f"new {self.doc.w}×{self.doc.h}"
        if cmd == "w":
            target = self._path(rest[0]) if rest else None
            if target is None and self.doc.path is None:
                return "name it: :w sprite.mpx (or .png)"
            path = px.save(self.doc, target)
            self.dirty = False
            self._remember()
            return f"saved {path}"
        if cmd == "e":
            if not rest:
                return "which file? :e name.mpx | name.png | a folder with avatar.json"
            self._open(self._path(" ".join(rest)))
            return f"opened {self.doc.path}"
        if cmd == "avatar":
            from .avatar import DEFAULT
            configured = Path(self.app.cfg.avatar).expanduser() if self.app.cfg.avatar else None
            self._open(configured if configured and configured.is_dir() else DEFAULT)
            return f"Mabel's avatar ({', '.join(t.name for t in self.doc.tags)}) · :w writes the strips back"
        if cmd == "export":
            how = rest[0] if rest and rest[0] in ("png", "strip", "sheet") else "png"
            nums = [int(a) for a in rest[1:] if a.isdigit()]
            names = [a for a in rest[1:] if not a.isdigit()]
            scale, columns = (nums + [1])[0], (nums[1:] + [4])[0]
            stem = (self.doc.path.stem if self.doc.path and self.doc.path.is_file() else
                    (self.doc.path.name if self.doc.path else "sprite"))
            path = self._path(names[0]) if names else self._dir() / f"{stem}-{how}{'' if scale == 1 else f'@{scale}x'}.png"
            px.export(self.doc, path, how=how, frame=self.frame, scale=scale, columns=columns)
            return f"exported {path}"
        if cmd == "palette":
            if not rest or rest[0] not in px.PALETTES:
                return "palettes: " + " ".join(px.PALETTES)
            self.hist.push(self.doc)
            self.doc.palette = list(px.PALETTES[rest[0]])
            self.doc.adopt_colors()
            self.dirty = True
            return f"palette {rest[0]}"
        if cmd == "hex":
            c = px._color(rest[0] if rest else "")
            if not c:
                return "a colour like :hex #ff8800"
            if c not in self.doc.palette and len(self.doc.palette) < px.MAX_PALETTE:
                self.doc.palette.append(c)
            self.fg = c
            return f"colour {c}"
        if cmd in ("flip", "rotate", "resize"):
            self.hist.push(self.doc)
            if cmd == "flip":
                self.doc.flip("v" if rest[:1] == ["v"] else "h")
            elif cmd == "rotate":
                self.doc.rotate(clockwise=rest[:1] != ["ccw"])
            else:
                self.doc.resize(int(rest[0]), int(rest[1]))
            self.dirty = True
            return f"{cmd} {' '.join(rest)}".strip()
        if cmd == "ms":
            if rest[:1] == ["all"]:
                for f in self.doc.frames:
                    f.ms = int(rest[1])
            else:
                self.doc.frames[self.frame].ms = int(rest[0])
            self.dirty = True
            return None
        if cmd == "tag":
            name, a, b = rest[0], int(rest[1]) - 1, int(rest[2]) - 1
            self.doc.tags = [t for t in self.doc.tags if t.name != name] + [px.Tag(name, min(a, b), max(a, b))]
            self.dirty = True
            return f"tag {name} {min(a, b) + 1}–{max(a, b) + 1}"
        if cmd == "untag":
            self.doc.tags = [t for t in self.doc.tags if t.name != (rest[0] if rest else "")]
            self.dirty = True
            return None
        if cmd in ("layer", "frame"):
            what = rest[0] if rest else ""
            self.hist.push(self.doc)
            self.dirty = True
            if cmd == "layer":
                if what == "name":
                    self.doc.layers[self.lay].name = " ".join(rest[1:]) or self.doc.layers[self.lay].name
                elif what == "del":
                    self.lay = self.doc.remove_layer(self.lay)
                elif what in ("up", "down"):
                    self.lay = self.doc.move_layer(self.lay, 1 if what == "up" else -1)
                return None
            if what == "del":
                self.frame = self.doc.remove_frame(self.frame)
            elif what in ("left", "right"):
                self.frame = self.doc.move_frame(self.frame, 1 if what == "right" else -1)
            return None
        if cmd in ("help", "h"):
            self.app.notify(COMMANDS, title="PIXEL COMMANDS", timeout=20)
            return None
        return f"unknown command :{cmd} (try :help)"
