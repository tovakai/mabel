"""Noticing when Anthon takes a while to answer.

When his reply to her last message took much longer than he usually takes,
or he took a while to answer a message she sent him first (outreach, news),
the persona composer tells her, as a plain observation: how long it took,
what's usual for him, what his devices showed meanwhile, and whether that's
been happening lately. What she does with it (wonder, tease, let it go, keep
it in mind) is hers; nothing here tells her to react.

"Usual" is his own median reply time over the last 30 days, so the bar moves
with him, kept separately for messages she wrote first: those reach him on his
phone while he's busy, so a slower answer is normal and the bar sits higher.
Inside a conversation, gaps long enough for the "since your last conversation"
gap note are left to that note; for her first messages this note replaces it. Each slow reply is kept in ``data/persona/pace.json``
(local, private) so later phases can find patterns in it.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MIN_SLOW_MINUTES = 15          # in a conversation: never "slow" below this, however quick he usually is
SLOW_FACTOR = 5                # ... and at least this many times his usual
FIRST_MIN_SLOW_MINUTES = 120   # her first messages: never "slow" below two hours
FIRST_SLOW_FACTOR = 3
FIRST_DEFAULT_USUAL = 60       # until there are enough of them to have a median
FIRST_MAX_MINUTES = 7 * 24 * 60
FIRST_BASELINE_DAYS = 90
FRESH_SECONDS = 180            # his reply must be the message being answered now
BASELINE_DAYS = 30
BASELINE_TTL_S = 3600
KEEP = 200
_LOCK = threading.Lock()
_baseline: dict = {}


def _db():
    from core.database import SessionLocal
    return SessionLocal()


def _utc(value) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def wrote_first(position: int, metadata) -> bool:
    """Her own opener: the first message of a chat she started (outreach, news), not a UI greeting."""
    if position != 0:
        return False
    try:
        meta = json.loads(metadata) if isinstance(metadata, str) else (metadata or {})
    except ValueError:
        return False
    return bool(meta.get("outreach_id") or meta.get("news_day") or meta.get("background_tool_result"))


def baselines(*, now: datetime | None = None, rows=None) -> dict:
    """His median reply time in minutes: ``conversation`` (to her replies) and ``first`` (to her openers)."""
    if rows is None:
        cached = _baseline.get("value")
        if cached is not None and time.monotonic() - _baseline.get("at", 0) < BASELINE_TTL_S:
            return cached
        from sqlalchemy import text
        now = now or datetime.now(timezone.utc)
        db = _db()
        try:
            rows = db.execute(text("SELECT session_id, role, timestamp, metadata FROM chat_messages "
                                   "WHERE timestamp > :since ORDER BY session_id, timestamp"),
                              {"since": (now - timedelta(days=FIRST_BASELINE_DAYS)).replace(tzinfo=None)}).fetchall()
        finally:
            db.close()
        recent_cut = now - timedelta(days=BASELINE_DAYS)
    else:
        recent_cut = None
    conversation, first = [], []
    prev, position, current = None, 0, None
    for session, role, ts, metadata in rows:
        at = _utc(datetime.fromisoformat(ts) if isinstance(ts, str) else ts)
        position = position + 1 if session == current else 0
        current = session
        if position == 0:
            opener = at if role == "assistant" and wrote_first(0, metadata) else None
            answered = False
        if prev and prev[0] == session and prev[1] == "assistant" and role == "user":
            minutes = (at - prev[2]).total_seconds() / 60
            if opener and not answered and position == 1:
                if minutes <= FIRST_MAX_MINUTES:
                    first.append(minutes)
            elif 0 <= minutes <= 360 and (recent_cut is None or at >= recent_cut):
                conversation.append(minutes)
        if role == "user":
            answered = True
        prev = (session, role, at)
    value = {"conversation": statistics.median(conversation) if len(conversation) >= 10 else None,
             "first": statistics.median(first) if len(first) >= 5 else FIRST_DEFAULT_USUAL}
    _baseline.update(value=value, at=time.monotonic())
    return value


def reply_wait(session_id: str, *, now: datetime | None = None, messages=None) -> dict | None:
    """How long her last message waited for the reply he's sending now (None if it isn't that situation).

    ``messages``: (id, role, timestamp, metadata) for the whole session.
    """
    now = now or datetime.now(timezone.utc)
    if messages is None:
        from sqlalchemy import text
        db = _db()
        try:
            messages = db.execute(text("SELECT id, role, timestamp, metadata FROM chat_messages WHERE session_id = :s "
                                       "ORDER BY timestamp LIMIT 2000"), {"s": session_id}).fetchall()
        finally:
            db.close()
    ordered = sorted(((mid, role, _utc(datetime.fromisoformat(ts) if isinstance(ts, str) else ts), meta)
                      for mid, role, ts, meta in messages), key=lambda m: m[2])
    index = next((i for i in range(len(ordered) - 1, -1, -1) if ordered[i][1] == "assistant"), None)
    if index is None:
        return None
    last_mabel = ordered[index]
    replies = [m for m in ordered[index + 1:] if m[1] == "user"]
    replied_at = replies[0][2] if replies else now
    if (now - replied_at).total_seconds() > FRESH_SECONDS:
        return None                           # already answered earlier in this conversation
    return {"message_id": last_mabel[0], "since": last_mabel[2], "until": replied_at,
            "minutes": (replied_at - last_mabel[2]).total_seconds() / 60,
            "first": wrote_first(index, last_mabel[3])}


def meanwhile(start: datetime, end: datetime, *, store=None) -> list[str]:
    """What his devices showed during the wait: at most two things, biggest first."""
    try:
        from src.lifelog.store import default_store
        from src.lifelog.summary import summarize
        summary = summarize((store or default_store()).spans(start, end), start=start, end=end)
    except Exception:
        return []
    things = []
    for key, label in (("games", "playing {}"), ("media", "watching {}"), ("apps", "in {}")):
        top = (summary.get(key) or [None])[0]
        if top and top.get("minutes", 0) >= 5:
            things.append((top["minutes"], label.format(top["name"]) + f" ({top['minutes']} min)"))
    for place in summary.get("places") or []:
        if str(place.get("place", "")).strip().lower() not in ("home", "hemma") and place.get("minutes", 0) >= 5:
            things.append((place["minutes"], f"at {place['place']}"))
            break
    return [text for _, text in sorted(things, key=lambda t: -t[0])[:2]]


def _path(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from src.constants import DATA_DIR
        data_dir = Path(DATA_DIR)
    return Path(data_dir) / "persona" / "pace.json"


def history(data_dir: Path | None = None) -> list[dict]:
    try:
        return json.loads(_path(data_dir).read_text(encoding="utf-8")).get("slow") or []
    except (OSError, ValueError, AttributeError):
        return []


def _keep(event: dict, data_dir: Path | None = None) -> None:
    with _LOCK:
        events = history(data_dir)
        if any(e.get("message_id") == event["message_id"] for e in events):
            return
        path = _path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pace-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"slow": (events + [event])[-KEEP:]}, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, path)


def _duration(minutes: float) -> str:
    if minutes < 90:
        return f"{round(minutes)} min"
    hours = minutes / 60
    return f"{hours:.1f} h".replace(".0 h", " h")


def note(session_id: str | None, *, now: datetime | None = None, max_minutes: float = 360,
         data_dir: Path | None = None, wait=None, usual=None, doing=None) -> dict | None:
    """``{"text", "first"}`` for the composer, or None when his reply wasn't notably slow.

    ``max_minutes`` bounds replies inside a conversation (longer is the gap note's job);
    replies to her first messages go up to a week.
    """
    if not session_id:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        wait = wait if wait is not None else reply_wait(session_id, now=now)
        if not wait:
            return None
        first = bool(wait.get("first"))
        if wait["minutes"] >= (FIRST_MAX_MINUTES if first else max_minutes):
            return None
        usual = usual if usual is not None else baselines(now=now)["first" if first else "conversation"]
        floor, factor = (FIRST_MIN_SLOW_MINUTES, FIRST_SLOW_FACTOR) if first else (MIN_SLOW_MINUTES, SLOW_FACTOR)
        if usual is None or wait["minutes"] < max(floor, factor * usual):
            return None
        doing = doing if doing is not None else meanwhile(wait["since"], wait["until"])
        week = [e for e in history(data_dir) if e.get("message_id") != wait["message_id"]
                and now - datetime.fromisoformat(e["at"]) <= timedelta(days=7)]
        _keep({"at": wait["until"].isoformat(timespec="seconds"), "message_id": wait["message_id"],
               "session": session_id, "first": first, "minutes": round(wait["minutes"], 1), "usual": round(usual, 1),
               "meanwhile": doing}, data_dir)
    except Exception:
        logger.debug("[pace] couldn't tell how long he took", exc_info=True)
        return None
    if first:
        text = (f"You wrote to him first, and he's answering {_duration(wait['minutes'])} later; "
                f"when you reach out, he usually answers within about {_duration(max(usual, 1))}.")
    else:
        text = (f"Your last message waited {_duration(wait['minutes'])} for this reply; "
                f"he usually answers you within about {_duration(max(usual, 1))}.")
    if doing:
        text += " Meanwhile his devices showed him " + " and ".join(doing) + "."
    if week:
        text += f" He's been slow to answer {len(week)} other time{'s' if len(week) > 1 else ''} this week."
    return {"text": text + " Notice it or not; that's up to you.", "first": first}
