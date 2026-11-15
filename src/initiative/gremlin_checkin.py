"""Evening check-in on the Gremlin Index: what's still checked out?

Once a night-owl day (06:00 -> 06:00), from ``HOUR`` local time on, if anything
in his inventory is checked out, Mabel writes him a short text about it. It
lands as a new chat plus a phone notification, the same way her outreach does
(src/initiative/outreach.py), so he can answer right there ("the calipers are
back") and she returns them with manage_gremlin.

Gates (deterministic): setting gremlin_checkin_enabled, initiative not paused,
the hour, not already done today, something out, and her model free (no chat,
no research, Strata up). Busy evenings just wait for the next heartbeat tick;
past ``LAST_HOUR`` the day is skipped. Nothing out = nothing sent.

State: data/persona/gremlin_checkin.json. CLI: python -m src.initiative.gremlin_checkin --preview
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

HOUR = 21                 # earliest local hour for the check-in
LAST_HOUR = 3             # after this (next morning) the day's check-in is skipped
DAY_START_HOUR = 6
MESSAGE_LIMIT = 900
MAX_LISTED = 15
TOKENS = 4000             # her private thinking counts against this too


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def day_of(now: datetime) -> str:
    return (now.astimezone() - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "gremlin_checkin.json"


def load_state(data_dir: Path) -> dict:
    try:
        data = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gremlin_checkin.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def in_window(now: datetime) -> bool:
    hour = now.astimezone().hour
    return hour >= HOUR or hour < LAST_HOUR


def gate(*, data_dir: Path, now: datetime, model_free: bool, force: bool = False) -> list[str]:
    """Why it can't run now ([] = go). Doesn't open the database."""
    reasons = []
    if not _setting("gremlin_checkin_enabled", True):
        reasons.append("switched off (gremlin_checkin_enabled)")
    if _setting("initiative_paused", False):
        reasons.append("initiative paused")
    if not force and not in_window(now):
        reasons.append(f"not evening yet (from {HOUR}:00)")
    if not force and load_state(data_dir).get("last_day") == day_of(now):
        reasons.append("already checked in today")
    if not model_free:
        reasons.append("her model is busy")
    return reasons


def items_out() -> list[dict]:
    from src.agent_tools import gremlin_tool
    inv, db = gremlin_tool.open_index()
    try:
        return gremlin_tool.checked_out(inv, db)
    finally:
        db.close()


def _describe(item: dict) -> str:
    days = item.get("days_out", 0)
    when = "today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago"
    bits = [f"- {item['name']} (ID {item['id']}), out since {when}"]
    if item.get("out_to"):
        bits.append(f"to {item['out_to']}")
    if item.get("reason"):
        bits.append(f"for: {item['reason']}")
    if item.get("with_it"):
        bits.append("with " + ", ".join(item["with_it"][:5]) + " inside")
    bits.append(f"home: {item.get('home')}")
    return "; ".join(bits)


async def write_message(items: list[dict], *, owner: str, ask=None) -> str:
    listing = "\n".join(_describe(i) for i in items[:MAX_LISTED])
    if len(items) > MAX_LISTED:
        listing += f"\n- … and {len(items) - MAX_LISTED} more"
    prompt = (
        "It's evening. Anthon asked you to check in with him at night about things from his Gremlin Index "
        "(his workshop inventory) that are still checked out, because he's bad at putting stuff back. "
        "Right now these are out:\n\n" + listing + "\n\n"
        "Write him a short text, in your own voice: name what's out (use the names he'd recognise, "
        "not IDs), and ask whether any of it is back or should go back tonight. If something has been "
        "out a long time, you can tease him a little. No lecture, no guilt. He can answer you in the same "
        "chat, and you'll mark things returned.\n"
        "Reply with only the message text.")
    system = "You are Mabel."
    try:
        from src.persona import composer
        library = composer().modules.load()
        if library.core:
            system = library.core.body
    except Exception:
        pass
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if ask is None:
        from src import model_roles
        reply = await model_roles.call("default", messages, owner=owner, max_tokens=TOKENS, temperature=0.8,
                                       timeout=180)
    else:
        reply = await ask(messages, owner)
    message = str(reply or "").strip().strip('"').strip()
    from src.persona.modules import _AUTHORITY_CONFLICT
    if not message or len(message) > MESSAGE_LIMIT or _AUTHORITY_CONFLICT.search(message):
        return ""
    return message


def _model_free() -> bool:
    from src.curiosity import runner
    from src.heartbeat import signals
    return not signals.any_chat_active() and not runner.status().get("running") and signals.strata_up()


async def run_once(*, data_dir: Path, owner: str = "tovakai", now: datetime | None = None, ask=None,
                   model_free: bool | None = None, send=True, force: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    if model_free is None:
        model_free = _model_free()
    blocked = gate(data_dir=data_dir, now=now, model_free=model_free, force=force)
    if blocked:
        return {"status": "gated", "reasons": blocked}
    state = load_state(data_dir)
    try:
        items = items_out()
    except Exception as exc:
        logger.warning("[gremlin] couldn't read the index: %s", exc)
        return {"status": "error", "reasons": [f"index unreadable: {exc}"]}
    if not items:
        if not send:
            return {"status": "nothing_out"}
        state.update(last_day=day_of(now), last={"at": now.isoformat(), "status": "nothing_out"})
        save_state(data_dir, state)
        return {"status": "nothing_out"}
    message = await write_message(items, owner=owner, ask=ask)
    if not message:
        return {"status": "no_message", "reasons": ["the drafted message didn't pass the checks"]}
    record = {"at": now.isoformat(), "items": [i["id"] for i in items], "message": message}
    if send:
        from src.initiative import outreach
        session_id = outreach.create_chat(owner, "🧰 Still out tonight", message,
                                          {"gremlin_checkin": day_of(now), "gremlin_items": record["items"]})
        delivered = outreach.send_notification(
            message if len(message) <= outreach.PUSH_LIMIT else message[:outreach.PUSH_LIMIT].rsplit(" ", 1)[0] + "…",
            outreach.chat_link(session_id), f"gremlin-{day_of(now)}")
        record.update(session_id=session_id, status="sent" if delivered else "in_app_only")
    else:
        record["status"] = "preview"
    if send:
        state.update(last_day=day_of(now), last=record)
        state["history"] = (state.get("history") or [])[-60:] + [record]
        save_state(data_dir, state)
    logger.info("[gremlin] evening check-in: %d item(s) out, %s", len(items), record["status"])
    return {"status": record["status"], "message": message, "items": len(items)}


def main() -> None:
    import argparse
    import asyncio
    cli = argparse.ArgumentParser(prog="python -m src.initiative.gremlin_checkin")
    cli.add_argument("--preview", action="store_true", help="write the message but don't send it (ignores the hour)")
    cli.add_argument("--now", action="store_true", help="send it now (ignores the hour and today's check-in)")
    args = cli.parse_args()
    from src.constants import DATA_DIR
    result = asyncio.run(run_once(data_dir=Path(DATA_DIR), model_free=True, send=not args.preview,
                                  force=args.preview or args.now))
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
