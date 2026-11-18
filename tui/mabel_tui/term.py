"""TERM: real shells inside MABEL-OS (pyte emulates the terminal, a pty runs the shell).

Every key goes to the shell (vim, htop, ssh work) except alt+1–6 (channels) and ctrl+] which
steps back out to the sections. Tabs: N new shell, [ ] previous/next (in nav mode), X close.
shift+PageUp/PageDown scroll back. The 16 terminal colours follow the MABEL-OS palette.
"""
from __future__ import annotations

import fcntl
import os
import pty
import signal
import struct
import termios

import pyte
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.geometry import Size
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import ContentSwitcher, Static

from .nav import mark
from .palette import P

KEYS = {"enter": b"\r", "tab": b"\t", "shift+tab": b"\x1b[Z", "backspace": b"\x7f", "escape": b"\x1b",
        "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
        "home": b"\x1b[H", "end": b"\x1b[F", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
        "delete": b"\x1b[3~", "insert": b"\x1b[2~", "ctrl+up": b"\x1b[1;5A", "ctrl+down": b"\x1b[1;5B",
        "ctrl+right": b"\x1b[1;5C", "ctrl+left": b"\x1b[1;5D",
        "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS", "f5": b"\x1b[15~",
        "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~", "f9": b"\x1b[20~", "f10": b"\x1b[21~",
        "f11": b"\x1b[23~", "f12": b"\x1b[24~", "ctrl+space": b"\x00", "ctrl+at": b"\x00"}
APP_CURSOR = {"up": b"\x1bOA", "down": b"\x1bOB", "right": b"\x1bOC", "left": b"\x1bOD"}
LEAVE_KEY = "ctrl+right_square_bracket"


def ansi_colours() -> dict[str, str]:
    """pyte's colour names → the MABEL-OS palette (same family as the kitty/foot themes)."""
    return {"black": P.bg, "red": P.red, "green": "#a8c66c", "brown": P.amber, "yellow": P.amber,
            "blue": "#6aa6c8", "magenta": "#d98bb5", "cyan": P.teal, "white": P.beige,
            "brightblack": P.beige_dark, "brightred": P.orange, "brightgreen": "#c8e08a",
            "brightbrown": P.amber_hi, "brightyellow": P.amber_hi, "brightblue": "#8cc4e0",
            "brightmagenta": "#f0a8cc", "brightcyan": "#7fe8dc", "brightwhite": "#f5ecd2"}


class _Screen(pyte.HistoryScreen):
    """pyte, tolerant of the private SGR sequences (CSI > … m, e.g. vim's modifyOtherKeys request)."""

    def select_graphic_rendition(self, *attrs, private=False, **kw):
        if private:
            return                                              # keyboard-mode requests, not colours
        super().select_graphic_rendition(*attrs)


class Terminal(Widget, can_focus=True):
    """One shell in a pty, drawn from pyte's screen line by line."""

    class Exited(Message):
        def __init__(self, terminal: "Terminal"):
            super().__init__()
            self.terminal = terminal

    def __init__(self, title: str = "shell", **kw):
        super().__init__(**kw)
        self.title = title
        self.screen_ = _Screen(80, 24, history=2000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen_)
        self.fd: int | None = None
        self.pid: int | None = None
        self.alive = False
        self.scrolled = 0
        self._colours = ansi_colours()

    # ── the process ────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.spawn()

    def spawn(self) -> None:
        cols, rows = max(20, self.size.width or 80), max(5, self.size.height or 24)
        self.screen_.resize(rows, cols)
        pid, fd = pty.fork()
        if pid == 0:                                            # the child: become the shell
            env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", MABEL_OS="1")
            shell = os.environ.get("SHELL") or "/bin/bash"
            os.chdir(os.path.expanduser("~"))
            os.execvpe(shell, [shell, "-l"], env)
        self.pid, self.fd, self.alive = pid, fd, True
        self._set_size(rows, cols)
        import asyncio
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def _readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        if not data:
            self._ended()
            return
        try:
            self.stream.feed(data)
        except Exception:                                       # an escape sequence pyte can't parse: skip it
            pass
        if self.scrolled:
            self.scrolled = 0
        self.refresh()

    def _ended(self) -> None:
        import asyncio
        if self.fd is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.fd)
                os.close(self.fd)
            except (OSError, RuntimeError):
                pass
        if self.pid:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        self.fd, self.alive = None, False
        self.stream.feed(b"\r\n\x1b[33m[the shell ended: enter starts a new one, X closes the tab]\x1b[0m")
        self.refresh()
        self.post_message(self.Exited(self))

    def _set_size(self, rows: int, cols: int) -> None:
        if self.fd is not None:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def write(self, data: bytes) -> None:
        if self.fd is not None:
            try:
                os.write(self.fd, data)
            except OSError:
                pass

    def close(self) -> None:
        if self.pid and self.alive:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass

    def on_unmount(self) -> None:
        self.close()

    def on_resize(self, event: events.Resize) -> None:
        rows, cols = max(5, event.size.height), max(20, event.size.width)
        if (rows, cols) != (self.screen_.lines, self.screen_.columns):
            self.screen_.resize(rows, cols)
            self._set_size(rows, cols)
            self.refresh()

    # ── keys and paste ─────────────────────────────────────────────────

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key == LEAVE_KEY:
            return                                              # let the app take us back to the sections
        if key in ("shift+pageup", "shift+pagedown"):
            event.stop(); event.prevent_default()
            self._scroll(-1 if key == "shift+pageup" else 1)
            return
        event.stop(); event.prevent_default()
        if not self.alive:
            if key == "enter":
                self.stream.feed(b"\r\n")
                self.spawn()
            return
        mode_app_cursor = (1 << 5) in self.screen_.mode       # DECCKM (application cursor keys)
        if mode_app_cursor and key in APP_CURSOR:
            self.write(APP_CURSOR[key])
        elif key in KEYS:
            self.write(KEYS[key])
        elif key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
            self.write(bytes([ord(key[5]) & 0x1F]))
        elif key.startswith("alt+") and event.character:
            self.write(b"\x1b" + event.character.encode())
        elif event.character:
            self.write(event.character.encode())

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        data = event.text.replace("\r\n", "\n").replace("\n", "\r").encode()
        if (2004 << 5) in self.screen_.mode:                   # bracketed paste, if the program asked
            data = b"\x1b[200~" + data + b"\x1b[201~"
        self.write(data)

    def _scroll(self, direction: int) -> None:
        if direction < 0 and self.screen_.history.top:
            self.screen_.prev_page()
            self.scrolled += 1
        elif direction > 0 and self.scrolled:
            self.screen_.next_page()
            self.scrolled -= 1
        self.refresh()

    # ── drawing ────────────────────────────────────────────────────────

    def _colour(self, value: str, fallback: str) -> str:
        if value == "default":
            return fallback
        if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
            return "#" + value
        return self._colours.get(value, fallback)

    def render_line(self, y: int) -> Strip:
        screen = self.screen_
        if y >= screen.lines:
            return Strip.blank(self.size.width, Style(bgcolor=P.bg))
        row = screen.buffer[y]
        segments, run, run_style = [], "", None
        cursor = (self.has_focus and not screen.cursor.hidden and y == screen.cursor.y and not self.scrolled)
        for x in range(screen.columns):
            ch = row[x]
            fg, bg = self._colour(ch.fg, P.amber), self._colour(ch.bg, P.bg)
            if ch.reverse:
                fg, bg = bg, fg
            if cursor and x == screen.cursor.x:
                fg, bg = P.bg, P.amber_hi
            style = Style(color=fg, bgcolor=bg, bold=ch.bold, italic=ch.italics, underline=ch.underscore,
                          strike=ch.strikethrough)
            if style != run_style and run:
                segments.append(Segment(run, run_style))
                run = ""
            run_style = style
            run += ch.data or " "
        if run:
            segments.append(Segment(run, run_style))
        return Strip(segments).adjust_cell_length(self.size.width, Style(bgcolor=P.bg))

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return container.height


