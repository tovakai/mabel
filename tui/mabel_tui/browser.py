"""A file/project browser for CREATIVE's apps (PIXEL, TRACKER): ctrl+o, or :e without a name.

Lists the app's folder (pixel_dir / tracker_dir): folders first (a folder is a project), then
files newest first, with a preview on the right that the app provides. Keys: j/k move ·
enter open (or go into a folder) · h/backspace up · n new · m new folder · r rename ·
c duplicate · D delete (moves it to .trash, so nothing is lost) · / filter · s sort by name/date ·
esc close. An app can list extra entries (PIXEL: Mabel's avatar).

When the app has unsaved changes, opening something asks first: s save, then open · d drop them.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Static

from .palette import P

TRASH = ".trash"


@dataclass
class Entry:
    path: Path
    label: str
    kind: str                      # dir | file | other (listed, can't be opened) | extra (outside the folder)
    mtime: float = 0.0


@dataclass
class BrowserSpec:
    """What an app tells the browser."""
    title: str
    root: Path
    suffixes: tuple[str, ...]                          # files the app opens
    also_show: tuple[str, ...] = ()                    # listed, dimmed (exports)
    is_document_dir: Callable[[Path], bool] = lambda p: False      # a folder that opens as one document
    preview: Callable[[Path, int, int], Text] | None = None
    extras: Callable[[], list[Entry]] = lambda: []
    new_suffix: str = ""
    dirty: bool = False                                # the app has unsaved changes
    current: Path | None = None
    renamed: Callable[[Path, Path], None] = lambda old, new: None    # the open file was renamed


@dataclass
class Choice:
    """What the browser hands back: open ``path`` (or make it, if ``new``), saving the current work first?"""
    path: Path
    new: bool = False
    save_first: bool = False


def _ago(mtime: float) -> str:
    s = time.time() - mtime
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")


class FileList(Widget, can_focus=True):
    def __init__(self, browser: "Browser", **kw):
        super().__init__(**kw)
        self.b = browser

    def render(self) -> Text:
        return self.b.draw_list(self.size.height, self.size.width)

    async def _on_key(self, event: events.Key) -> None:
        if self.b.key(event):
            event.stop()
            event.prevent_default()


class Preview(Widget):
    def __init__(self, browser: "Browser", **kw):
        super().__init__(**kw)
        self.b = browser

    def render(self) -> Text:
        return self.b.draw_preview(self.size.width, self.size.height)


class Browser(ModalScreen):
    """Returns a Choice, or None when closed."""

    BINDINGS = [Binding("escape", "close", show=False)]

    def __init__(self, spec: BrowserSpec):
        super().__init__()
        self.spec = spec
        self.dir = spec.root
        here = spec.current.parent if spec.current else None
        if here and here.is_dir() and here.is_relative_to(spec.root):
            self.dir = here                                    # open where the current work lives
        self.entries: list[Entry] = []
        self.index = 0
        self.top = 0
        self.by_name = False
        self.filter = ""
        self.prompt: tuple[str, Callable[[str], None]] | None = None
        self.confirm: tuple[str, Path, bool] | None = None    # ("delete" | "dirty", path, is it new)
        self.message = ""
        self._previews: dict[tuple, Text] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="browser"):
            with Horizontal(id="browser-main"):
                yield FileList(self, id="browser-list")
                yield Preview(self, id="browser-preview")
            yield Static(id="browser-help")
            yield Input(id="browser-input")

    def on_mount(self) -> None:
        self.spec.root.mkdir(parents=True, exist_ok=True)
        box = self.query_one("#browser")
        box.border_title = self.spec.title
        self.query_one("#browser-input").display = False
        self.reload()
        if self.spec.current:
            for i, e in enumerate(self.entries):
                if e.path == self.spec.current:
                    self.index = i
        self.query_one(FileList).focus()
        self._redraw()

    # --- listing ----------------------------------------------------------------------------------

    def reload(self) -> None:
        entries: list[Entry] = []
        if self.dir != self.spec.root:
            entries.append(Entry(self.dir.parent, "..", "up"))
        else:
            entries += self.spec.extras()
        dirs, files = [], []
        try:
            children = list(self.dir.iterdir())
        except OSError:
            children = []
        for p in children:
            if p.name.startswith("."):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if self.filter and self.filter.lower() not in p.name.lower():
                continue
            if p.is_dir():
                kind = "file" if self.spec.is_document_dir(p) else "dir"
                (files if kind == "file" else dirs).append(Entry(p, p.name + ("" if kind == "file" else "/"), kind, mtime))
            elif p.suffix.lower() in self.spec.suffixes:
                files.append(Entry(p, p.name, "file", mtime))
            elif p.suffix.lower() in self.spec.also_show:
                files.append(Entry(p, p.name, "other", mtime))
        key = (lambda e: e.label.lower()) if self.by_name else (lambda e: -e.mtime)
        files.sort(key=key)
        files.sort(key=lambda e: e.kind == "other")           # exports below what can be opened
        self.entries = entries + sorted(dirs, key=key) + files
        self.index = max(0, min(self.index, len(self.entries) - 1))

    @property
    def entry(self) -> Entry | None:
        return self.entries[self.index] if self.entries else None

    # --- drawing ----------------------------------------------------------------------------------

    def draw_list(self, height: int, width: int) -> Text:
        t = Text()
        rel = self.dir.relative_to(self.spec.root) if self.dir != self.spec.root else Path(".")
        t.append(f" {self.spec.root.name}/{'' if str(rel) == '.' else str(rel) + '/'}", style=f"bold {P.beige}")
        if self.filter:
            t.append(f"  filter: {self.filter}", style=P.sand)
        t.append("\n")
        rows = max(1, height - 1)
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + rows:
            self.top = self.index - rows + 1
        if not self.entries:
            t.append("  (empty: n makes a new one)", style=P.amber_dim)
        for i in range(self.top, min(len(self.entries), self.top + rows)):
            e = self.entries[i]
            icon = {"dir": "▸ ", "up": "◂ ", "file": "◆ ", "other": "· ", "extra": "★ "}[e.kind]
            when = _ago(e.mtime) if e.mtime else ""
            name = e.label[: max(8, width - len(when) - 6)]
            style = {"dir": P.amber_hi, "up": P.amber_dim, "file": P.amber, "other": P.amber_dim,
                     "extra": P.teal}[e.kind]
            if i == self.index:
                style = f"bold {P.bg} on {P.amber}"
            if self.spec.current and e.path == self.spec.current and i != self.index:
                style += " underline"
            line = f"{icon}{name}"
            t.append(line.ljust(max(0, width - len(when) - 1)), style=style)
            t.append(f"{when}\n", style=P.amber_dim if i != self.index else style)
        return t

    def draw_preview(self, width: int, height: int) -> Text:
        e = self.entry
        if e is None or e.kind in ("up",):
            return Text("")
        if e.kind == "dir":
            try:
                n = sum(1 for p in e.path.iterdir() if not p.name.startswith("."))
            except OSError:
                n = 0
            return Text(f"\n project folder\n {n} item{'s' if n != 1 else ''}\n\n enter opens it", style=P.amber)
        if e.kind == "other":
            size = e.path.stat().st_size if e.path.exists() else 0
            return Text(f"\n an export ({e.path.suffix})\n {size / 1024:.0f} KB\n\n not opened here", style=P.amber_dim)
        key = (e.path, e.mtime, width, height)
        if key not in self._previews:
            try:
                self._previews[key] = self.spec.preview(e.path, width, height) if self.spec.preview else Text("")
            except Exception as exc:
                self._previews[key] = Text(f"\n can't preview: {exc}", style=P.red)
        return self._previews[key]

    def _redraw(self) -> None:
        self.query_one(FileList).refresh()
        self.query_one(Preview).refresh()
        help_ = Text()
        if self.confirm and self.confirm[0] == "delete":
            help_.append(f" delete {self.confirm[1].name}? D again moves it to .trash · esc keeps it", style=f"bold {P.red}")
        elif self.confirm and self.confirm[0] == "dirty":
            help_.append(f" unsaved changes! s save them, then open · d drop them · esc stay", style=f"bold {P.red}")
        elif self.message:
            help_.append(f" {self.message}", style=f"bold {P.sand}")
        else:
            help_.append(" enter open · h up · n new · m folder · r rename · c duplicate · D delete · / filter · "
                         f"s sort ({'name' if self.by_name else 'newest'}) · esc", style=P.amber_dim)
        self.query_one("#browser-help", Static).update(help_)

    # --- keys -------------------------------------------------------------------------------------

    def key(self, event: events.Key) -> bool:
        k = event.key
        self.message = ""
        if self.confirm:
            kind, path, new = self.confirm
            if kind == "delete" and k == "D":
                self._trash(path)
            elif kind == "dirty" and k in ("s", "d"):
                self.dismiss(Choice(path, new=new, save_first=k == "s"))
                return True
            self.confirm = None
            self._redraw()
            return True
        if k in ("down", "j"):
            self.index = min(len(self.entries) - 1, self.index + 1)
        elif k in ("up", "k"):
            self.index = max(0, self.index - 1)
        elif k in ("pagedown", "pageup"):
            self.index = max(0, min(len(self.entries) - 1, self.index + (10 if k == "pagedown" else -10)))
        elif k in ("home", "g"):
            self.index = 0
        elif k in ("end", "G"):
            self.index = len(self.entries) - 1
        elif k in ("enter", "l", "right"):
            self.open_entry(go_in=k in ("l", "right"))
        elif k in ("h", "left", "backspace"):
            self.go_up()
        elif k == "n":
            self.ask(f"new {self.spec.new_suffix or 'file'} name", self._new)
        elif k == "m":
            self.ask("new folder name", self._mkdir)
        elif k == "r" and self.entry and self.entry.kind in ("file", "dir", "other"):
            self.ask("rename to", self._rename, self.entry.path.name)
        elif k == "c" and self.entry and self.entry.kind in ("file", "other"):
            self._duplicate(self.entry.path)
        elif k == "D" and self.entry and self.entry.kind in ("file", "dir", "other"):
            self.confirm = ("delete", self.entry.path, False)
        elif k == "slash" or event.character == "/":
            self.ask("filter (empty clears)", self._filter, self.filter)
        elif k == "s":
            self.by_name = not self.by_name
            self.reload()
        else:
            return False
        self._redraw()
        return True

    def action_close(self) -> None:
        if self.prompt:
            self._close_prompt()
        elif self.confirm:
            self.confirm = None
            self._redraw()
        else:
            self.dismiss(None)

    def open_entry(self, go_in: bool = False) -> None:
        e = self.entry
        if e is None:
            return
        if e.kind in ("dir", "up") or (go_in and e.kind == "file" and e.path.is_dir()):
            self.dir = e.path
            self.index, self.top = 0, 0
            self.reload()
        elif e.kind == "other":
            self.message = "that's an export: open the song or sprite it came from"
        elif self.spec.dirty and e.path != self.spec.current:
            self.confirm = ("dirty", e.path, False)
        else:
            self.dismiss(Choice(e.path))

    def go_up(self) -> None:
        if self.dir != self.spec.root:
            came_from = self.dir
            self.dir = self.dir.parent
            self.reload()
            self.index = next((i for i, e in enumerate(self.entries) if e.path == came_from), 0)

    # --- prompts and file operations ---------------------------------------------------------------

    def ask(self, label: str, then: Callable[[str], None], value: str = "") -> None:
        box = self.query_one("#browser-input", Input)
        box.placeholder, box.value, box.display = label, value, True
        self.prompt = (label, then)
        box.focus()

    def _close_prompt(self) -> None:
        self.prompt = None
        self.query_one("#browser-input").display = False
        self.query_one(FileList).focus()
        self._redraw()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        then = self.prompt[1] if self.prompt else None
        self._close_prompt()
        if then:
            try:
                then(event.value.strip())
            except OSError as exc:
                self.message = f"✖ {exc}"
            self._redraw()

    def _safe(self, name: str) -> str:
        name = name.replace("/", "-").strip()
        if not name or name in (".", ".."):
            raise OSError("give it a name")
        return name

    def _new(self, name: str) -> None:
        name = self._safe(name)
        path = self.dir / name
        if self.spec.new_suffix and not path.suffix:
            path = path.with_suffix(self.spec.new_suffix)
        if path.exists():
            raise OSError(f"{path.name} already exists")
        if self.spec.dirty:
            self.confirm = ("dirty", path, True)
            return
        self.dismiss(Choice(path, new=True))

    def _mkdir(self, name: str) -> None:
        path = self.dir / self._safe(name)
        path.mkdir(parents=True, exist_ok=False)
        self.reload()
        self.index = next((i for i, e in enumerate(self.entries) if e.path == path), 0)
        self.message = f"folder {path.name}"

    def _rename(self, name: str) -> None:
        old = self.entry.path
        new = old.with_name(self._safe(name))
        if old.is_file() and not new.suffix:
            new = new.with_suffix(old.suffix)
        if new.exists():
            raise OSError(f"{new.name} already exists")
        old.rename(new)
        current = self.spec.current
        if current == old or (current and old.is_dir() and current.is_relative_to(old)):
            moved = new if current == old else new / current.relative_to(old)
            self.spec.current = moved
            self.spec.renamed(current, moved)          # the open file keeps saving to the right place
        self.reload()
        self.index = next((i for i, e in enumerate(self.entries) if e.path == new), 0)
        self.message = f"renamed to {new.name}"

    def _duplicate(self, path: Path) -> None:
        stem, n = path.stem, 2
        while (copy_ := path.with_name(f"{stem} {n}{path.suffix}")).exists():
            n += 1
        (shutil.copytree if path.is_dir() else shutil.copy2)(path, copy_)
        self.reload()
        self.index = next((i for i, e in enumerate(self.entries) if e.path == copy_), self.index)
        self.message = f"copied to {copy_.name}"

    def _trash(self, path: Path) -> None:
        trash = self.spec.root / TRASH
        trash.mkdir(exist_ok=True)
        target = trash / f"{datetime.now():%Y%m%d-%H%M%S} {path.name}"
        shutil.move(str(path), str(target))
        self.reload()
        self.message = f"{path.name} moved to {TRASH}/"

    def _filter(self, text: str) -> None:
        self.filter = text
        self.index = 0
        self.reload()
