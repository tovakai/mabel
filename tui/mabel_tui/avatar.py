"""Mabel's pixel avatar, drawn exactly: one terminal cell = 1×2 pixels (▀, top pixel in the
foreground colour, bottom pixel in the background), so the 28×28 sprite is 28×14 cells.

Animation-ready: a horizontal strip of square frames (an Aseprite sprite-sheet export, "by rows"
with one row) plays at `fps`; a single 28×28 image is a still. Transparent pixels show the panel.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget


DEFAULT = Path(__file__).parent / "assets" / "mabel.png"


def load_frames(path: Path) -> list[list[list[tuple | None]]]:
    """Frames of rows of pixels ((r, g, b) or None for transparent)."""
    im = Image.open(path).convert("RGBA")
    side = im.height
    count = max(1, im.width // side) if im.width % side == 0 else 1
    width = side if count > 1 else im.width
    frames = []
    for f in range(count):
        frame = im.crop((f * width, 0, (f + 1) * width, im.height))
        frames.append([[(p[0], p[1], p[2]) if p[3] >= 128 else None for p in
                        (frame.getpixel((x, y)) for x in range(frame.width))] for y in range(frame.height)])
    return frames


def _hex(rgb: tuple | None) -> str | None:
    return None if rgb is None else "#%02x%02x%02x" % rgb


class Avatar(Widget):
    DEFAULT_CSS = "Avatar { width: auto; height: auto; }"

    def __init__(self, path: Path | str | None = None, fps: float = 4.0, **kw):
        super().__init__(**kw)
        self.frames = load_frames(Path(path).expanduser() if path else DEFAULT)
        self.frame = 0
        self.fps = fps

    def on_mount(self) -> None:
        if len(self.frames) > 1 and self.fps > 0:
            self.set_interval(1 / self.fps, self._tick)

    def _tick(self) -> None:
        self.frame = (self.frame + 1) % len(self.frames)
        self.refresh()

    def get_content_width(self, container, viewport) -> int:
        return len(self.frames[0][0])

    def get_content_height(self, container, viewport, width) -> int:
        return (len(self.frames[0]) + 1) // 2

    def render_line(self, y: int) -> Strip:
        pixels = self.frames[self.frame]
        top = pixels[2 * y] if 2 * y < len(pixels) else None
        if top is None:
            return Strip.blank(self.size.width)
        bottom = pixels[2 * y + 1] if 2 * y + 1 < len(pixels) else [None] * len(top)
        segments = []
        for up, down in zip(top, bottom):
            if up is None and down is None:
                segments.append(Segment(" "))
            elif up is None:
                segments.append(Segment("▄", Style(color=_hex(down))))
            else:
                segments.append(Segment("▀", Style(color=_hex(up), bgcolor=_hex(down))))
        return Strip(segments, len(segments))
