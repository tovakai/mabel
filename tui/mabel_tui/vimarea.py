"""A vim-flavoured TextArea for editing notes (he uses vim mode in Obsidian too).

Modes: NORMAL (default), INSERT, VISUAL (v, characters) and V-LINE (V).
NORMAL: h j k l, w b e, 0 ^ $, gg G, counts (3j, 2dd), x, dd, dw, D, cc, cw, C, yy, yw, p P,
        o O, i a I A, J (join), u, ctrl+r, ., :w :q :wq (via the : line), / search in the note.
VISUAL: motions extend the selection; d/x delete, y yank, c change, > < indent.
Yanks go to an internal register and to the system clipboard (OSC 52, works over SSH too).
Typing [[ in INSERT asks the channel for a note to link (it posts LinkWanted).
"""
from __future__ import annotations

import re

from rich.style import Style
from textual import events
from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from .palette import P

WORD = re.compile(r"\w+|[^\w\s]+")


def mabel_theme() -> TextAreaTheme:
    return TextAreaTheme(
        name="mabel",
        base_style=Style(color=P.amber, bgcolor=P.bg),
        gutter_style=Style(color=P.amber_faint, bgcolor=P.bg),
        cursor_style=Style(color=P.bg, bgcolor=P.amber_hi),
        cursor_line_style=Style(bgcolor=P.panel),
        cursor_line_gutter_style=Style(color=P.amber_dim, bgcolor=P.panel),
        selection_style=Style(bgcolor=P.amber_faint),
        syntax_styles={
            "heading": Style(color=P.amber_hi, bold=True), "heading.marker": Style(color=P.amber_dim),
            "bold": Style(bold=True), "italic": Style(italic=True), "strikethrough": Style(strike=True),
            "link.label": Style(color=P.teal), "link.uri": Style(color=P.beige_dark, underline=True),
            "list.marker": Style(color=P.amber_dim), "inline_code": Style(color=P.beige),
            "punctuation.special": Style(color=P.amber_dim), "string": Style(color=P.beige),
            "comment": Style(color=P.beige_dark), "keyword": Style(color=P.teal),
        })