class TermView(Vertical):
    """The TERM channel: a tab strip and the shells (one is shown)."""
    BINDINGS = [
        Binding("N", "new_tab", "new shell"),
        Binding("X", "close_tab", "close shell", show=False),
        Binding("right_square_bracket", "next_tab", "next", show=False),
        Binding("left_square_bracket", "prev_tab", "prev", show=False),
    ]

    def __init__(self, **kw):
        super().__init__(id="term-view", **kw)
        self.count = 0

    def compose(self) -> ComposeResult:
        yield Static(id="term-tabs")
        box = mark(ContentSwitcher(id="term-switch", classes="panel"), primary=self._focus_current)
        box.border_title = "TERM"
        box.border_subtitle = "ctrl+] back to the sections · shift+PgUp scroll back"
        yield box

    def on_mount(self) -> None:
        self.call_after_refresh(self.action_new_tab)

    @property
    def switch(self) -> ContentSwitcher:
        return self.query_one("#term-switch", ContentSwitcher)

    def terminals(self) -> list[Terminal]:
        return list(self.switch.query(Terminal))

    def _focus_current(self) -> None:
        current = self.switch.current
        if current:
            self.query_one(f"#{current}", Terminal).focus()

    def _tabs(self) -> None:
        t = Text()
        for term in self.terminals():
            on = term.id == self.switch.current
            label = f" {term.title} " + ("" if term.alive else "✖ ")
            t.append(label, style=f"bold {P.bg} on {P.amber}" if on else P.amber_dim)
            t.append(" ")
        t.append("   N new · [ ] switch · X close (in nav mode)", style=P.amber_faint)
        self.query_one("#term-tabs", Static).update(t)

    async def action_new_tab(self) -> None:
        self.count += 1
        term = Terminal(title=f"shell {self.count}", id=f"term-{self.count}")
        await self.switch.mount(term)
        self.switch.current = term.id
        self._tabs()
        if self.app.on_channel("term"):
            term.focus()

    def _move(self, step: int) -> None:
        terms = self.terminals()
        if not terms:
            return
        ids = [t.id for t in terms]
        i = ids.index(self.switch.current) if self.switch.current in ids else 0
        self.switch.current = ids[(i + step) % len(ids)]
        self._tabs()

    def action_next_tab(self) -> None:
        self._move(1)

    def action_prev_tab(self) -> None:
        self._move(-1)

    async def action_close_tab(self) -> None:
        current = self.switch.current
        if not current:
            return
        term = self.query_one(f"#{current}", Terminal)
        term.close()
        await term.remove()
        rest = self.terminals()
        self.switch.current = rest[-1].id if rest else None
        if not rest:
            await self.action_new_tab()
        self._tabs()

    def on_terminal_exited(self, event: Terminal.Exited) -> None:
        self._tabs()
