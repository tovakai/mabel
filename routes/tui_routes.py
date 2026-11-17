"""Read-only snapshot of Mabel's day for the terminal client's LIFE tab (tui/).

GET /api/tui/life          what she's doing, each job's last run and what holds it back,
                           her latest dream, song, paper note, book and news, his lists.
GET /api/tui/dream-image   the latest dream picture (for kitty's image protocol).

Browser sessions, or an API token with the "life:read" scope (token profile "tui").
Everything here comes from files the heartbeat already writes; nothing is computed by a model.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from src.auth_helpers import require_api_token_scope, require_user

logger = logging.getLogger(__name__)
_REPO = Path(__file__).resolve().parents[1]
MAX_TEXT = 1200


def life_user(request: Request) -> str:
    if getattr(request.state, "api_token", False):
        return require_api_token_scope(request, "life:read") or ""
    return require_user(request)


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _body(ref: str | None, limit: int = MAX_TEXT) -> str:
    """A journal note's text without its frontmatter (journal/… inside the repo only)."""
    if not ref:
        return ""
    note = (_REPO / ref).resolve()
    if _REPO.resolve() not in note.parents or not note.is_file():
        return ""
    text = note.read_text(encoding="utf-8", errors="replace")
    if text.startswith("---\n"):
        text = text[4:].partition("\n---\n")[2]
    text = re.sub(r"!\[\[[^\]]*\]\]\s*", "", text)          # Obsidian embeds (her dream picture)
    text = re.sub(r"\A\s*# .*\n+", "", text)                    # the note's own title line
    text = re.sub(r"^> \[!.*(?:\n>.*)*", "", text, flags=re.M)   # callout blocks (what she dreamt from)
    return text.strip()[:limit]


def _last(items) -> dict:
    return items[-1] if isinstance(items, list) and items and isinstance(items[-1], dict) else {}


def jobs(now: datetime) -> list[dict]:
    """Per job: when it last ran (24 h) and what holds it back right now."""
    from src.heartbeat import why
    try:
        report = why.explain(hours=24, now=now)
    except Exception:
        logger.debug("[tui] why.explain failed", exc_info=True)
        return []
    out = []
    for name, periods in (report.get("jobs") or {}).items():
        ran = [p for p in periods if "ran" in p]
        waiting = next((p for p in reversed(periods) if "reasons" in p and not p.get("to")), None)
        out.append({"job": name, "last_ran": ran[-1]["at"] if ran else None,
                    "outcome": ran[-1]["ran"] if ran else None,
                    "waiting_on": (waiting or {}).get("reasons") or []})
    return out


def doing() -> list[str]:
    """What's running right now, from the heartbeat's own task handles (same process)."""
    busy = []
    try:
        from src.heartbeat import daemon
        for label, attr in (("dreaming", "_dream_task"), ("revisiting", "_revisit_task"), ("composing", "_music_task"),
                            ("writing on paper", "_paper_task"), ("reading", "_book_task"),
                            ("reading the news", "_news_task"), ("writing to Tova", "_letter_task"),
                            ("on a Gremlin round", "_housekeeping_task"), ("writing the morning page", "_morning_task")):
            task = getattr(daemon, attr, None)
            if task is not None and not task.done():
                busy.append(label)
    except Exception:
        pass
    try:
        from src.curiosity import runner
        if runner.status().get("running"):
            busy.append("researching")
    except Exception:
        pass
    return busy


def snapshot(data_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    persona = data_dir / "persona"
    dreams, music, paper = _json(persona / "dreams.json"), _json(persona / "music.json"), _json(persona / "paper.json")
    books, news = _json(persona / "books.json"), _json(persona / "news.json")
    dream, song, note = _last(dreams.get("history")), _last(music.get("history")), _last(paper.get("history"))
    reading = books.get("current") or {}
    latest_news = _last(news.get("history"))
    groceries = []
    try:
        from src.initiative import groceries as g
        base = (g.load_state(data_dir).get("base") or {}).values()
        groceries = [{"item": i.get("name"), "amount": i.get("amount") or "", "bought": bool(i.get("bought"))}
                     for i in base]
    except Exception:
        pass
    gremlin_out = []
    try:
        from src.agent_tools import gremlin_tool
        inv, db = gremlin_tool.open_index()
        try:
            gremlin_out = [{"item": i["name"], "id": i["id"], "days_out": i.get("days_out", 0)}
                           for i in gremlin_tool.checked_out(inv, db)]
        finally:
            db.close()
    except Exception:
        pass
    length, position = reading.get("length") or 0, reading.get("position") or 0
    return {
        "at": now.isoformat(timespec="seconds"),
        "doing": doing(),
        "jobs": jobs(now),
        "dream": {"title": dream.get("title"), "at": dream.get("at"), "first_line": dream.get("first_line"),
                  "text": _body(dream.get("note")), "has_image": bool(dream.get("image"))} if dream else None,
        "song": {"title": song.get("title"), "at": song.get("at"), "text": _body(song.get("note"), 600)} if song else None,
        "paper": {"at": note.get("at"), "why": note.get("why"), "text": _body(note.get("note"), 800)} if note else None,
        "book": {"title": reading.get("title"), "author": reading.get("author"),
                 "progress": round(position / length, 3) if length else None} if reading else None,
        "news": {"day": latest_news.get("day"), "message": latest_news.get("message")} if latest_news else None,
        "groceries": groceries,
        "gremlin_out": gremlin_out,
    }


def setup_tui_routes() -> APIRouter:
    router = APIRouter(tags=["tui"])

    @router.get("/api/tui/life")
    async def life(_user=Depends(life_user)):
        import asyncio
        from src.constants import DATA_DIR
        return await asyncio.to_thread(snapshot, Path(DATA_DIR))

    @router.get("/api/tui/dream-image")
    async def dream_image(_user=Depends(life_user)):
        from src.constants import DATA_DIR
        dream = _last(_json(Path(DATA_DIR) / "persona" / "dreams.json").get("history"))
        name = Path(str(dream.get("image") or "")).name
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:png|jpe?g|webp)", name):
            raise HTTPException(404, "no dream picture")
        path = (Path(DATA_DIR) / "generated_images" / name).resolve()
        if not path.is_file() or (Path(DATA_DIR) / "generated_images").resolve() not in path.parents:
            raise HTTPException(404, "no dream picture")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    return router
