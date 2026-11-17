"""MABEL-OS: Mabel in a terminal. Channels COMMS, LIFE, AUDIO (INVENTORY comes later).

Run: mabel   (or: uv run --project ~/Documents/mabel/tui mabel)
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static, TabbedContent, TabPane

from . import config
from .api import Mabel
from .audio import Audio
from .comms import Comms
from .life import Life
from .player import Player

LOGO = "▙▟ MABEL-OS 2.6"


class MabelApp(App):
    CSS_PATH = "theme.tcss"
    TITLE = "MABEL-OS"
    BINDINGS = [
        Binding("f1", "channel('comms')", "COMMS", priority=True),
        Binding("f2", "channel('life')", "LIFE", priority=True),
        Binding("f3", "channel('audio')", "AUDIO", priority=True),
        Binding("ctrl+p", "pause", "play/pause", priority=True),
        Binding("ctrl+q", "quit", "power off", priority=True),
    ]

    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.mabel = Mabel(cfg.url, cfg.token)
        self.player = Player(self.mabel.stream_url, cfg.token, cfg.volume, on_scrobble=self._scrobble)
        self._status = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="casing"):
            yield Static(id="casing-left")
            yield Static(id="casing-clock")
        with TabbedContent(initial="comms"):
            with TabPane("CH-01 COMMS", id="comms"):
                yield Comms()
            with TabPane("CH-02 LIFE", id="life"):
                yield Life()
            with TabPane("CH-03 AUDIO", id="audio"):
                yield Audio()
        with Vertical(id="bottom"):
            yield Static(id="statusline")
            yield Footer()

    def on_mount(self) -> None:
        self._tick()
        self.set_interval(1, self._tick)
        self.refresh_statusline()

    def _tick(self) -> None:
        host = self.cfg.url.split("//", 1)[-1]
        left = Text()
        left.append(LOGO, style="bold #2a2114")
        left.append("  ░  TERMINAL UNIT 01  ░  LINK ", style="#2a2114")
        left.append(host, style="bold #2a2114")
        self.query_one("#casing-left", Static).update(left)
        self.query_one("#casing-clock", Static).update(
            Text(datetime.now().strftime("%a %d %b %Y  %H:%M:%S").upper(), style="bold #2a2114"))

    # ── status strip ────────────────────────────────────────────────────

    def set_status(self, text: str) -> None:
        self._status = text
        self.refresh_statusline()

    def refresh_statusline(self) -> None:
        line = Text()
        song = self.player.current
        if song:
            line.append("❚❚ " if self.player.paused else "▶ ", style="bold #2a2114")
            line.append(f"{song.get('title', '')} · {song.get('artist', '')}"[:60], style="#2a2114")
        else:
            line.append("◌ deck empty", style="#2a2114")
        if self._status:
            line.append(f"   {self._status}", style="bold #6b1d0a")
        try:
            self.query_one("#statusline", Static).update(line)
        except Exception:
            pass

    # ── actions ─────────────────────────────────────────────────────────

    def on_channel(self, name: str) -> bool:
        """Is this channel the one on screen? Background loads only take focus then."""
        try:
            return self.query_one(TabbedContent).active == name
        except Exception:
            return False

    FOCUS_ON = {"comms": "#prompt", "life": "#life-status", "audio": "#results"}

    def action_channel(self, name: str) -> None:
        # Drop focus first: a focused widget left in the old channel pulls the tabs back to it.
        self.screen.set_focus(None)
        self.query_one(TabbedContent).active = name

        def settle() -> None:
            try:
                self.query_one(self.FOCUS_ON[name]).focus()
            except Exception:
                pass
        self.call_after_refresh(settle)

    async def action_pause(self) -> None:
        await self.player.toggle()

    def _scrobble(self, song_id: str, submission: bool) -> None:
        asyncio.create_task(self._send_scrobble(song_id, submission))

    async def _send_scrobble(self, song_id: str, submission: bool) -> None:
        try:
            await self.mabel.music_write("scrobble", id=song_id, submission=submission)
        except Exception:
            pass

    async def on_unmount(self) -> None:
        await self.player.stop()
        await self.mabel.close()


def main() -> None:
    MabelApp(config.load()).run()


if __name__ == "__main__":
    main()
