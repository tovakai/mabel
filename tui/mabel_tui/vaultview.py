"""VAULT: his Obsidian vault in MABEL-OS (a basalt-style browser/editor, our own code).

Left: explorer (folders drill down; / searches contents; # tags; r recent). Centre: the note,
rendered (wiki links, callouts, tags, properties) or in the vim editor (i / e). Right: outline,
backlinks, the note's tags. Keys: o quick open · f follow a link · D today's daily note ·
n new note · R rename · V switch vault (core of truth / Mabel's journal) · M ask Mabel about
this note · backspace back.
"""
from __future__ import annotations

import re
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from .inventory import Ask, Tile, Zoom
from .nav import mark
from .palette import P
from .vault import WIKILINK, INLINE_TAG, Note, Vault, split_frontmatter
from .vimarea import VimArea

CALLOUT = re.compile(r"^(>\s*)\[!(\w+)\]([+-]?)\s*(.*)$")
ICONS = {"note": "✎", "info": "ℹ", "tip": "✦", "todo": "☐", "warning": "⚠", "danger": "⚡", "bug": "✖",
         "example": "▤", "quote": "❝", "success": "✓", "question": "?", "abstract": "≡", "failure": "✖"}


def _link(mm) -> str:
    bang, target, head, alias = mm.group(1), mm.group(2), mm.group(3) or "", mm.group(4)
    label = alias or (target + head if target else head.lstrip("#"))
    href = "wiki:" + (target + head).replace(" ", "%20")
    return f"🖼 [{label}]({href})" if bang else f"[{label}]({href})"


def obsidian_to_markdown(text: str) -> str:
    """Obsidian's extras as plain Markdown the widget can show (links stay clickable)."""
    fm, body, _ = split_frontmatter(text)
    out, fenced = [], False
    if fm:
        def value(v):
            text = ", ".join(map(str, v)) if isinstance(v, list) else str(v)
            return text if len(text) <= 140 else text[:139] + "…"
        props = [f"**{k}**: {value(v)}" for k, v in fm.items() if not isinstance(v, dict)][:14]
        out.append("> " + "  \n> ".join(WIKILINK.sub(_link, p) for p in props) + "\n")
    for line in body.splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        m = CALLOUT.match(line)
        if m:
            kind = m.group(2).lower()
            line = f"{m.group(1)}**{ICONS.get(kind, '◆')} {m.group(4) or kind.upper()}**"

        line = WIKILINK.sub(_link, line)
        line = INLINE_TAG.sub(lambda t: f"`#{t.group(1)}`", line)
        line = re.sub(r"==([^=]+)==", r"**\1**", line)
        out.append(line)
    return "\n".join(out)