class VimArea(TextArea):
    class ModeChanged(Message):
        def __init__(self, mode: str):
            super().__init__()
            self.mode = mode

    class Command(Message):
        """:w, :q, :wq … for the channel to act on."""
        def __init__(self, command: str):
            super().__init__()
            self.command = command

    class LinkWanted(Message):
        pass

    def __init__(self, text: str = "", **kw):
        super().__init__(text, language="markdown", soft_wrap=True, show_line_numbers=False, tab_behavior="indent", **kw)
        self.register_theme(mabel_theme())
        self.theme = "mabel"
        self.mode = "normal"
        self.pending = ""            # operator / prefix keys typed so far (d, g, y, c, >, <, a count)
        self.register = ("", False)  # (text, linewise)
        self.anchor = None           # visual-mode start
        self.last_change = None      # for "."
        self.cmdline = None          # ":" or "/" text being typed, else None
        self.last_search = ""

    # ── mode plumbing ──────────────────────────────────────────────────

    def set_mode(self, mode: str) -> None:
        self.mode, self.pending = mode, ""
        if mode in ("normal", "insert"):
            self.anchor = None
            self.selection = type(self.selection)(self.cursor_location, self.cursor_location)
        self.post_message(self.ModeChanged(mode))

    @property
    def status(self) -> str:
        if self.cmdline is not None:
            return self.cmdline + "█"
        name = {"normal": "NORMAL", "insert": "INSERT", "visual": "VISUAL", "vline": "V-LINE"}[self.mode]
        return f"-- {name} --" + (f"  {self.pending}" if self.pending else "")

    async def _on_key(self, event: events.Key) -> None:
        if self.mode == "insert":
            if event.key == "escape":
                event.stop(); event.prevent_default()
                row, col = self.cursor_location
                self.set_mode("normal")
                self.move_cursor((row, max(0, col - 1)))
                return
            if event.character == "[" and self._char_before() == "[":
                await super()._on_key(event)
                self.post_message(self.LinkWanted())
                return
            await super()._on_key(event)
            return
        event.stop(); event.prevent_default()
        if self.cmdline is not None:
            self._cmdline_key(event)
        elif self.mode in ("visual", "vline"):
            self._visual_key(event)
        else:
            self._normal_key(event)
        self.post_message(self.ModeChanged(self.mode))

    def _char_before(self) -> str:
        row, col = self.cursor_location
        line = self.document.get_line(row)
        return line[col - 1] if col > 0 else ""

    # ── motions ────────────────────────────────────────────────────────

    def _line(self, row: int) -> str:
        return self.document.get_line(row)

    def _motion(self, key: str, count: int, loc=None):
        row, col = loc or self.cursor_location
        last = self.document.line_count - 1
        if key in ("h", "left"):
            return row, max(0, col - count)
        if key in ("l", "right"):
            return row, min(max(0, len(self._line(row)) - 1), col + count)
        if key in ("j", "down"):
            r = min(last, row + count); return r, min(col, max(0, len(self._line(r)) - 1))
        if key in ("k", "up"):
            r = max(0, row - count); return r, min(col, max(0, len(self._line(r)) - 1))
        if key == "0":
            return row, 0
        if key == "circumflex_accent":
            line = self._line(row); return row, len(line) - len(line.lstrip())
        if key == "dollar_sign":
            return row, max(0, len(self._line(row)) - 1)
        if key == "G":
            return (count - 1 if count > 1 else last), 0
        if key in ("w", "b", "e"):
            for _ in range(count):
                row, col = self._word(key, row, col)
            return row, col
        return None

    def _word(self, key: str, row: int, col: int):
        text = self._line(row)
        spans = [(m.start(), m.end()) for m in WORD.finditer(text)]
        if key == "w":
            nxt = [s for s, e in spans if s > col]
            if nxt:
                return row, nxt[0]
            return (row + 1, 0) if row + 1 < self.document.line_count else (row, max(0, len(text) - 1))
        if key == "e":
            nxt = [e - 1 for s, e in spans if e - 1 > col]
            if nxt:
                return row, nxt[0]
            return (row + 1, 0) if row + 1 < self.document.line_count else (row, col)
        prev = [s for s, e in spans if s < col]
        if prev:
            return row, prev[-1]
        return (row - 1, max(0, len(self._line(row - 1)) - 1)) if row > 0 else (row, 0)

    # ── NORMAL ─────────────────────────────────────────────────────────

    def _count(self) -> tuple[int, str]:
        m = re.match(r"(\d*)(.*)", self.pending)
        return (int(m.group(1)) if m.group(1) else 1), m.group(2)

    def _normal_key(self, event: events.Key) -> None:
        key, ch = event.key, event.character or ""
        if ch.isdigit() and (ch != "0" or re.match(r"\d", self.pending or "")):
            self.pending += ch
            return
        count, op = self._count()
        row, col = self.cursor_location
        if op in ("d", "c", "y", ">", "<"):
            self.pending = ""
            self._operator(op, key, ch, count)
            return
        if op == "g":
            self.pending = ""
            if ch == "g":
                self.move_cursor((0, 0))
            return
        if key in ("d", "c", "y", "g") or ch in (">", "<"):
            self.pending += ch or key
            return
        self.pending = ""
        target = self._motion(key, count)
        if target is not None:
            self.move_cursor(target)
            return
        if ch == "i":
            self.set_mode("insert")
        elif ch == "a":
            self.move_cursor((row, min(len(self._line(row)), col + 1))); self.set_mode("insert")
        elif ch == "I":
            self.move_cursor(self._motion("circumflex_accent", 1)); self.set_mode("insert")
        elif ch == "A":
            self.move_cursor((row, len(self._line(row)))); self.set_mode("insert")
        elif ch == "o":
            self.move_cursor((row, len(self._line(row)))); self.insert("\n"); self.set_mode("insert")
        elif ch == "O":
            self.move_cursor((row, 0)); self.insert("\n"); self.move_cursor((row, 0)); self.set_mode("insert")
        elif ch == "x":
            line = self._line(row)
            if line:
                end = min(len(line), col + count)
                self._yank(line[col:end], False); self.delete((row, col), (row, end))
                self.last_change = ("x", count)
        elif ch == "D":
            self._yank(self._line(row)[col:], False); self.delete((row, col), (row, len(self._line(row))))
        elif ch == "C":
            self.delete((row, col), (row, len(self._line(row)))); self.set_mode("insert")
        elif ch == "J":
            for _ in range(count):
                if row + 1 < self.document.line_count:
                    end = len(self._line(row))
                    nxt = self._line(row + 1).lstrip()
                    self.replace(" " + nxt if nxt else "", (row, end), (row + 1, len(self._line(row + 1))))
        elif ch == "p" or ch == "P":
            self._put(after=ch == "p")
        elif ch == "u":
            self.undo()
        elif key == "ctrl+r":
            self.redo()
        elif ch == "v":
            self.anchor = self.cursor_location; self.set_mode("visual")
        elif ch == "V":
            self.anchor = self.cursor_location; self.set_mode("vline"); self._select_lines()
        elif ch == ".":
            self._repeat()
        elif ch == ":":
            self.cmdline = ":"
        elif ch == "/":
            self.cmdline = "/"
        elif ch == "n" and self.last_search:
            self._find(self.last_search)
        elif key == "escape":
            self.post_message(self.Command("leave"))

    def _operator(self, op: str, key: str, ch: str, count: int) -> None:
        row, col = self.cursor_location
        if (op == "d" and ch == "d") or (op == "c" and ch == "c") or (op == "y" and ch == "y") or (op in "><" and ch == op):
            last = min(self.document.line_count - 1, row + count - 1)
            if op in "><":
                for r in range(row, last + 1):
                    if op == ">":
                        self.insert("    ", (r, 0))
                    else:
                        lead = len(self._line(r)) - len(self._line(r).lstrip(" "))
                        self.delete((r, 0), (r, min(4, lead)))
                return
            lines = [self._line(r) for r in range(row, last + 1)]
            self._yank("\n".join(lines) + "\n", True)
            if op == "y":
                return
            if op == "c":
                self.replace("", (row, 0), (last, len(self._line(last)))); self.move_cursor((row, 0)); self.set_mode("insert")
                return
            if last + 1 < self.document.line_count:
                self.delete((row, 0), (last + 1, 0))
            elif row > 0:
                self.delete((row - 1, len(self._line(row - 1))), (last, len(self._line(last))))
            else:
                self.delete((0, 0), (last, len(self._line(last))))
            self.move_cursor((min(row, self.document.line_count - 1), 0))
            self.last_change = ("dd", count)
            return
        if ch == "w" and op == "c":
            key = "e_incl"
        target = self._motion("e" if key == "e_incl" else key, count)
        if target is None:
            return
        if key == "w" and target[0] > row:                  # like vim: dw on the last word stops at the line end
            target = (row, len(self._line(row)))
        start, end = sorted([(row, col), target])
        if key in ("e", "e_incl", "dollar_sign", "l"):
            end = (end[0], end[1] + 1)
        text = self.get_text_range(start, end)
        self._yank(text, False)
        if op == "y":
            return
        self.delete(start, end)
        self.move_cursor(start)
        if op == "c":
            self.set_mode("insert")
        else:
            self.last_change = ("d", key, count)

    def _repeat(self) -> None:
        change = self.last_change
        if not change:
            return
        if change[0] == "x":
            self.pending = ""; row, col = self.cursor_location; line = self._line(row)
            end = min(len(line), col + change[1]); self.delete((row, col), (row, end))
        elif change[0] == "dd":
            self._operator("d", "d", "d", change[1])
        elif change[0] == "d":
            self._operator("d", change[1], "", change[2])

    # ── VISUAL ─────────────────────────────────────────────────────────

    def _select_lines(self) -> None:
        a, c = self.anchor, self.cursor_location
        top, bottom = min(a[0], c[0]), max(a[0], c[0])
        sel = type(self.selection)
        self.selection = sel((top, 0), (bottom, len(self._line(bottom))))

    def _visual_key(self, event: events.Key) -> None:
        key, ch = event.key, event.character or ""
        if key == "escape" or ch in ("v", "V") and {"v": "visual", "V": "vline"}[ch] == self.mode:
            self.set_mode("normal")
            return
        target = self._motion(key, 1, self.selection.end if self.mode == "visual" else self.cursor_location)
        if key == "G" or (ch == "g"):
            target = (self.document.line_count - 1, 0) if key == "G" else (0, 0)
        if target is not None:
            if self.mode == "vline":
                self.move_cursor(target); self._select_lines()
            else:
                sel = type(self.selection)
                self.selection = sel(self.anchor, target)
            return
        start, end = sorted([self.selection.start, self.selection.end])
        if self.mode == "visual":
            end = (end[0], end[1] + 1)
        text = self.get_text_range(start, end)
        if ch == "y":
            self._yank(text + ("\n" if self.mode == "vline" else ""), self.mode == "vline")
            self.move_cursor(start); self.set_mode("normal")
        elif ch in ("d", "x", "c"):
            self._yank(text + ("\n" if self.mode == "vline" else ""), self.mode == "vline")
            if self.mode == "vline" and end[0] + 1 < self.document.line_count and ch != "c":
                end = (end[0] + 1, 0)
            self.delete(start, end); self.move_cursor(start)
            self.set_mode("insert" if ch == "c" else "normal")
        elif ch in (">", "<"):
            for r in range(start[0], end[0] + 1):
                if ch == ">":
                    self.insert("    ", (r, 0))
                else:
                    lead = len(self._line(r)) - len(self._line(r).lstrip(" "))
                    self.delete((r, 0), (r, min(4, lead)))
            self.set_mode("normal")

    # ── registers, : and / ─────────────────────────────────────────────

    def _yank(self, text: str, linewise: bool) -> None:
        self.register = (text, linewise)
        try:
            self.app.copy_to_clipboard(text)
        except Exception:
            pass

    def _put(self, after: bool) -> None:
        text, linewise = self.register
        if not text:
            return
        row, col = self.cursor_location
        if linewise:
            if after:
                end = len(self._line(row))
                self.insert("\n" + text.rstrip("\n"), (row, end)); self.move_cursor((row + 1, 0))
            else:
                self.insert(text, (row, 0)); self.move_cursor((row, 0))
        else:
            at = (row, min(len(self._line(row)), col + 1)) if after else (row, col)
            self.insert(text, at)

    def _cmdline_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.cmdline = None
        elif event.key == "enter":
            line, self.cmdline = self.cmdline, None
            if line.startswith("/"):
                self.last_search = line[1:]
                self._find(self.last_search)
            else:
                self.post_message(self.Command(line[1:].strip()))
        elif event.key == "backspace":
            self.cmdline = self.cmdline[:-1] or None
        elif event.character and event.character.isprintable():
            self.cmdline += event.character

    def _find(self, needle: str) -> None:
        if not needle:
            return
        row, col = self.cursor_location
        lines = self.text.split("\n")
        order = list(range(row, len(lines))) + list(range(0, row))
        for r in order:
            start = col + 1 if r == row else 0
            i = lines[r].lower().find(needle.lower(), start)
            if i >= 0:
                self.move_cursor((r, i))
                return
        self.app.notify(f"not found: {needle}", timeout=2)
