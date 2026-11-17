"""LIFE: what Mabel is doing and has been up to, from /api/tui/life. Refreshes every minute (r: now)."""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, VerticalScroll
from textual.widgets import ProgressBar, Static

from .config import CACHE
from .nav import mark

JOB_NAMES = {"research": "research", "letters": "tova letters", "news": "news", "dream": "dream",
             "revisit": "look back", "music": "compose", "paper": "paper note", "morning": "morning page",
             "book": "book", "outreach": "outreach", "housekeeping": "gremlin round"}


def _hm(value: str | None) -> str:
    if not value:
        return "--:--"
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%H:%M")
    except ValueError:
        return "--:--"


def _status(data: dict) -> Text:
    out = Text()
    doing = data.get("doing") or []
    out.append("NOW  ", style="bold #d9cba4")
    out.append((", ".join(doing) if doing else "idle, waiting for you to step away") + "\n\n",
               style="bold #3fd6c6" if doing else "#a86b00")
    for job in data.get("jobs") or []:
        name = JOB_NAMES.get(job["job"], job["job"])
        ran = job.get("last_ran")
        out.append(f"{'●' if ran else '○'} ", style="#3fd6c6" if ran else "#3d2a08")
        out.append(f"{name:<14}", style="#ffb000")
        out.append(f"{_hm(ran)}  ", style="#ffd066" if ran else "#3d2a08")
        waiting = (job.get("waiting_on") or [""])[0]
        reason = job.get("outcome") or waiting
        out.append(f"{reason[:26] + '…' if len(reason) > 27 else reason}\n", style="#a86b00")
    return out


def _journal(data: dict) -> Text:
    out = Text()

    def head(label: str, when: str | None = None):
        out.append(f"\n{label}", style="bold #d9cba4")
        out.append(f"  ·  {_hm(when)}\n" if when else "\n", style="#8a7b58")

    song = data.get("song")
    if song:
        head("♪ LAST COMPOSITION", song.get("at"))
        out.append(f"{song.get('title')}\n", style="bold #ffd066")
    note = data.get("paper")
    if note:
        head("✎ ON PAPER", note.get("at"))
        out.append((note.get("text") or "")[:400] + "\n", style="#ffb000")
    news = data.get("news")
    if news and news.get("message"):
        head(f"◈ NEWS  {news.get('day') or ''}")
        out.append(news["message"][:420] + "\n", style="#ffb000")
    return out


def _lists(data: dict) -> Text:
    out = Text()
    out.append("GROCERIES\n", style="bold #d9cba4")
    items = data.get("groceries") or []
    for item in items:
        mark, style = ("☑", "#8a7b58") if item.get("bought") else ("☐", "#ffd066")
        amount = f" ({item['amount']})" if item.get("amount") else ""
        out.append(f" {mark} {item.get('item')}{amount}\n", style=style)
    if not items:
        out.append(" nothing on the list\n", style="#a86b00")
    out.append("\nGREMLIN · CHECKED OUT\n", style="bold #d9cba4")
    gone = data.get("gremlin_out") or []
    for item in gone:
        days = item.get("days_out", 0)
        out.append(f" ▪ {item.get('item')[:30]}", style="#ffb000")
        out.append(f"  {days}d\n", style="#ff5a36" if days >= 3 else "#a86b00")
    if not gone:
        out.append(" everything is home\n", style="#a86b00")
    return out


class Life(Grid):
    BINDINGS = [Binding("r", "refresh", "refresh")]

    def __init__(self, **kw):
        super().__init__(id="life-view", **kw)
        self._dream_at = None

    def compose(self) -> ComposeResult:
        status = VerticalScroll(Static(id="status-text"), id="life-status", classes="panel section")
        status.border_title = "SYSTEMS"
        yield status
        dream = VerticalScroll(Container(id="dream-holder"), Static(id="dream-text"), id="life-dream", classes="panel section")
        dream.border_title = "LAST DREAM"
        yield dream
        journal = VerticalScroll(Static(id="book-text"), ProgressBar(total=1.0, show_eta=False, id="book-bar"),
                                 Static(id="journal-text"), id="life-journal", classes="panel section")
        journal.border_title = "JOURNAL"
        yield journal
        lists = VerticalScroll(Static(id="lists-text"), id="life-lists", classes="panel section")
        lists.border_title = "LISTS"
        yield lists

    def on_mount(self) -> None:
        self.action_refresh()
        self.set_interval(60, self.action_refresh)

    @work(exclusive=True, group="life")
    async def action_refresh(self) -> None:
        try:
            data = await self.app.mabel.life()
        except Exception as exc:
            self.query_one("#status-text", Static).update(Text(f"✖ can't reach Mabel: {exc}", style="#ff5a36"))
            return
        self.query_one("#status-text", Static).update(_status(data))
        self.query_one("#journal-text", Static).update(_journal(data))
        self.query_one("#lists-text", Static).update(_lists(data))
        book = data.get("book") or {}
        self.query_one("#book-text", Static).update(Text.assemble(
            ("▤ READING\n", "bold #d9cba4"), (f"{book.get('title') or 'nothing right now'}\n", "bold #ffd066"),
            (f"{book.get('author') or ''}", "#a86b00")))
        self.query_one("#book-bar", ProgressBar).update(progress=book.get("progress") or 0)
        dream = data.get("dream") or {}
        self.query_one("#life-dream").border_subtitle = _hm(dream.get("at"))
        self.query_one("#dream-text", Static).update(Text.assemble(
            (f"{dream.get('title') or ''}\n\n", "bold #ffd066"), ((dream.get("text") or "")[:1200], "#ffb000")))
        if dream.get("has_image") and dream.get("at") != self._dream_at:
            await self._show_dream(dream.get("at"))

    async def _show_dream(self, at: str | None) -> None:
        picture = await self.app.mabel.dream_image()
        if not picture:
            return
        CACHE.mkdir(parents=True, exist_ok=True)
        path = CACHE / "dream.png"
        path.write_bytes(picture)
        from textual_image.widget import HalfcellImage as Image   # pixel art on purpose: 1×2 px per cell
        holder = self.query_one("#dream-holder", Container)
        await holder.remove_children()
        await holder.mount(Image(str(path), id="dream-image"))
        self._dream_at = at
