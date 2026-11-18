"""INVENTORY: the Gremlin Index, zoomed like its web UI (home → room → furniture → place → spot).

Left: where you are (breadcrumbs), place cards to zoom into, the place drawn as a map (Gridfinity
grids and pegboards as boxes with their footprints; drawers and shelves as levels; sorted shelves;
spot lists), and the things there. Right: the item card. Writes go to tk-index (undoable there).

Keys: enter zoom/open · backspace up · / search · H home · b Mabel's housekeeping inbox ·
o loose ends · on an item: e edit, m move, c check out, r return, k nickname, p its place ·
u undo · g reload.
"""
from __future__ import annotations

import zlib

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TextArea

from . import housekeeping as hk
from .config import CACHE
from .nav import mark
from .tkindex import KEY, POS, TKIndex, World
from .palette import P

PALETTE = [P.amber, P.amber_hi, P.teal, P.beige, P.orange, P.sand, P.mint]
AWAY = P.red


def hue(category: str | None) -> str:
    return PALETTE[zlib.crc32(str(category or "?").encode()) % len(PALETTE)]


# ── messages ────────────────────────────────────────────────────────────

class Zoom(Message):
    def __init__(self, route: str):
        super().__init__()
        self.route = route


class Pick(Message):
    def __init__(self, item_id: str):
        super().__init__()
        self.item_id = item_id


# ── small focusable pieces ─────────────────────────────────────────────

class Tile(Static):
    """A focusable line or card: enter or a click activates it."""
    can_focus = True
    BINDINGS = [Binding("enter", "activate", "open", show=False),
                Binding("down,right", "app.focus_next", "next", show=False),
                Binding("up,left", "app.focus_previous", "previous", show=False)]

    def __init__(self, content, *, route: str | None = None, item_id: str | None = None, classes: str = ""):
        super().__init__(content, classes=classes)
        self.route, self.item_id = route, item_id

    def action_activate(self) -> None:
        if self.route is not None:
            self.post_message(Zoom(self.route))
        elif self.item_id:
            self.post_message(Pick(self.item_id))

    def on_click(self) -> None:
        self.focus()
        self.action_activate()


def place_card(card) -> Tile:
    text = Text()
    text.append(card.title + "\n", style=f"bold {P.amber_hi}")
    text.append(card.code[:34], style=P.beige_dark)
    if card.count:
        text.append(f"  · {card.count}", style=P.teal)
    return Tile(text, route=card.route, classes="pcard")


def thing_tile(world: World, item: dict, prefix: str = "") -> Tile:
    text = Text()
    away = item.get("status") == "away"
    text.append("▪ ", style=AWAY if away else hue(item.get("category")))
    text.append(prefix, style=P.beige_dark)
    text.append(item.get("name", ""), style=P.amber)
    kids = len(world.kids.get(item["id"], []))
    text.append(f"  #{item['id']}" + (f" · {kids} inside" if kids else ""), style=P.beige_dark)
    if away:
        text.append("  OUT", style=f"bold {AWAY}")
    elif item.get("status") not in (None, "home"):
        text.append(f"  {item.get('status')}", style=P.amber_dim)
    return Tile(text, item_id=item["id"], classes="thing")


class Crumbs(Static):
    """Where you are: each step is clickable (zoom out to it). Keyboard: backspace, or [ to step up."""

    def action_crumb(self, route: str) -> None:
        self.post_message(Zoom(route))


# ── the grid map ───────────────────────────────────────────────────────

