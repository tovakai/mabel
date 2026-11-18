"""MABEL-OS: Mabel in a terminal. Channels COMMS, LIFE, AUDIO and INVENTORY (the Gremlin Index).

Run: mabel   (or: uv run --project ~/Documents/mabel/tui mabel)
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static, TabbedContent, TabPane

from . import config, nav, update
from .api import Mabel
from .audio import Audio
from .comms import Comms
from .creative import Creative
from .inventory import Inventory
from .term import Terminal, TermView
from .vaultview import VaultView
from .life import Life
from .mpris import Mpris
from .player import Player
from .tkindex import TKIndex
from .palette import P

LOGO = "▙▟ MABEL-OS 2.6"


HELP = {
    "everywhere": [("alt+1–7 / F1–F7", "switch channel (F7 again: next CREATIVE app)"),
                   ("h j k l", "move between sections (the lit border)"),
                   ("i / a / enter", "go into the lit section"),
                   ("esc", "back out to the sections"),
                   ("inside: h j k l", "like the arrow keys, where the section doesn't use the letter"),
                   ("ctrl+z", "drop to a plain shell (exit comes back)"),
                   ("ctrl+p", "play / pause music"), ("ctrl+t", "colour tuner (for screens that show colours their own way)"),
                   ("F12", "install the update when the header says UPDATE (pushed from the PC)"),
                   ("?", "this list"), ("ctrl+q", "power off")],
    "comms": [("enter", "send"), ("ctrl+n", "new chat"), ("ctrl+s", "show/hide the chat list"),
              ("esc, k, then i", "into the conversation to scroll it"),
              ("ctrl+s, then h → list", "open another chat (the list hides on the deck)")],
    "life": [("r", "refresh now"), ("v / p", "open her last dream / paper note in VAULT")],
    "audio": [("/", "search"), ("↑↓ enter", "open album/artist, or play from that song"), ("a / n", "append / play next"),
              ("backspace", "back"), ("p", "playlists"), ("s", "add selection to a playlist"),
              ("S", "save the queue as a playlist"), ("d", "remove from the open playlist"),
              ("O", "download for offline (the open album/playlist, else the selection)"),
              ("o", "offline library (d removes a download)"), ("↓", "marks downloaded songs"),
              ("z", "shuffle on/off (shuffles the rest of the queue)"), ("R", "a random mix from the library"),
              ("space", "pause"), ("[ ]", "previous / next"),
              ("← →", "seek 10 s"), ("+ -", "volume")],
    "vault": [("o", "open a note (fuzzy)"), ("/", "search the vault's text"), ("#", "browse tags"),
              ("r", "recently changed"), ("enter / click", "open a folder, note, heading or link"),
              ("f", "follow a link in the note"), ("backspace", "back (note history, then folders)"),
              ("i / e", "edit (vim: hjkl w b e 0 $ gg G, i a o O, x dd dw D cw cc yy p P u ctrl+r v V :w :q)"),
              ("[[ while typing", "pick a note to link"), ("ctrl+s", "save"), ("esc esc", "back to reading (saves)"),
              ("D", "today's daily note (from your template)"), ("n / R", "new note / rename (links updated)"),
              ("V", "switch vault: core of truth ↔ Mabel's journal"), ("M", "ask Mabel about this note")],
    "term": [("(inside a shell)", "every key goes to the program: vim, htop, ssh all work"),
             ("ctrl+]", "back out to the sections"), ("alt+1–6", "switch channel (still works inside)"),
             ("shift+PgUp / PgDn", "scroll back"), ("N", "new shell tab (in nav mode)"),
             ("[ ]", "previous / next shell (nav mode)"), ("X", "close the shell tab (nav mode)")],
    "creative": [("F7 / alt+7 again", "next app: PIXEL · TRACKER"),
                 ("PIXEL: hjkl / arrows", "move the cursor (HJKL ×4); space or enter uses the tool"),
                 ("b e f n r o c", "pencil · eraser · fill · line · rect · ellipse · pick a colour"),
                 ("t", "pen down: moving draws (t / esc lifts it)"),
                 ("F m g O", "filled shapes · mirror · grid · onion skin"),
                 ("+ - 0", "zoom (1 = exact half-blocks, 2+ = big square pixels) · 0 fits the canvas"),
                 ("[ ] x #", "previous / next colour · swap fg and bg · type a hex colour"),
                 ("{ } A V", "layer below / above · add a layer · hide it"),
                 ("< > N p", "previous / next frame · new frame (a copy) · play the animation"),
                 ("u ctrl+r ctrl+s", "undo · redo · save"),
                 (":", ":new 28 28 · :w file · :e file · :avatar · :export strip 4 · :help for all")],
    "inventory": [("↑↓←→ enter", "move between cards and lines, open"), ("backspace", "zoom out one step"),
                  ("H", "home"), ("/", "search"), ("b", "Mabel's housekeeping inbox (a apply, x tick off, v open in VAULT)"),
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
    ENABLE_COMMAND_PALETTE = False                         # ctrl+p is play/pause (and the shell's history in TERM)
    # Wide = his PC (as designed); narrow = the 7" cyberdeck (1024×600 ≈ 128×37): stacked panels.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (140, "-wide")]
    TITLE = "MABEL-OS"
    BINDINGS = [
        Binding("f1", "fchannel('comms')", "COMMS", priority=True),
        Binding("f2", "fchannel('life')", "LIFE", priority=True),
        Binding("f3", "fchannel('audio')", "AUDIO", priority=True),
        Binding("f4", "fchannel('inventory')", "INVENTORY", priority=True),
        Binding("alt+1", "channel('comms')", "COMMS", show=False, priority=True),
        Binding("alt+2", "channel('life')", "LIFE", show=False, priority=True),
        Binding("alt+3", "channel('audio')", "AUDIO", show=False, priority=True),
        Binding("alt+4", "channel('inventory')", "INVENTORY", show=False, priority=True),
        Binding("f5", "fchannel('vault')", "VAULT", show=False, priority=True),
        Binding("alt+5", "channel('vault')", "VAULT", show=False, priority=True),
        Binding("f6", "fchannel('term')", "TERM", show=False, priority=True),
        Binding("alt+6", "channel('term')", "TERM", show=False, priority=True),
        Binding("f7", "fchannel('creative')", "CREATIVE", show=False, priority=True),
        Binding("alt+7", "channel('creative')", "CREATIVE", show=False, priority=True),
        Binding("ctrl+right_square_bracket", "nav_out", show=False),
        Binding("ctrl+z", "shell", "shell", show=False, priority=True),
        Binding("question_mark", "help", "keys"),
        Binding("ctrl+t", "tune", "colours", show=False, priority=True),
        Binding("h", "navkey('h')", show=False), Binding("j", "navkey('j')", show=False),
        Binding("k", "navkey('k')", show=False), Binding("l", "navkey('l')", show=False),
        Binding("i,a,enter", "nav_enter", show=False),
        Binding("escape", "nav_out", "out", show=False),
        Binding("ctrl+p", "pause", "play/pause", priority=True),
        Binding("ctrl+q", "quit", "power off", priority=True),
        Binding("f12", "update", "update", show=False, priority=True),
    ]

    def __init__(self, cfg: config.Config):
        super().__init__()
        self.cfg = cfg
        self.mabel = Mabel(cfg.url, cfg.token, cfg.preset)
        self.index = TKIndex(cfg.index_url)
        from .music_source import MabelMusic, Navidrome
        from .offline import Offline
        nd = cfg.navidrome
        self.music = Navidrome(nd["url"], nd["user"], nd["password"]) if nd else MabelMusic(self.mabel)
        self.offline = Offline()
        self.player = Player(self._stream_url, cfg.token if self.music.label == "mabel" else None, cfg.volume,
                             on_scrobble=self._scrobble)
        self.mpris = Mpris(self, self.player)
        self._status = ""
        self.nav_section = None                      # the lit section in nav mode
        self._update_ready: str | None = None        # a newer build Mabel serves (update.py)
        self._updating = False
        self._last_input = time.monotonic()

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
            with TabPane("CH-05 VAULT", id="vault"):
                yield VaultView()
            with TabPane("CH-06 TERM", id="term"):
                yield TermView()
            with TabPane("CH-07 CREATIVE", id="creative"):
                yield Creative()
        with Vertical(id="bottom"):
            yield Static(id="statusline")
            yield Footer()

    async def on_mount(self) -> None:
        await self.mpris.start()                      # the desktop's media widgets see the deck
        self.watch(self.screen, "focused", self._focus_moved, init=False)
        self._tick()
        self.set_interval(1, self._tick)
        self.refresh_statusline()
        if update.installed_build():                  # installed by Mabel's installer: she can push updates
            self.set_timer(10, self._check_update)
            self.set_interval(update.CHECK_EVERY_S, self._check_update)

    def _tick(self) -> None:
        host = self.cfg.url.split("//", 1)[-1]
        left = Text()
        left.append(LOGO, style=f"bold {P.casing_text}")
        left.append("  ░  TERMINAL UNIT 01  ░  LINK ", style=P.casing_text)
        left.append(host, style=f"bold {P.casing_text}")
        if self._updating:
            left.append("  ▲ UPDATING…", style=f"bold {P.rust}")
        elif self._update_ready:
            left.append("  ▲ UPDATE F12", style=f"bold {P.rust}")
        self.query_one("#casing-left", Static).update(left)
        self.query_one("#casing-clock", Static).update(
            Text(datetime.now().strftime("%a %d %b %Y  %H:%M:%S").upper(), style=f"bold {P.casing_text}"))

    # ── updates pushed from the PC (update.py) ──────────────────────────

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, (events.Key, events.MouseDown)):
            self._last_input = time.monotonic()
        await super().on_event(event)

    async def _check_update(self, *, quiet: bool = True) -> None:
        served = await update.served_build(self.cfg.url)
        if update.newer(update.installed_build(), served):
            if self._update_ready != served:
                self._update_ready = served
                self.notify("A new MABEL-OS is ready. F12 installs it and restarts.", title="UPDATE", timeout=10)
                self._tick()
            idle = time.monotonic() - self._last_input >= update.IDLE_BEFORE_AUTO_S
            playing = bool(self.player.current) and not self.player.paused
            if self.cfg.auto_update and idle and not playing:
                self.action_update()
        elif not quiet:
            self.notify("Already the newest build." if served else "Couldn't ask Mabel for the newest build.",
                        title="UPDATE")

    def action_update(self) -> None:
        if self._updating:
            return
        if not update.installed_build():
            self.notify("This copy runs from the source tree, so it's always current.", title="UPDATE")
            return
        if not self._update_ready:
            self.run_worker(self._check_update(quiet=False), exclusive=True, group="update")
            return
        self._updating = True
        self._tick()
        self.run_worker(self._install_update, thread=True, exclusive=True, group="update")

    def _install_update(self) -> None:
        ok, tail = update.apply(self.cfg.url)
        self.call_from_thread(self._update_done, ok, tail)

    def _update_done(self, ok: bool, tail: str) -> None:
        self._updating = False
        if ok:
            self.exit(result="restart")              # main() starts the new build in this terminal
            return
        self._tick()
        self.notify(tail or "the installer failed", title="UPDATE FAILED", severity="error", timeout=15)

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

    # ── the terminal gets its keys ─────────────────────────────────────

    def check_action(self, action: str, parameters):
        """Inside a TERM shell, F-keys, ctrl+p/q/t/z belong to the program (htop, vim, the shell)."""
        if isinstance(self.focused, Terminal) and action not in ("channel", "nav_out"):
            return False                                   # only alt+1–6 and ctrl+] stay with MABEL-OS
        return True

    def action_fchannel(self, name: str) -> None:
        self.action_channel(name)

    def action_shell(self) -> None:
        """ctrl+z: the whole screen becomes a plain shell; exit comes back to MABEL-OS."""
        import os
        import subprocess
        shell = os.environ.get("SHELL") or "/bin/bash"
        with self.suspend():
            print("\033[33mMABEL-OS is paused — type exit to come back\033[0m", flush=True)
            subprocess.call([shell], cwd=os.path.expanduser("~"))

    def open_in_vault(self, vault_name: str, rel: str, line: int | None = None) -> None:
        """Mabel links: open a note from another channel in VAULT."""
        self.action_channel("vault")
        self.query_one(VaultView).open_external(vault_name, rel, line)

    def ask_about_note(self, name: str, rel: str, content: str) -> None:
        """VAULT's M: the next COMMS message goes to Mabel with this note attached."""
        comms = self.query_one(Comms)
        comms.pending_note = (name, rel, content)
        self.action_channel("comms")
        self.query_one("#prompt").placeholder = f"ask Mabel about “{name}”…"

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
        if name == "creative" and self.query_one(TabbedContent).active == "creative":
            self.query_one(Creative).cycle()               # F7 again: the next app in the suite
            return
        # Drop focus first: a focused widget left in the old channel pulls the tabs back to it.
        self.screen.set_focus(None)
        self.query_one(TabbedContent).active = name

        def settle() -> None:
            self._light(None)
            if name == "comms":
                self.query_one("#prompt").focus()          # chat is for typing: straight into the prompt
            elif name == "term":
                self.query_one(TermView)._focus_current()  # a terminal is for typing too
            elif name == "creative":
                self.query_one(Creative).focus_primary()   # straight onto the canvas
            else:
                self.start_nav()
        self.set_timer(0.15, settle)                   # after the pane has its size, so sections can be lit

    def _stream_url(self, song_id: str) -> str:
        """A downloaded song plays from disk; the rest streams."""
        return self.offline.local(song_id) or self.music.stream_url(song_id)

    async def action_pause(self) -> None:
        await self.player.toggle()

    def _scrobble(self, song_id: str, submission: bool) -> None:
        asyncio.create_task(self._send_scrobble(song_id, submission))

    async def _send_scrobble(self, song_id: str, submission: bool) -> None:
        try:
            await self.music.music_write("scrobble", id=song_id, submission=submission)
        except Exception:
            pass

    async def on_unmount(self) -> None:
        await self.player.stop()
        await self.mpris.stop()
        await self.mabel.close()
        await self.music.close()
        await self.index.close()


def main() -> None:
    # textual_image asks the terminal what it supports when first imported. Do that now, before
    # Textual owns the terminal: imported later (first cover/dream picture) it raced Textual's input
    # thread for the reply and froze the app on the deck (black screen, 2026-11-18).
    import textual_image.widget  # noqa: F401
    if MabelApp(config.load()).run() == "restart":
        update.restart()


if __name__ == "__main__":
    main()
