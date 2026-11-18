"""Mabel's pixel avatar, drawn exactly: one terminal cell = 1×2 pixels (▀, top pixel in the
foreground colour, bottom pixel in the background), so a 28×28 sprite is 28×14 cells.

States (idle, talk, later thinking, sleeping…) are horizontal PNG strips of square frames, listed in
assets/avatar/avatar.json with per-frame timings in ms (as set in Aseprite). `avatar` in config.toml
may point at another such folder, or at a single PNG (a still, or one strip used for every state).
A state that has no strip falls back to idle. Transparent pixels show the panel.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip
from textual.widget import Widget

DEFAULT = Path(__file__).parent / "assets" / "avatar"

Pixels = list[list[tuple | None]]


def load_frames(path: Path) -> list[Pixels]:
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


@dataclass
class Animation:
    frames: list[Pixels]
    ms: list[int] = field(default_factory=list)
    hold: tuple[int, int] | None = None        # first frame held this long (random range) before each loop

    def duration(self, i: int) -> float:
        base = (self.ms[i] if i < len(self.ms) else (self.ms[-1] if self.ms else 250)) / 1000
        if i == 0 and self.hold:
            return max(base, random.uniform(*self.hold) / 1000)
        return base


def load_animations(path: Path | str | None, fps: float = 4.0) -> dict[str, Animation]:
    path = Path(path).expanduser() if path else DEFAULT
    if path.is_file():
        frames = load_frames(path)
        return {"idle": Animation(frames, [int(1000 / fps)] if fps > 0 else [])}
    manifest = json.loads((path / "avatar.json").read_text(encoding="utf-8"))
    states = {}
    for name, spec in manifest.items():
        if isinstance(spec, dict) and spec.get("file"):
            hold = spec.get("hold")
            states[name] = Animation(load_frames(path / spec["file"]), [int(m) for m in spec.get("ms") or []],
                                     tuple(hold) if hold else None)
    if "idle" not in states:
        still = path / "still.png"
        states["idle"] = Animation(load_frames(still) if still.exists() else next(iter(states.values())).frames)
    return states


def _hex(rgb: tuple | None) -> str | None:
    return None if rgb is None else "#%02x%02x%02x" % rgb


class Avatar(Widget):
    DEFAULT_CSS = "Avatar { width: auto; height: auto; }"

    def __init__(self, path: Path | str | None = None, fps: float = 4.0, **kw):
        super().__init__(**kw)
        self.animations = load_animations(path, fps)
        self.state = "idle"
        self.frame = 0
        self._timer = None
        self._strips: dict[tuple[str, int], list[Strip]] = {}   # built once per frame: the Pi can't afford it per paint

    @property
    def animation(self) -> Animation:
        return self.animations.get(self.state) or self.animations["idle"]

    def on_mount(self) -> None:
        self._schedule()
        self.set_timer(3, self._warm)                # build every frame once startup has settled

    def _warm(self) -> None:
        for state, anim in self.animations.items():
            for i, frame in enumerate(anim.frames):
                self._strips.setdefault((state, i), _frame_strips(frame))

    def set_state(self, state: str) -> None:
        """Switch animation (idle, talk, …); a state without its own strip shows idle."""
        if state == self.state:
            return
        self.state, self.frame = state, 0
        self.refresh()
        self._schedule()

    def _schedule(self) -> None:
        if self._timer:
            self._timer.stop()
            self._timer = None
        anim = self.animation
        if len(anim.frames) > 1:
            self._timer = self.set_timer(anim.duration(self.frame), self._tick)

    def _tick(self) -> None:
        self.frame = (self.frame + 1) % len(self.animation.frames)
        self.refresh()
        self._schedule()

    def get_content_width(self, container, viewport) -> int:
        return len(self.animation.frames[0][0])

    def get_content_height(self, container, viewport, width) -> int:
        return (len(self.animation.frames[0]) + 1) // 2

    def render_line(self, y: int) -> Strip:
        frames = self.animation.frames
        key = (self.state if self.state in self.animations else "idle", self.frame % len(frames))
        strips = self._strips.get(key)
        if strips is None:
            strips = self._strips[key] = _frame_strips(frames[key[1]])
        return strips[y] if y < len(strips) else Strip.blank(self.size.width)


def _frame_strips(pixels: Pixels) -> list[Strip]:
    """One frame as cell rows: ▀ with the top pixel as colour and the bottom as background."""
    styles: dict[tuple, Style] = {}

    def style(fg, bg) -> Style:
        if (fg, bg) not in styles:
            styles[(fg, bg)] = Style(color=_hex(fg), bgcolor=_hex(bg))
        return styles[(fg, bg)]

    rows = []
    for y in range(0, len(pixels), 2):
        top = pixels[y]
        bottom = pixels[y + 1] if y + 1 < len(pixels) else [None] * len(top)
        segments = []
        for up, down in zip(top, bottom):
            if up is None and down is None:
                segments.append(Segment(" "))
            elif up is None:
                segments.append(Segment("▄", style(down, None)))
            else:
                segments.append(Segment("▀", style(up, down)))
        rows.append(Strip(Segment.simplify(segments), len(segments)))
    return rows