def draw_grid(world: World, layout: dict, data: dict, width: int, selected: str | None) -> Text:
    """A Gridfinity grid / pegboard / compartment box as box-drawing, bins at their footprints."""
    C, R = data["cols"], data["rows"]
    cw = max(5, min(14, (width - 6) // max(C, 1)))
    chh = 3 if cw >= 8 else 2
    W, H = C * cw + 1, R * chh + 1
    chars = [[" "] * W for _ in range(H)]
    style = [[P.amber_faint] * W for _ in range(H)]

    def put(x, y, ch, st):
        if 0 <= x < W and 0 <= y < H:
            chars[y][x], style[y][x] = ch, st

    for r in range(R):                                      # empty cells: a faint dot
        for c in range(C):
            put(c * cw + cw // 2, r * chh + chh // 2, "·", P.amber_faint)

    def box(c, r, w, h, label, colour, *, dashed=False, extra=""):
        x0, y0, x1, y1 = (c - 1) * cw, (r - 1) * chh, (c - 1 + w) * cw, (r - 1 + h) * chh
        horiz, vert = ("╌", "╎") if dashed else ("─", "│")
        for x in range(x0 + 1, x1):
            put(x, y0, horiz, colour)
            put(x, y1, horiz, colour)
        for y in range(y0 + 1, y1):
            put(x0, y, vert, colour)
            put(x1, y, vert, colour)
            for x in range(x0 + 1, x1):
                put(x, y, " ", colour)
        for (x, y), ch in {(x0, y0): "┌", (x1, y0): "┐", (x0, y1): "└", (x1, y1): "┘"}.items():
            put(x, y, ch, colour)
        inner_w, inner_h = max(1, x1 - x0 - 1), max(1, y1 - y0 - 1)
        words, lines, line = label.split(), [], ""
        for word in words:
            if len(line) + len(word) + (1 if line else 0) <= inner_w:
                line = (line + " " + word).strip()
            else:
                if line:
                    lines.append(line)
                line = word[:inner_w]
        if line:
            lines.append(line)
        lines = lines[:inner_h]
        if extra and lines and len(lines) == inner_h and len(lines[-1]) + len(extra) + 1 > inner_w:
            lines[-1] = lines[-1][: max(0, inner_w - len(extra) - 1)]
        for i, text in enumerate(lines):
            for j, ch in enumerate(text):
                put(x0 + 1 + j, y0 + 1 + i, ch, colour)
        if extra:
            for j, ch in enumerate(extra):
                put(x1 - len(extra) + j, y1 - 1, ch, P.teal)

    if data["compartments"]:
        for b in data["blocks"]:
            i = b["item"]
            colour = AWAY if i.get("status") == "away" else hue(i.get("category"))
            box(b["c"], b["r"], 1, 1, i.get("name", ""), colour)
    else:
        for b in data["blocks"]:
            i = b["item"]
            colour = AWAY if i.get("status") == "away" else hue(i.get("category"))
            box(b["c"], b["r"], b["w"], b["h"], i.get("name", ""), colour, dashed=b["assumed"] or b["unknown"],
                extra=f"+{b['kids']}" if b["kids"] else "")
    for s in data["subs"]:
        box(s["c"], s["r"], 1, 1, "▦ " + s["layout"].get("label", ""), P.teal)
    if selected:
        for b in data["blocks"]:
            if b["item"]["id"] == selected:
                x0, y0 = (b["c"] - 1) * cw, (b["r"] - 1) * chh
                x1, y1 = (b["c"] - 1 + b["w"]) * cw, (b["r"] - 1 + b["h"]) * chh
                for y in range(y0, y1 + 1):
                    for x in range(x0, x1 + 1):
                        style[y][x] = "reverse bold " + style[y][x]
    out = Text()
    header = "    " + "".join(f"C{c:02d}".ljust(cw) for c in range(1, C + 1))
    out.append(header[: W + 4] + "\n", style=P.beige_dark)
    for y in range(H):
        out.append(f"R{(y // chh) + 1:02d} " if y % chh == chh // 2 and y // chh < R else "    ", style=P.beige_dark)
        run, run_style = "", None
        for x in range(W):
            if style[y][x] != run_style and run:
                out.append(run, style=run_style)
                run = ""
            run_style = style[y][x]
            run += chars[y][x]
        out.append(run + "\n", style=run_style)
    return out


# ── modals ──────────────────────────────────────────────────────────────

class Ask(ModalScreen[dict | None]):
    """A small form: fields (key, label, value, kind) → {key: value} or None."""
    BINDINGS = [Binding("escape", "cancel", "cancel"), Binding("ctrl+s", "save", "save", priority=True)]

    def __init__(self, title: str, fields: list[tuple], ok: str = "Save"):
        super().__init__()
        self.title_text, self.fields, self.ok = title, fields, ok

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="ask", classes="panel") as box:
            box.border_title = self.title_text.upper()
            box.border_subtitle = "ctrl+s save · esc cancel"
            for key, label, value, kind in self.fields:
                yield Label(label, classes="ask-label")
                if kind == "text":
                    yield TextArea(str(value or ""), id=f"f-{key}", classes="ask-area")
                elif isinstance(kind, list):
                    yield Select([(o or "—", o) for o in kind], value=value if value in kind else Select.BLANK,
                                 id=f"f-{key}", allow_blank=True)
                else:
                    yield Input(str(value or ""), id=f"f-{key}")
            with Horizontal(classes="ask-buttons"):
                yield Button(self.ok, id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        widgets = self.query("Input, TextArea, Select")
        if widgets:
            widgets.first().focus()

    def action_save(self) -> None:
        self.save()

    @on(Button.Pressed, "#ok")
    def save(self) -> None:
        out = {}
        for key, _label, _value, kind in self.fields:
            w = self.query_one(f"#f-{key}")
            if isinstance(w, TextArea):
                out[key] = w.text
            elif isinstance(w, Select):
                out[key] = "" if w.value is Select.BLANK else w.value
            else:
                out[key] = w.value
        self.dismiss(out)

    @on(Input.Submitted)
    def submitted(self) -> None:
        if len(self.fields) == 1:
            self.save()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


EDIT_FIELDS = [("name", "Name", None), ("spec_code", "Spec ID", None), ("category", "Category", None),
               ("quantity", "Quantity", None), ("quantity_note", "Quantity note", None),
               ("stage", "Stage", ["", "Placed", "Review", "Bagged", "Needs ID"]),
               ("confidence", "Confidence", ["Certain", "Likely", "Unknown"]),
               ("decision", "Decision", ["", "Keep", "Recycle", "Donate", "Unsure"]),
               ("search_words", "Search words (other words that find it)", None),
               ("sort_key", "Sort title", None), ("owner", "Owner", None),
               ("specs", "Specs", "text"), ("notes", "Notes", "text")]


# ── the channel ─────────────────────────────────────────────────────────

class Inventory(Horizontal):
    BINDINGS = [
        Binding("backspace", "up", "up"),
        Binding("slash", "search", "search"),
        Binding("H", "home", "home"),
        Binding("b", "inbox", "inbox"),
        Binding("o", "loose", "loose ends"),
        Binding("e", "edit", "edit"),
        Binding("m", "move", "move"),
        Binding("c", "checkout", "check out"),
        Binding("r", "return_home", "return"),
        Binding("k", "nickname", "nickname", show=False),
        Binding("p", "goto_place", "its place", show=False),
        Binding("u", "undo", "undo"),
        Binding("g", "reload", "reload", show=False),
        Binding("a", "apply", "apply", show=False),
        Binding("x", "tick", "tick", show=False),
    ]

    def __init__(self, **kw):
        super().__init__(id="inventory-view", **kw)
        self.world: World | None = None
        self.route = ""
        self.mode = "browse"             # browse | search | inbox | loose
        self.selected: str | None = None
        self.item: dict | None = None
        self.suggestions: list[hk.Suggestion] = []
        self._image_key = None

    def compose(self) -> ComposeResult:
        with Vertical(id="inv-main", classes="panel") as main:
            main.border_title = "INVENTORY"
            yield mark(Input(placeholder="/ search the Gremlin Index (names, nicknames, spec IDs, svenska)", id="inv-search"))
            yield Crumbs(id="inv-crumbs")
            yield mark(VerticalScroll(id="inv-body"), primary=lambda: self._focus_first(force=True))
        with VerticalScroll(id="inv-item", classes="panel section") as card:
            card.border_title = "ITEM"
            yield Container(id="inv-photo")
            yield Static(id="inv-card")

    @property
    def idx(self) -> TKIndex:
        return self.app.index

    def on_mount(self) -> None:
        self.query_one("#inv-card", Static).update(Text("Pick something on the left.", style=P.amber_dim))
        self.load_world()

    # ── loading and rendering ───────────────────────────────────────────

    @work(exclusive=True, group="inv-load")
    async def load_world(self, then_item: str | None = None) -> None:
        try:
            self.world = await self.idx.world()
        except Exception as exc:
            self.query_one("#inv-crumbs", Static).update(Text(f"✖ can't reach the Gremlin Index: {exc}", style=AWAY))
            return
        if not self.world.valid(self.route):
            self.route = ""
        await self.render_page()
        if then_item or self.selected:
            await self.show_item(then_item or self.selected)

    async def render_page(self) -> None:
        w, body = self.world, self.query_one("#inv-body", VerticalScroll)
        if w is None:
            return
        focused = self.app.focused
        self._was_inside = focused is not None and body in focused.ancestors_with_self
        await body.remove_children()
        crumbs = self.query_one("#inv-crumbs", Static)
        main = self.query_one("#inv-main")
        if self.mode == "search":
            q = self.query_one("#inv-search", Input).value.strip()
            hits = w.search(q)
            main.border_title = f"INVENTORY ░ SEARCH “{q.upper()[:30]}”"
            crumbs.update(Text(f"{len(hits)} found · esc back to browsing", style=P.beige_dark))
            await body.mount_all(thing_tile(w, i, prefix=f"{w.place_title(i.get('address'))[:28]} · ") for i in hits)
            return
        if self.mode == "inbox":
            await self._render_inbox(body, crumbs, main)
            return
        if self.mode == "loose":
            counts = w.status_counts()
            main.border_title = "INVENTORY ░ LOOSE ENDS"
            crumbs.update(Text(f"{counts.get('away', 0)} away · {counts.get('unplaced', 0)} without a home", style=P.beige_dark))
            await body.mount(Static(Text("AWAY FROM HOME", style=f"bold {P.beige}"), classes="sec"))
            await body.mount_all(thing_tile(w, i, prefix=f"→ {i.get('checked_out_to') or '?'} · ")
                                 for i in w.search("", "away"))
            await body.mount(Static(Text("\nWITHOUT A HOME", style=f"bold {P.beige}"), classes="sec"))
            await body.mount_all(thing_tile(w, i) for i in w.search("", "unplaced"))
            return
        r = self.route
        main.border_title = f"INVENTORY ░ {w.title(r).upper()[:40]}"
        trail = Text()
        for n, step in enumerate(w.chain(r)):
            if n:
                trail.append("  ›  ", style=P.amber_faint)
            if step == r:
                trail.append(w.title(step), style=f"bold {P.amber_hi}")
            else:
                trail.append(w.title(step), style=Style(color=P.amber_dim, underline=True,
                                                        meta={"@click": f"crumb({step!r})"}))
        trail.append(f"     {w.code(r)}", style=P.beige_dark)
        crumbs.update(trail)
        page = w.page(r)
        if not r:
            counts = w.status_counts()
            live = sum(v for k, v in counts.items() if k != "retired")
            await body.mount(Static(Text(f"WHERE IS EVERYTHING?   {live} things · {len(page['cards'])} rooms",
                                         style=f"bold {P.beige}"), classes="sec"))
        if page["cards"]:
            await body.mount(Grid(*[place_card(c) for c in page["cards"]], classes="pcards"))
        if page["inline"]:
            await body.mount(Tile(Text(page["inline"][0].upper(), style=f"bold {P.beige}"), route=page["inline"][1], classes="sec"))
        for zone in page["zones"]:
            await self._render_zone(body, zone, len(page["zones"]) > 1)
        if page["things"]:
            await body.mount(Static(Text("\n" + page["things_head"].upper(), style=f"bold {P.beige}"), classes="sec"))
            await body.mount_all(thing_tile(w, i) for i in page["things"])
        if not r and (w.status_counts().get("away") or w.status_counts().get("unplaced")):
            c = w.status_counts()
            await body.mount(Tile(Text(f"\n◆ LOOSE ENDS   {c.get('away', 0)} away from home · {c.get('unplaced', 0)} without a home",
                                       style=f"bold {P.orange}"), route="!loose", classes="sec loose"))
        if getattr(self, "_was_inside", False):
            self.call_after_refresh(lambda: self._focus_first(force=True))   # stay inside after zooming

    def _focus_first(self, force: bool = False) -> None:
        if not force:
            return                                   # nav mode decides; only i/a/enter focuses inside
        body = self.query_one("#inv-body")
        for selector in (".pcard", ".suggestion", ".level", ".thing", "Tile"):
            found = body.query(selector)
            if found:
                found.first().focus()
                return

    async def _render_zone(self, body, zone, labelled: bool) -> None:
        w, l = self.world, zone.layout
        if labelled:
            await body.mount(Static(Text(f"\n{l.get('label', '')}  {l.get('prefix')}", style=f"bold {P.beige}"), classes="sec"))
        if zone.kind == "grid":
            width = body.size.width or 100
            await body.mount(Static(draw_grid(w, l, zone.data, width, self.selected), classes="map"))
            legend = f"{zone.data['cols']}×{zone.data['rows']} · ╌ size assumed or unknown · +n things inside"
            await body.mount(Static(Text(legend, style=P.beige_dark), classes="legend"))
            for s in zone.data["subs"]:
                await body.mount(Tile(Text(f"▦ {s['layout'].get('label')}  ({POS(s['layout']['parent_address'])})",
                                           style=P.teal), route=s["route"], classes="thing"))
            for b in sorted(zone.data["blocks"], key=lambda b: (b["r"], b["c"])):
                await body.mount(thing_tile(w, b["item"], prefix=f"C{b['c']:02d}R{b['r']:02d} "))
        elif zone.kind == "levels":
            for lv in zone.data["levels"]:
                text = Text()
                text.append(f"{lv['name']:<18}", style=f"bold {P.amber_hi}")
                text.append(f"{lv['where']:<7}", style=P.beige_dark)
                bits = [f"▦ {s.get('label')}" for s in lv["subs"]] + [i.get("name", "") for i in lv["items"]]
                text.append(" · ".join(bits)[:160] or "empty", style=P.amber if bits else P.amber_faint)
                await body.mount(Tile(text, route=lv["route"], classes="level"))
        elif zone.kind == "media":
            items = zone.data["items"]
            await body.mount(Static(Text(f"{len(items)} titles in sort order", style=P.beige_dark)))
            letter = None
            for i in items:
                first = (w.sort_title(i)[:1] or "#").upper()
                first = first if first.isalpha() else "#"
                if first != letter:
                    letter = first
                    await body.mount(Static(Text(f"\n{letter}", style=f"bold {P.teal}")))
                await body.mount(thing_tile(w, i))
        else:
            if zone.data["cards"]:
                await body.mount(Grid(*[place_card(c) for c in zone.data["cards"]], classes="pcards"))
            for spot, items in zone.data["spots"].items():
                label = "anywhere" if spot == "000000" else f"spot {int(spot)}"
                await body.mount(Static(Text(f"\n{label.upper()}", style=f"bold {P.beige_dark}")))
                await body.mount_all(thing_tile(w, i) for i in items)

    async def _render_inbox(self, body, crumbs, main) -> None:
        self.suggestions = hk.load(self.app.cfg.vault)
        open_ = [s for s in self.suggestions if not s.done]
        main.border_title = "INVENTORY ░ MABEL'S HOUSEKEEPING"
        crumbs.update(Text(f"{len(open_)} open · {sum(s.applicable for s in open_)} can be applied with a · "
                           "x ticks one off · enter opens the item", style=P.beige_dark))
        section = None
        for n, s in enumerate(self.suggestions):
            if s.done:
                continue
            if s.section != section:
                section = s.section
                await body.mount(Static(Text(f"\n{section.upper()}", style=f"bold {P.beige}"), classes="sec"))
            text = Text()
            text.append("◆ " if s.applicable else "◇ ", style=P.teal if s.applicable else P.amber_dim)
            text.append(s.name[:56], style=P.amber_hi)
            text.append(f"  {', '.join(s.ids)}\n", style=P.beige_dark)
            text.append(f"   → {s.change}", style=P.amber)
            if s.why:
                text.append(f"   ({s.why})", style=P.beige_dark)
            tile = Tile(text, item_id=s.ids[0], classes="suggestion")
            tile.suggestion = s
            await body.mount(tile)
        if not open_:
            await body.mount(Static(Text("Nothing waiting. Mabel does a round when you're away.", style=P.amber_dim)))

    # ── the item card ──────────────────────────────────────────────────

    async def show_item(self, item_id: str) -> None:
        try:
            d = await self.idx.item(item_id)
        except Exception as exc:
            self.app.notify(f"Couldn't open #{item_id}: {exc}", severity="error")
            return
        self.selected, self.item = item_id, d
        w = self.world
        t = Text()
        t.append(f"{d.get('name')}\n", style=f"bold {P.amber_hi}")
        t.append(f"#{d['id']}", style=P.beige_dark)
        if d.get("spec_code"):
            t.append(f"  ·  {d['spec_code']}", style=P.teal)
        t.append(f"  ·  {d.get('category') or '—'}\n\n", style=P.amber_dim)
        if d.get("status") == "away":
            t.append("● OUT ", style=f"bold {AWAY}")
            t.append(f"to {d.get('checked_out_to') or '?'} since {str(d.get('checked_out_at') or '')[:16]}", style=AWAY)
            if d.get("checkout_reason"):
                t.append(f" · {d['checkout_reason']}", style=AWAY)
            t.append("\n")
        else:
            t.append(f"● {str(d.get('status') or 'home').upper()}\n", style=f"bold {P.teal}")
        t.append("HOME  ", style=f"bold {P.beige}")
        t.append(f"{w.place_title(d.get('address')) if w else d.get('address')}\n", style=P.amber)
        t.append(f"      {d.get('address') or 'no address'}\n", style=P.beige_dark)
        if d.get("parent_item_id"):
            parent = (w.I.get(d["parent_item_id"]) if w else None) or {}
            t.append("IN    ", style=f"bold {P.beige}")
            t.append(f"{parent.get('name', d['parent_item_id'])}\n", style=P.amber)
        qty = d.get("quantity")
        t.append("QTY   ", style=f"bold {P.beige}")
        t.append(f"{qty:g} " if isinstance(qty, (int, float)) else "? ", style=P.amber)
        t.append(f"{d.get('quantity_note') or ''}\n", style=P.amber_dim)
        t.append("STAGE ", style=f"bold {P.beige}")
        t.append(f"{d.get('stage') or '—'} · {d.get('confidence') or ''}"
                 f"{' · ' + d['decision'] if d.get('decision') else ''}\n", style=P.amber)
        if d.get("nominal_size"):
            t.append("SIZE  ", style=f"bold {P.beige}")
            size = d["nominal_size"]
            t.append(("×".join(str(x) for x in size) if isinstance(size, (list, tuple)) else str(size)) + "\n",
                     style=P.amber)
        if d.get("nicknames"):
            t.append("AKA   ", style=f"bold {P.beige}")
            t.append(", ".join(n if isinstance(n, str) else n.get("alias", "") for n in d["nicknames"]) + "\n", style=P.amber)
        for label, key in (("SPECS", "specs"), ("NOTES", "notes")):
            if d.get(key):
                t.append(f"\n{label}\n", style=f"bold {P.beige}")
                t.append(d[key][:900] + "\n", style=P.amber)
        children = d.get("children") or []
        if children:
            t.append(f"\nINSIDE ({len(children)})\n", style=f"bold {P.beige}")
            for c in children[:20]:
                t.append(f" ▪ {c.get('name')}  #{c.get('id')}\n", style=P.amber)
        moves = d.get("movements") or []
        if moves:
            t.append("\nLATELY\n", style=f"bold {P.beige}")
            for mv in moves[-4:][::-1]:
                t.append(f" {str(mv.get('happened_at', ''))[:16]}  {mv.get('kind')}  {mv.get('reason') or ''}\n"[:90],
                         style=P.beige_dark)
        t.append("\ne edit · m move · c check out · r return · k nickname · p its place", style=P.amber_faint)
        self.query_one("#inv-card", Static).update(t)
        self.query_one("#inv-item").border_title = f"ITEM ░ #{d['id']}"
        await self._show_photo(d)

    async def _show_photo(self, d: dict) -> None:
        holder = self.query_one("#inv-photo", Container)
        images = d.get("images") or []
        path = (images[0].get("path") if images and isinstance(images[0], dict) else None) or \
            (self.world.icons.get(d["id"]) if self.world else None)
        if path == self._image_key:
            return
        self._image_key = path
        await holder.remove_children()
        if not path:
            return
        data = await self.idx.image(path)
        if not data:
            return
        CACHE.mkdir(parents=True, exist_ok=True)
        local = CACHE / ("item-" + path.replace("/", "_"))
        local.write_bytes(data)
        from textual_image.widget import HalfcellImage as Image   # pixel art, like the rest
        await holder.mount(Image(str(local), id="inv-image"))

    # ── navigation ──────────────────────────────────────────────────────

    @on(Zoom)
    async def zoom(self, event: Zoom) -> None:
        event.stop()
        if event.route == "!loose":
            return await self.set_mode("loose")
        self.mode = "browse"
        self.route = self.world.resolve(event.route) if self.world else event.route
        await self.render_page()

    @on(Pick)
    async def pick(self, event: Pick) -> None:
        event.stop()
        await self.show_item(event.item_id)
        if self.mode == "browse" and any(z.kind == "grid" for z in (self.world.page(self.route)["zones"] if self.world else [])):
            await self.render_page()                 # redraw the map with this bin highlighted

    async def set_mode(self, mode: str) -> None:
        self.mode = mode
        await self.render_page()

    async def action_up(self) -> None:
        if self.mode != "browse":
            return await self.set_mode("browse")
        if self.world and self.route:
            chain = self.world.chain(self.route)
            self.route = chain[-2] if len(chain) > 1 else ""
            await self.render_page()

    async def action_home(self) -> None:
        self.mode, self.route = "browse", ""
        await self.render_page()

    def action_search(self) -> None:
        self.query_one("#inv-search", Input).focus()

    @on(Input.Submitted, "#inv-search")
    async def searched(self, event: Input.Submitted) -> None:
        if event.value.strip():
            await self.set_mode("search")
            tiles = self.query_one("#inv-body").query(Tile)
            if tiles:
                tiles.first().focus()

    async def action_inbox(self) -> None:
        await self.set_mode("inbox")

    async def action_loose(self) -> None:
        await self.set_mode("loose")

    async def action_goto_place(self) -> None:
        if self.item and self.world and self.item.get("address"):
            self.mode = "browse"
            self.route = self.world.resolve(self.world.place_route(self.item["address"]))
            await self.render_page()

    def action_reload(self) -> None:
        self.load_world()

    # ── writes (all through tk-index, undoable there) ──────────────────

    async def _write(self, path: str, data: dict, done: str) -> bool:
        try:
            await self.idx.post(path, data)
        except Exception as exc:
            self.app.notify(str(exc), severity="error", timeout=6)
            return False
        self.app.notify(f"✓ {done}", timeout=3)
        self.load_world(then_item=self.selected)
        return True

    def _need_item(self) -> bool:
        if not self.item:
            self.app.notify("Pick an item first", severity="warning")
            return False
        return True

    def action_edit(self) -> None:
        if not self._need_item():
            return
        d = self.item
        fields = [(k, label, d.get(k) if d.get(k) is not None else "", kind or "line") for k, label, kind in EDIT_FIELDS]

        async def done(result: dict | None) -> None:
            if not result:
                return
            changed = {}
            for k, v in result.items():
                old = d.get(k)
                if k == "quantity":
                    v = float(v) if str(v).strip() else None
                if (old if old is not None else "") != (v if v is not None else ""):
                    changed[k] = v
            if changed:
                await self._write(f"items/{d['id']}/edit", changed, f"Edited {d.get('name')}")
        self.app.push_screen(Ask(f"Edit #{d['id']}", fields), done)

    def action_move(self) -> None:
        if not self._need_item():
            return
        d = self.item

        async def done(result: dict | None) -> None:
            if result and result.get("destination", "").strip():
                await self._write(f"items/{d['id']}/move", {"destination": result["destination"].strip().upper(),
                                                            "reason": "moved in the TUI"}, f"Moved {d.get('name')}")
        self.app.push_screen(Ask(f"Move {d.get('name')}", [("destination", "New home address (16 characters, e.g. GRIDES0001C03R05)",
                                                             d.get("address") or "", "line")], ok="Move"), done)

    def action_checkout(self) -> None:
        if not self._need_item():
            return
        d = self.item

        async def done(result: dict | None) -> None:
            if result is not None:
                await self._write(f"items/{d['id']}/checkout", {"destination": result.get("destination") or None,
                                                                "reason": result.get("reason", "")},
                                  f"{d.get('name')} is out")
        self.app.push_screen(Ask(f"Check out {d.get('name')}", [("destination", "Where is it going? (optional)", "", "line"),
                                                                ("reason", "What for? (optional)", "", "line")],
                                 ok="Check out"), done)

    async def action_return_home(self) -> None:
        if self._need_item():
            await self._write(f"items/{self.item['id']}/return", {}, f"{self.item.get('name')} is back home")

    def action_nickname(self) -> None:
        if not self._need_item():
            return
        d = self.item

        async def done(result: dict | None) -> None:
            if result and result.get("add", "").strip():
                await self._write(f"items/{d['id']}/nickname", {"add": result["add"].strip()}, "Nickname added")
        self.app.push_screen(Ask(f"Nickname for {d.get('name')}", [("add", "Also called", "", "line")], ok="Add"), done)

    async def action_undo(self) -> None:
        try:
            last = (await self.idx.get("undo")).get("last") or {}
            await self.idx.post("undo", {})
        except Exception as exc:
            self.app.notify(str(exc), severity="warning")
            return
        self.app.notify(f"↶ undid: {last.get('label', 'the last change')}", timeout=4)
        self.load_world(then_item=self.selected)

    # ── the housekeeping inbox ─────────────────────────────────────────

    def _focused_suggestion(self) -> hk.Suggestion | None:
        focused = self.app.focused
        return getattr(focused, "suggestion", None)

    async def action_apply(self) -> None:
        s = self._focused_suggestion()
        if s is None:
            return
        if not s.applicable:
            self.app.notify("That one is a to-do for you (x ticks it off once done)", severity="warning")
            return
        for item_id in s.ids:
            try:
                await self.idx.post(f"items/{item_id}/edit", s.edit)
            except Exception as exc:
                self.app.notify(f"#{item_id}: {exc}", severity="error", timeout=6)
                return
        hk.tick(self.app.cfg.vault, s)
        self.app.notify(f"✓ applied to {len(s.ids)} item(s): {s.change} (undo: u)", timeout=4)
        self.world = await self.idx.world()
        await self.render_page()

    async def action_tick(self) -> None:
        s = self._focused_suggestion()
        if s is not None and hk.tick(self.app.cfg.vault, s):
            self.app.notify("ticked off", timeout=2)
            await self.render_page()

    def on_key(self, event) -> None:
        if event.key == "escape" and self.mode != "browse":
            event.stop()
            self.run_worker(self.set_mode("browse"))
