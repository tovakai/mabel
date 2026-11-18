"""Colour tuner (ctrl+t): adjust the palette live and save it to this machine's config.

Made for the deck's 7" panel, which shows colours its own way and has no settings of its own.
j/k pick a colour · h/l hue · -/+ lightness · [ ] saturation · g the whole amber family together ·
e type a hex · s save to config.toml [colors] · r back to the preset · esc close.
"""
from __future__ import annotations

import colorsys
import re

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from . import palette
from .config import CONFIG
from .palette import P

KEYS = ["amber", "amber_hi", "amber_dim", "amber_faint", "beige", "beige_dark", "casing_text",
        "teal", "orange", "red", "sand", "mint", "bg", "panel"]
FAMILY = ("amber", "amber_hi", "amber_dim", "amber_faint", "orange", "sand")
USE = {"amber": "most text", "amber_hi": "titles, highlights", "amber_dim": "secondary text, borders",
       "amber_faint": "selection, faint lines", "beige": "labels, the casing strip", "beige_dark": "codes, hints",
       "casing_text": "text on the casing", "teal": "HUD accents", "orange": "warnings, loose ends",
       "red": "errors, things out", "sand": "map bins", "mint": "map bins", "bg": "background",
       "panel": "panel background"}


def to_hls(hex_: str) -> tuple[float, float, float]:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)


def to_hex(h: float, l: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def nudge(hex_: str, dh: float = 0, dl: float = 0, ds: float = 0) -> str:
    h, l, s = to_hls(hex_)
    return to_hex(h + dh, l + dl, s + ds)


def save_colours(colours: dict[str, str], path=CONFIG) -> None:
    """Write the colours that differ from the chosen preset as [colors] in config.toml."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?ms)^\[colors\]\n.*?(?=^\[|\Z)", "", text).rstrip("\n")
    m = re.search(r'^palette\s*=\s*"([^"]+)"', text, re.M)
    base = {**palette.DEFAULT, **palette.PRESETS.get(m.group(1) if m else "default", {})}
    changed = {k: v for k, v in colours.items() if base.get(k) != v}
    if changed:
        text += "\n\n# tuned with ctrl+t\n[colors]\n" + "".join(f'{k} = "{v}"\n' for k, v in changed.items())
    path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")


class Tuner(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("j,down", "move(1)", show=False), Binding("k,up", "move(-1)", show=False),
        Binding("h,left", "adjust(-0.01, 0, 0)", show=False), Binding("l,right", "adjust(0.01, 0, 0)", show=False),
        Binding("minus", "adjust(0, -0.03, 0)", show=False), Binding("plus,equals_sign", "adjust(0, 0.03, 0)", show=False),
        Binding("left_square_bracket", "adjust(0, 0, -0.05)", show=False),
        Binding("right_square_bracket", "adjust(0, 0, 0.05)", show=False),
        Binding("g", "group", show=False), Binding("s", "save", show=False), Binding("r", "reset", show=False),
        Binding("e", "edit_hex", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.row, self.group, self.dirty = 0, False, False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="tuner", classes="panel") as box:
            box.border_title = "COLOUR TUNER"
            box.border_subtitle = "s save · r reset · esc close"
            yield Static(id="tuner-body")

    def on_mount(self) -> None:
        self.paint()

    def paint(self) -> None:
        t = Text()
        t.append("j k pick · h l hue · - + lighter/darker · [ ] saturation · g whole amber family · e hex\n\n",
                 style=P.beige_dark)
        for i, key in enumerate(KEYS):
            value = getattr(P, key)
            marked = i == self.row or (self.group and key in FAMILY and KEYS[self.row] in FAMILY)
            t.append("▶ " if i == self.row else ("· " if marked else "  "), style=P.amber_hi)
            t.append("      ", style=f"on {value}")
            t.append(f"  {key:<12} {value}  ", style=f"bold {P.amber_hi}" if marked else P.amber)
            t.append("Sample text", style=f"{value} on {P.bg}")
            t.append(f"  {USE[key]}\n", style=P.beige_dark)
        t.append(f"\n{'GROUP: the amber family moves together' if self.group else 'one colour at a time'}",
                 style=P.teal)
        t.append("   ·   unsaved changes" if self.dirty else "", style=P.orange)
        self.query_one("#tuner-body", Static).update(t)

    def on_click(self, event) -> None:
        y = event.screen_y - self.query_one("#tuner-body").region.y - 2
        if 0 <= y < len(KEYS):
            self.row = y
            self.paint()

    def action_move(self, step: int) -> None:
        self.row = (self.row + step) % len(KEYS)
        self.paint()

    def action_group(self) -> None:
        self.group = not self.group
        self.paint()

    def action_adjust(self, dh: float, dl: float, ds: float) -> None:
        key = KEYS[self.row]
        targets = FAMILY if self.group and key in FAMILY else (key,)
        for k in targets:
            setattr(P, k, nudge(getattr(P, k), dh, dl, ds))
        self.dirty = True
        self.app.apply_palette()
        self.paint()

    def action_reset(self) -> None:
        for k, v in palette.load().items():
            setattr(P, k, v)
        self.dirty = False
        self.app.apply_palette()
        self.paint()

    def action_save(self) -> None:
        save_colours(vars(P))
        self.dirty = False
        self.app.notify(f"✓ colours saved to {CONFIG}", timeout=3)
        self.paint()

    def action_edit_hex(self) -> None:
        from .inventory import Ask
        key = KEYS[self.row]

        def done(result):
            value = (result or {}).get("hex", "").strip()
            if re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
                setattr(P, key, "#" + value.lstrip("#").lower())
                self.dirty = True
                self.app.apply_palette()
                self.paint()
        self.app.push_screen(Ask(f"{key} as hex", [("hex", "e.g. #ffa640", getattr(P, key), "line")], ok="Set"), done)
