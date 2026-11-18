"""A channel that holds several apps (CREATIVE: PIXEL, TRACKER · SYS: STATUS, NETWORK…):
a bar of apps over the one on screen. The channel's F-key pressed again moves to the next app;
the bar is clickable too. Subclasses list APPS, build them in make(), and say what to focus.
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Static

from .palette import P


class SuiteBar(Static):
    def on_click(self, event: events.Click) -> None:
        x = 0
        suite = self.parent
        for key, label, _ in suite.apps():
            width = len(label) + 6
            if x <= event.x < x + width:
                suite.show(key)
                return
            x += width


class Suite(Vertical):
    APPS: list[tuple[str, str, str]] = []        # (key, LABEL, hint)
    FKEY = ""                                     # shown in the bar: "F7: next app"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.current = self.apps()[0][0]

    def apps(self) -> list[tuple[str, str, str]]:
        return self.APPS

    def make(self) -> list[Widget]:
        raise NotImplementedError

    def focus_app(self, key: str) -> None:
        self.app.start_nav()

    def shown(self, key: str) -> None:
        """Hook: an app came on screen."""

    def compose(self) -> ComposeResult:
        yield SuiteBar(classes="suite-bar")
        with ContentSwitcher(initial=self.current, classes="suite-apps"):
            yield from self.make()

    def on_mount(self) -> None:
        self.query_one(".suite-bar", Static).display = len(self.apps()) > 1
        self._bar()

    def _bar(self) -> None:
        t = Text()
        for key, label, _ in self.apps():
            on = key == self.current
            t.append(f" ◆ {label} " if on else f"   {label} ",
                     style=f"bold {P.bg} on {P.amber}" if on else f"bold {P.amber_dim}")
            t.append("  ")
        hint = next(h for k, _, h in self.apps() if k == self.current)
        t.append(f"  {hint}" + (f"   {self.FKEY}: next app" if self.FKEY else ""), style=P.amber_dim)
        self.query_one(".suite-bar", Static).update(t)

    def show(self, key: str) -> None:
        self.current = key
        self.query_one(".suite-apps", ContentSwitcher).current = key
        self._bar()
        self.shown(key)
        self.focus_primary()

    def cycle(self) -> None:
        keys = [k for k, _, _ in self.apps()]
        self.show(keys[(keys.index(self.current) + 1) % len(keys)])

    def focus_primary(self) -> None:
        self.focus_app(self.current)