class Picker(ModalScreen):
    """Fuzzy pick from a list (quick open, [[ links, links in a note). Returns the chosen id or None."""
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel"), Binding("down", "down", show=False),
                Binding("up", "up", show=False)]

    def __init__(self, title: str, source, placeholder: str = "type to filter"):
        super().__init__()
        self.title_text, self.source, self.placeholder = title, source, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="picker", classes="panel") as box:
            box.border_title = self.title_text.upper()
            box.border_subtitle = "enter picks · esc cancels"
            yield Input(placeholder=self.placeholder, id="picker-q")
            yield OptionList(id="picker-list")

    def on_mount(self) -> None:
        self.fill("")
        self.query_one("#picker-q", Input).focus()

    def fill(self, query: str) -> None:
        opts = self.query_one("#picker-list", OptionList)
        opts.clear_options()
        opts.add_options([Option(label, id=key) for key, label in self.source(query)[:60]])
        if opts.option_count:
            opts.highlighted = 0

    @on(Input.Changed, "#picker-q")
    def changed(self, event: Input.Changed) -> None:
        self.fill(event.value)

    @on(Input.Submitted, "#picker-q")
    def submitted(self) -> None:
        opts = self.query_one("#picker-list", OptionList)
        if opts.highlighted is not None and opts.option_count:
            self.dismiss(opts.get_option_at_index(opts.highlighted).id)

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_down(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#picker-list", OptionList).action_cursor_up()


class VaultView(Horizontal):
    BINDINGS = [
        Binding("o", "quick_open", "open"),
        Binding("slash", "search", "search"),
        Binding("number_sign", "tags", "tags"),
        Binding("r", "recent", "recent", show=False),
        Binding("f", "follow", "follow link"),
        Binding("e", "edit", "edit"),
        Binding("i", "edit", "edit", show=False),
        Binding("O", "toggle_side", "outline", show=False),
        Binding("D", "daily", "daily note"),
        Binding("n", "new_note", "new", show=False),
        Binding("R", "rename", "rename", show=False),
        Binding("V", "switch_vault", "vault", show=False),
        Binding("M", "ask_mabel", "ask Mabel", show=False),
        Binding("backspace", "back", "back"),
        Binding("ctrl+s", "save", "save", show=False, priority=True),
    ]

    def __init__(self, **kw):
        super().__init__(id="vault-view", **kw)
        self.vaults: list[tuple[str, Path]] = []
        self.vault: Vault | None = None
        self.vault_name = ""
        self.note: Note | None = None
        self.folder = ""                 # explorer: current folder (vault-relative)
        self.explorer_mode = "files"     # files | search | tags | tag | recent
        self.explorer_arg = ""
        self.history: list[str] = []
        self.dirty = False

    def compose(self) -> ComposeResult:
        explorer = mark(VerticalScroll(id="vault-explorer", classes="panel"),
                        primary=lambda: self._focus_in("#vault-explorer"))
        explorer.border_title = "VAULT"
        yield explorer
        with Vertical(id="vault-note", classes="panel section") as note:
            note.border_title = "NOTE"
            note.section_primary = self._enter_note
            yield Static(id="vault-meta")
            with ContentSwitcher(initial="vault-read", id="vault-switch"):
                yield VerticalScroll(Markdown(id="vault-md", open_links=False), id="vault-read")
                yield VimArea(id="vault-edit")
            yield Static(id="vault-status")
        side = mark(VerticalScroll(id="vault-side", classes="panel"), primary=lambda: self._focus_in("#vault-side"))
        side.border_title = "OUTLINE · LINKS"
        yield side

    def on_mount(self) -> None:
        cfg = self.app.cfg
        self.vaults = [("core of truth", Path(cfg.vault).expanduser())]
        journal = Path(getattr(cfg, "journal", "") or "~/Documents/mabel/journal").expanduser()
        if journal.is_dir():
            self.vaults.append(("Mabel's journal", journal))
        self.load_vault(0)

    # ── loading ─────────────────────────────────────────────────────────

    @work(exclusive=True, group="vault-load", thread=True)
    def load_vault(self, index: int, then: str | None = None) -> None:
        name, root = self.vaults[index % len(self.vaults)]
        vault = Vault(root)
        if not vault.exists:
            self.app.call_from_thread(self._show_missing, name, root)
            return
        vault.scan()
        self.app.call_from_thread(self._vault_ready, vault, name, then)

    def _show_missing(self, name: str, root: Path) -> None:
        self.query_one("#vault-meta", Static).update(Text(f"✖ {name} isn't at {root}", style=P.red))

    async def _vault_ready(self, vault: Vault, name: str, then: str | None) -> None:
        self.vault, self.vault_name = vault, name
        self.folder, self.explorer_mode, self.history = "", "files", []
        self.query_one("#vault-explorer").border_title = f"VAULT ░ {name.upper()}"
        await self.render_explorer()
        if then:
            await self.open_note(then)
        elif not self.note:
            self.query_one("#vault-meta", Static).update(Text(
                f"{len(vault.notes)} notes · o open · / search · # tags · D today's note", style=P.beige_dark))

    # ── explorer ────────────────────────────────────────────────────────

    async def render_explorer(self) -> None:
        box, v = self.query_one("#vault-explorer", VerticalScroll), self.vault
        if v is None:
            return
        await box.remove_children()
        tiles: list = []
        if self.explorer_mode == "files":
            prefix = self.folder + "/" if self.folder else ""
            folders, notes = set(), []
            for rel, note in v.notes.items():
                if not rel.startswith(prefix):
                    continue
                rest = rel[len(prefix):]
                if "/" in rest:
                    folders.add(rest.split("/", 1)[0])
                else:
                    notes.append(note)
            head = Text(("▸ " + self.folder) if self.folder else "▸ /", style=f"bold {P.beige}")
            tiles.append(Static(head, classes="sec"))
            if self.folder:
                tiles.append(Tile(Text("◂ ..", style=P.amber_dim), route="dir:" + self.folder.rsplit("/", 1)[0]
                                  if "/" in self.folder else "dir:", classes="thing"))
            for f in sorted(folders, key=str.lower):
                count = sum(1 for r in v.notes if r.startswith(prefix + f + "/"))
                tiles.append(Tile(Text.assemble(("▤ ", P.amber_dim), (f, P.amber_hi), (f"  {count}", P.beige_dark)),
                                  route="dir:" + prefix + f, classes="thing"))
            for n in sorted(notes, key=lambda n: n.name.lower()):
                tiles.append(self._note_tile(n))
        elif self.explorer_mode == "search":
            hits = v.search(self.explorer_arg)
            tiles.append(Static(Text(f"“{self.explorer_arg}” · {len(hits)} lines", style=f"bold {P.beige}"), classes="sec"))
            for note, line, text in hits:
                t = Text.assemble((note.name, P.amber_hi), (f" :{line + 1}\n", P.beige_dark), (text[:80], P.amber))
                tiles.append(Tile(t, route=f"note:{note.rel}:{line}", classes="thing"))
        elif self.explorer_mode == "tags":
            tiles.append(Static(Text("TAGS", style=f"bold {P.beige}"), classes="sec"))
            for tag, notes in v.tags().items():
                tiles.append(Tile(Text.assemble((f"#{tag}", P.teal), (f"  {len(notes)}", P.beige_dark)),
                                  route="tag:" + tag, classes="thing"))
        elif self.explorer_mode == "tag":
            notes = v.tags().get(self.explorer_arg, [])
            tiles.append(Static(Text(f"#{self.explorer_arg} · {len(notes)}", style=f"bold {P.teal}"), classes="sec"))
            tiles += [self._note_tile(n, path=True) for n in sorted(notes, key=lambda n: n.name.lower())]
        elif self.explorer_mode == "recent":
            tiles.append(Static(Text("RECENT", style=f"bold {P.beige}"), classes="sec"))
            tiles += [self._note_tile(n, path=True) for n in v.find_notes("", 40)]
        await box.mount_all(tiles)

    def _note_tile(self, note: Note, path: bool = False) -> Tile:
        t = Text.assemble(("• " if note is not self.note else "▶ ", P.amber_dim), (note.name, P.amber))
        if path and "/" in note.rel:
            t.append(f"  {note.rel.rsplit('/', 1)[0]}", style=P.beige_dark)
        return Tile(t, route="note:" + note.rel, classes="thing")

    def _focus_in(self, selector: str) -> None:
        tiles = self.query_one(selector).query(Tile)
        (tiles.first() if tiles else self.query_one(selector)).focus()

    @on(Zoom)
    async def zoom(self, event: Zoom) -> None:
        event.stop()
        kind, _, arg = event.route.partition(":")
        if kind == "dir":
            self.explorer_mode, self.folder = "files", arg
            await self.render_explorer()
            self._focus_in("#vault-explorer")
        elif kind == "tag":
            self.explorer_mode, self.explorer_arg = "tag", arg
            await self.render_explorer()
            self._focus_in("#vault-explorer")
        elif kind == "note":
            rel, _, line = arg.rpartition(":") if re.search(r":\d+$", arg) else (arg, "", "")
            await self.open_note(rel or arg, int(line) if line else None)
        elif kind == "head":
            self._scroll_to_heading(int(arg))

    # ── the note ────────────────────────────────────────────────────────

    async def open_note(self, rel: str, line: int | None = None, remember: bool = True) -> None:
        v = self.vault
        if v is None:
            return
        note = v.notes.get(rel) or v.resolve(rel)
        if note is None:
            self.app.notify(f"No note “{rel}” in {self.vault_name}", severity="warning")
            return
        if self.dirty:
            self.action_save()
        if self._editing():
            self._leave_editor()
        if remember and self.note is not None and self.note is not note:
            self.history.append(self.note.rel)
        self.note = note
        text = v.read(note)
        self.query_one("#vault-note").border_title = f"NOTE ░ {note.name.upper()[:44]}"
        meta = Text.assemble((note.rel, P.beige_dark))
        if note.tags:
            meta.append("   " + " ".join("#" + t for t in sorted(note.tags)), style=P.teal)
        self.query_one("#vault-meta", Static).update(meta)
        await self.query_one("#vault-md", Markdown).update(obsidian_to_markdown(text))
        self.query_one("#vault-status", Static).update(Text("read · i/e edit · f follow a link · M ask Mabel", style=P.amber_faint))
        await self.render_side()
        await self.render_explorer()
        if line is not None:
            self.call_after_refresh(lambda: self._scroll_to_line(line, text))

    def _scroll_to_line(self, line: int, text: str) -> None:
        total = max(1, text.count("\n") + 1)
        read = self.query_one("#vault-read", VerticalScroll)
        read.scroll_to(y=read.max_scroll_y * line / total, animate=False)

    def _scroll_to_heading(self, line: int) -> None:
        if self.note and self.vault:
            if self._editing():
                self.query_one(VimArea).move_cursor((line, 0))
            else:
                self._scroll_to_line(line, self.vault.read(self.note))

    async def render_side(self) -> None:
        side, v, note = self.query_one("#vault-side", VerticalScroll), self.vault, self.note
        await side.remove_children()
        if not note or not v:
            return
        tiles: list = [Static(Text("OUTLINE", style=f"bold {P.beige}"), classes="sec")]
        for level, text, line in note.headings:
            tiles.append(Tile(Text("  " * (level - 1) + text, style=P.amber_hi if level <= 2 else P.amber),
                              route=f"head:{line}", classes="thing"))
        if not note.headings:
            tiles.append(Static(Text("no headings", style=P.amber_faint)))
        back = v.backlinks(note)
        tiles.append(Static(Text(f"\nBACKLINKS · {len(back)}", style=f"bold {P.beige}"), classes="sec"))
        tiles += [self._note_tile(n, path=True) for n in back[:60]]
        out = [l for l in dict.fromkeys(note.links)]
        tiles.append(Static(Text(f"\nLINKS · {len(out)}", style=f"bold {P.beige}"), classes="sec"))
        for target in out[:60]:
            found = v.resolve(target, note)
            tiles.append(Tile(Text(target, style=P.amber if found else P.amber_faint),
                              route="note:" + (found.rel if found else target), classes="thing"))
        if note.tags:
            tiles.append(Static(Text("\nTAGS", style=f"bold {P.beige}"), classes="sec"))
            tiles += [Tile(Text("#" + t, style=P.teal), route="tag:" + t, classes="thing") for t in sorted(note.tags)]
        await side.mount_all(tiles)

    @on(Markdown.LinkClicked)
    async def link_clicked(self, event: Markdown.LinkClicked) -> None:
        href = event.href
        if href.startswith("wiki:"):
            target = href[5:].replace("%20", " ")
            name, _, head = target.partition("#")
            await self.open_note(name or (self.note.rel if self.note else ""), self._heading_line(head) if head else None)
        else:
            self.app.notify(f"external link: {href}", timeout=4)

    def _heading_line(self, head: str) -> int | None:
        if not self.note:
            return None
        return next((line for _, text, line in self.note.headings if text.lower() == head.lower()), None)

    def _enter_note(self) -> None:
        if self._editing():
            self.query_one(VimArea).focus()
        else:
            self.query_one("#vault-read").focus()

    # ── editing ─────────────────────────────────────────────────────────

    def _editing(self) -> bool:
        return self.query_one("#vault-switch", ContentSwitcher).current == "vault-edit"

    def action_edit(self) -> None:
        if not self.note or not self.vault:
            return
        area = self.query_one(VimArea)
        area.load_text(self.vault.read(self.note))
        area.set_mode("normal")
        self.query_one("#vault-switch", ContentSwitcher).current = "vault-edit"
        self.dirty = False
        area.focus()
        self._status(area.status)

    @on(VimArea.Changed)
    def changed(self, event) -> None:
        if self._editing() and self.note:
            self.dirty = True

    @on(VimArea.ModeChanged)
    def mode_changed(self, event: VimArea.ModeChanged) -> None:
        self._status(self.query_one(VimArea).status)

    def _status(self, mode: str) -> None:
        self.query_one("#vault-status", Static).update(Text.assemble(
            (mode, f"bold {P.amber_hi}"), ("   ● unsaved" if self.dirty else "   saved", P.orange if self.dirty else P.beige_dark),
            ("   ctrl+s save · esc esc back to reading", P.amber_faint)))

    def action_save(self) -> None:
        if not self._editing() or not self.note or not self.vault:
            return
        self.note = self.vault.write(self.note, self.query_one(VimArea).text)
        self.dirty = False
        self._status(self.query_one(VimArea).status)
        self.app.notify(f"✓ saved {self.note.name}", timeout=2)

    @on(VimArea.Command)
    async def command(self, event: VimArea.Command) -> None:
        cmd = event.command
        if cmd in ("w", "wq", "x"):
            self.action_save()
        if cmd in ("q", "wq", "x", "leave", "q!"):
            if self.dirty and cmd != "q!":
                self.action_save()                      # like Obsidian: never lose typing
            self._leave_editor()
            if self.note:
                await self.open_note(self.note.rel, remember=False)
                self.query_one("#vault-read").focus()

    def _leave_editor(self) -> None:
        self.query_one("#vault-switch", ContentSwitcher).current = "vault-read"
        self.dirty = False

    @on(VimArea.LinkWanted)
    def link_wanted(self) -> None:
        def chosen(rel):
            area = self.query_one(VimArea)
            if rel and self.vault:
                area.insert(self.vault.notes[rel].name + "]]")
            area.focus()
        self.app.push_screen(Picker("Link to", self._note_choices), chosen)

    # ── actions ─────────────────────────────────────────────────────────

    def _note_choices(self, query: str) -> list[tuple[str, str]]:
        notes = self.vault.find_notes(query, 60) if self.vault else []
        return [(n.rel, f"{n.name}   {n.rel.rsplit('/', 1)[0] if '/' in n.rel else ''}") for n in notes]

    def action_quick_open(self) -> None:
        def chosen(rel):
            if rel:
                self.run_worker(self.open_note(rel))
        self.app.push_screen(Picker("Open note", self._note_choices), chosen)

    def action_follow(self) -> None:
        if not self.note or not self.vault:
            return
        links = list(dict.fromkeys(self.note.links))

        def source(query):
            return [(t, t) for t in links if query.lower() in t.lower()]

        def chosen(target):
            if target:
                found = self.vault.resolve(target, self.note)
                self.run_worker(self.open_note(found.rel if found else target))
        self.app.push_screen(Picker(f"Follow a link ({len(links)})", source), chosen)

    def action_search(self) -> None:
        def done(result):
            q = (result or {}).get("q", "").strip()
            if q:
                self.explorer_mode, self.explorer_arg = "search", q
                self.run_worker(self.render_explorer())
        self.app.push_screen(Ask("Search the vault", [("q", "Words (all must be on the line)", "", "line")], ok="Search"), done)

    async def action_tags(self) -> None:
        self.explorer_mode = "tags"
        await self.render_explorer()
        self._focus_in("#vault-explorer")

    async def action_recent(self) -> None:
        self.explorer_mode = "recent"
        await self.render_explorer()
        self._focus_in("#vault-explorer")

    async def action_back(self) -> None:
        if self._editing():
            return
        if self.history:
            await self.open_note(self.history.pop(), remember=False)
        elif self.explorer_mode != "files" or self.folder:
            self.explorer_mode = "files"
            self.folder = self.folder.rsplit("/", 1)[0] if "/" in self.folder else ""
            await self.render_explorer()

    async def action_daily(self) -> None:
        if not self.vault:
            return
        if self.vault_name != "core of truth":
            self.app.notify("Daily notes live in core of truth (V switches vault)", severity="warning")
            return
        note = self.vault.daily()
        await self.open_note(note.rel)

    def action_new_note(self) -> None:
        def done(result):
            name = (result or {}).get("name", "").strip()
            if name and self.vault:
                rel = (self.folder + "/" if self.folder and self.explorer_mode == "files" else "") + name
                note = self.vault.create(rel, f"# {name.rsplit('/', 1)[-1]}\n\n")
                self.run_worker(self._open_and_edit(note.rel))
        where = self.folder if self.explorer_mode == "files" and self.folder else "the vault root"
        self.app.push_screen(Ask(f"New note in {where}", [("name", "Name", "", "line")], ok="Create"), done)

    async def _open_and_edit(self, rel: str) -> None:
        await self.open_note(rel)
        self.action_edit()

    def action_rename(self) -> None:
        if not self.note or not self.vault:
            return

        def done(result):
            name = (result or {}).get("name", "").strip()
            if not name or name == self.note.name:
                return
            try:
                note, changed = self.vault.rename(self.note, name)
            except (ValueError, OSError) as exc:
                self.app.notify(str(exc), severity="error")
                return
            self.app.notify(f"✓ renamed; links updated in {changed} note(s)", timeout=4)
            self.note = None
            self.run_worker(self.open_note(note.rel, remember=False))
        self.app.push_screen(Ask(f"Rename {self.note.name}", [("name", "New name (links are updated)", self.note.name, "line")],
                                 ok="Rename"), done)

    def action_switch_vault(self) -> None:
        if len(self.vaults) < 2:
            self.app.notify("Only one vault here", timeout=2)
            return
        names = [n for n, _ in self.vaults]
        current = names.index(self.vault_name) if self.vault_name in names else 0
        self.note = None
        self.load_vault(current + 1)

    def action_toggle_side(self) -> None:
        self.query_one("#vault-side").toggle_class("-shown" if self.screen.has_class("-narrow") else "-hidden")

    def action_ask_mabel(self) -> None:
        if not self.note or not self.vault:
            return
        self.app.ask_about_note(self.note.name, self.note.rel, self.vault.read(self.note))

    # ── called from other channels (Mabel links) ───────────────────────

    def open_external(self, vault_name: str, rel: str, line: int | None = None) -> None:
        names = [n for n, _ in self.vaults]
        if vault_name in names and vault_name != self.vault_name:
            self.load_vault(names.index(vault_name), then=rel)
        else:
            self.run_worker(self.open_note(rel, line))
