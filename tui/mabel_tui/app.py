"""MABEL-OS: Mabel in a terminal. Channels COMMS, LIFE, AUDIO and INVENTORY (the Gremlin Index).

Run: mabel   (or: uv run --project ~/Documents/mabel/tui mabel)
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static, TabbedContent, TabPane

from . import config, nav
from .api import Mabel
from .audio import Audio
from .comms import Comms
from .inventory import Inventory
from .life import Life
from .mpris import Mpris
from .player import Player
from .tkindex import TKIndex
from .palette import P

LOGO = "▙▟ MABEL-OS 2.6"


HELP = {
    "everywhere": [("alt+1–4 / F1–F4", "switch channel"),
                   ("h j k l", "move between sections (the lit border)"),
                   ("i / a / enter", "go into the lit section"),
                   ("esc", "back out to the sections"),
                   ("inside: h j k l", "like the arrow keys, where the section doesn't use the letter"),
                   ("ctrl+p", "play / pause music"), ("ctrl+t", "colour tuner (for screens that show colours their own way)"),
                   ("?", "this list"), ("ctrl+q", "power off")],
    "comms": [("enter", "send"), ("ctrl+n", "new chat"), ("ctrl+s", "show/hide the chat list"),
              ("esc, k, then i", "into the conversation to scroll it"),
              ("ctrl+s, then h → list", "open another chat (the list hides on the deck)")],
    "life": [("r", "refresh now"), ("tab, ↑↓", "scroll a panel")],
    "audio": [("/", "search"), ("↑↓ enter", "open album/artist, or play from that song"), ("a / n", "append / play next"),
              ("backspace", "back"), ("p", "playlists"), ("s", "add selection to a playlist"),
              ("S", "save the queue as a playlist"), ("d", "remove from the open playlist"),
              ("z", "shuffle on/off (shuffles the rest of the queue)"), ("R", "a random mix from the library"),
              ("space", "pause"), ("[ ]", "previous / next"),
              ("← →", "seek 10 s"), ("+ -", "volume")],
    "inventory": [("↑↓←→ enter", "move between cards and lines, open"), ("backspace", "zoom out one step"),
                  ("H", "home"), ("/", "search"), ("b", "Mabel's housekeeping inbox (a apply, x tick off)"),
                  ("o", "loose ends"), ("e m c r k", "edit, move, check out, return, nickname"),
                  ("p", "go to the item's place"), ("u", "undo the last change"), ("ctrl+s", "save in a dialog")],
}


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", "close")]

    def __init__(self, channel: str):
        super().__init__()
        self.channel = channel

    def compose(self) -> ComposeResult:
        text = Text()
        for part in (self.channel, "everywhere"):
            text.append(f"{part.upper()}\n", style=f"bold {P.beige}")
            for keys, what in HELP.get(part, []):
                text.append(f"  {keys:<22}", style=f"bold {P.amber_hi}")
                text.append(f"{what}\n", style=P.amber)
            text.append("\n")
        box = Static(text, id="help", classes="panel")
        box.border_title = "KEYS"
        box.border_subtitle = "esc closes"
        yield box


class MabelApp(App):
    CSS_PATH = "theme.tcss"
    # Wide = his PC (as designed); narrow = the 7" cyberdeck (1024×600 ≈ 128×37): stacked panels.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (140, "-wide")]
    TITLE = "MABEL-OS"
    BINDINGS = [
        Binding("f1", "channel('comms')", "COMMS", priority=True),
        Binding("f2", "channel('life')", "LIFE", priority=True),
        Binding("f3", "channel('audio')", "AUDIO", priority=True),
        Binding("f4", "channel('inventory')", "INVENTORY", priority=True),
        Binding("alt+1", "channel('comms')", "COMMS", show=False, priority=True),
        Binding("alt+2", "channel('life')", "LIFE", show=False, priority=True),
        Binding("alt+3", "channel('audio')", "AUDIO", show=False, priority=True),
        Binding("alt+4", "channel('inventory')", "INVENTORY", show=False, priority=True),
        Binding("question_mark", "help", "keys"),
        Binding("ctrl+t", "tune", "colours", show=False, priority=True),
        Binding("h", "navkey('h')", show=False), Binding("j", "navkey('j')", show=False),
        Binding("k", "navkey('k')", show=False), Binding("l", "navkey('l')", show=False),
        Binding("i,a,enter", "nav_enter", show=False),
        Binding("escape", "nav_out", "out", show=False),
        Binding("ctrl+p", "pause", "play/pause", priority=True),
        Binding("ctrl+q", "quit", "power off", priority=True),
    ]

    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.mabel = Mabel(cfg.url, cfg.token)
        self.index = TKIndex(cfg.index_url)
        self.player = Player(self.mabel.stream_url, cfg.token, cfg.volume, on_scrobble=self._scrobble)
        self.mpris = Mpris(self, self.player)
        self._status = ""
        self.nav_section = None                      # the lit section in nav mode

    def get_css_variables(self) -> dict[str, str]:
        from .palette import css_variables
        return {**super().get_css_variables(), **css_variables()}     # $mo-* = palette.py (PC or deck)

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
            with TabPane("CH-04 INVENTORY", id="inventory"):
                yield Inventory()
        with Vertical(id="bottom"):
            yield Static(id="statusline")
            yield Footer()

    async def on_mount(self) -> None:
        await self.mpris.start()                      # the desktop's media widgets see the deck
        self.watch(self.screen, "focused", self._focus_moved, init=False)
        self._tick()
        self.set_interval(1, self._tick)
        self.refresh_statusline()

    def _tick(self) -> None:
        host = self.cfg.url.split("//", 1)[-1]
        left = Text()
        left.append(LOGO, style=f"bold {P.casing_text}")
        left.append("  ░  TERMINAL UNIT 01  ░  LINK ", style=P.casing_text)
        left.append(host, style=f"bold {P.casing_text}")
        self.query_one("#casing-left", Static).update(left)
        self.query_one("#casing-clock", Static).update(
            Text(datetime.now().strftime("%a %d %b %Y  %H:%M:%S").upper(), style=f"bold {P.casing_text}"))

    # ── status strip ────────────────────────────────────────────────────

    def set_status(self, text: str) -> None:
        self._status = text
        self.refresh_statusline()

    def refresh_statusline(self) -> None:
        line = Text()
        song = self.player.current
        if song:
            line.append("❚❚ " if self.player.paused else "▶ ", style=f"bold {P.casing_text}")
            line.append(f"{song.get('title', '')} · {song.get('artist', '')}"[:60], style=P.casing_text)
        else:
            line.append("◌ deck empty", style=P.casing_text)
        if self._status:
            line.append(f"   {self._status}", style=f"bold {P.rust}")
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

    # ── vim-style sections (nav.py) ─────────────────────────────────────

    def _main_screen(self) -> bool:
        return self.screen is self.screen_stack[0]

    def _pane(self):
        return self.query_one(f"#{self.query_one(TabbedContent).active}")

    def _light(self, section) -> None:
        if self.nav_section is not None:
            self.nav_section.remove_class("-nav")
        self.nav_section = section
        if section is not None:
            section.add_class("-nav")
            self.screen.set_focus(None)
            section.scroll_visible()

    def _focus_moved(self, widget) -> None:
        if widget is not None and self.nav_section is not None:
            self.nav_section.remove_class("-nav")          # clicked or tabbed into something: inside now

    def start_nav(self) -> None:
        sections = nav.visible_sections(self._pane())
        if sections:
            self._light(sections[0])

    async def on_key(self, event) -> None:
        """Nav mode (nothing focused): a key the app doesn't use goes to the channel on screen,
        so space, z, R, / and friends work without first going into a section."""
        if self.focused is not None or not self._main_screen():
            return
        pane = self._pane()
        view = pane.children[0] if pane.children else None
        if view is None:
            return
        try:
            bindings = view._bindings.get_bindings_for_key(event.key)
        except Exception:
            return
        if bindings:
            event.stop()
            await view.run_action(bindings[0].action)

    def action_navkey(self, key: str) -> None:
        if not self._main_screen():
            return
        if self.focused is None:
            sections = nav.visible_sections(self._pane())
            current = self.nav_section if self.nav_section in sections else None
            if current is None:
                return self.start_nav()
            target = nav.neighbour(current, sections, key)
            if target is not None:
                self._light(target)
        else:
            self.simulate_key(nav.ARROWS[key])             # inside a section: hjkl are the arrows

    def action_nav_enter(self) -> None:
        if self._main_screen() and self.focused is None and self.nav_section is not None:
            section = self.nav_section
            section.remove_class("-nav")
            nav.enter(section)

    def action_nav_out(self) -> None:
        if not self._main_screen():
            return
        section = nav.section_of(self.focused)
        if section is not None:
            self._light(section)
        elif self.focused is None and self.nav_section is None:
            self.start_nav()

    def action_tune(self) -> None:
        from .tuner import Tuner
        if self._main_screen():
            self.push_screen(Tuner())

    def apply_palette(self) -> None:
        """After the tuner changed P: the stylesheet, then the views' own coloured text."""
        self.refresh_css(animate=False)
        self._tick()
        self.refresh_statusline()
        try:
            self.query_one(Life).action_refresh()
            self.query_one(Audio)._dirty = True
            inventory = self.query_one(Inventory)
            self.run_worker(inventory.render_page())
        except Exception:
            pass

    def action_help(self) -> None:
        self.push_screen(HelpScreen(self.query_one(TabbedContent).active))

    def action_channel(self, name: str) -> None:
        # Drop focus first: a focused widget left in the old channel pulls the tabs back to it.
        self.screen.set_focus(None)
        self.query_one(TabbedContent).active = name

        def settle() -> None:
            self._light(None)
            if name == "comms":
                self.query_one("#prompt").focus()          # chat is for typing: straight into the prompt
            else:
                self.start_nav()
        self.set_timer(0.15, settle)                   # after the pane has its size, so sections can be lit

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
        await self.mpris.stop()
        await self.mabel.close()
        await self.index.close()


def main() -> None:
    MabelApp(config.load()).run()


if __name__ == "__main__":
    main()
