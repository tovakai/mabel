"""CH-07 CREATIVE: MABEL-OS Creative Suite. A bar of apps (PIXEL, TRACKER) over the one on screen.

F7 / alt+7 opens the channel; pressed again while it's on screen it moves to the next app.
The bar is clickable too.
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import ContentSwitcher, Static

from .palette import P
from .pixelview import PixelView

APPS = [("pixel", "PIXEL", "sprites and animation"), ("tracker", "TRACKER", "chip music · coming next")]


class AppBar(Static):
    def on_click(self, event: events.Click) -> None:
        x = 0
        for key, label, _ in APPS:
            width = len(label) + 6
            if x <= event.x < x + width:
                self.parent.show(key)
                return
            x += width


class TrackerSoon(Static):
    def on_mount(self) -> None:
        t = Text()
        t.append("\n  TRACKER\n\n", style=f"bold {P.amber_hi}")
        t.append("  An LSDJ-style tracker without the menu-diving: song, chain, phrase and\n"
                 "  instrument on screen together, a chip synth and MIDI out.\n  Next round.", style=P.amber)
        self.update(t)


class Creative(Vertical):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.current = "pixel"

    def compose(self) -> ComposeResult:
        yield AppBar(id="creative-bar")
        with ContentSwitcher(initial="pixel", id="creative-apps"):
            yield PixelView(id="pixel")
            yield TrackerSoon(id="tracker", classes="panel")

    def on_mount(self) -> None:
        self._bar()

    def _bar(self) -> None:
        t = Text()
        for key, label, hint in APPS:
            on = key == self.current
            t.append(f" ◆ {label} " if on else f"   {label} ",
                     style=f"bold {P.bg} on {P.amber}" if on else f"bold {P.amber_dim}")
            t.append("  ")
        hint = next(h for k, _, h in APPS if k == self.current)
        t.append(f"  {hint}   F7: next app", style=P.amber_dim)
        self.query_one("#creative-bar", Static).update(t)

    def show(self, key: str) -> None:
        self.current = key
        self.query_one("#creative-apps", ContentSwitcher).current = key
        self._bar()
        self.focus_primary()

    def cycle(self) -> None:
        keys = [k for k, _, _ in APPS]
        self.show(keys[(keys.index(self.current) + 1) % len(keys)])

    def focus_primary(self) -> None:
        if self.current == "pixel":
            self.query_one(PixelView).focus_canvas()
        else:
            self.app.start_nav()
