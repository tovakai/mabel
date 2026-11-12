"""Turn raw life-log spans into a compact picture of a day (deterministic, no model)."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timedelta, timezone

DAY_START_HOUR = 6          # night owl: a "day" runs 06:00 -> 06:00 local
TOP = 12
MESSAGE_CHATS = 15          # busiest chats in a day account
MESSAGE_PREVIEWS = 6        # latest previews per chat


def day_bounds(local_moment: datetime, start_hour: int = DAY_START_HOUR) -> tuple[datetime, datetime]:
    """The 06:00 -> 06:00 local window that contains ``local_moment``."""
    local_moment = local_moment.astimezone()
    start = datetime.combine(local_moment.date(), time(start_hour), tzinfo=local_moment.tzinfo)
    if local_moment < start:
        start -= timedelta(days=1)
    return start, start + timedelta(days=1)


def _minutes(span: dict, start: datetime, end: datetime) -> float:
    a = max(datetime.fromisoformat(span["start"]), start.astimezone(timezone.utc))
    b = min(datetime.fromisoformat(span["end"]), end.astimezone(timezone.utc))
    return max(0.0, (b - a).total_seconds() / 60)


def _top(totals: dict, n: int = TOP) -> list[dict]:
    return [{"name": k, "minutes": round(v)} for k, v in sorted(totals.items(), key=lambda kv: -kv[1])[:n] if v >= 1]


def _clipped(iso: str, start: datetime, end: datetime) -> datetime:
    """A span edge as local time, held inside the day window (spans can begin before it or still be open)."""
    return min(max(datetime.fromisoformat(iso), start.astimezone(timezone.utc)), end.astimezone(timezone.utc)).astimezone()


def not_games() -> dict[str, str]:
    """Steam app ids that are launched like games but aren't (e.g. a desktop environment on the Steam Frame),
    mapped to what to call them. Setting ``lifelog_not_games``: {"<appid>": "<label>"}."""
    try:
        from src.settings import get_setting
        value = get_setting("lifelog_not_games", {}) or {}
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
    except Exception:
        return {}


def summarize(spans: list[dict], *, start: datetime, end: datetime, relabel: dict[str, str] | None = None) -> dict:
    relabel = not_games() if relabel is None else relabel
    apps, games, media, phone_apps, activity = (defaultdict(float) for _ in range(5))
    sites: dict[str, int] = defaultdict(int)
    titles: list[str] = []
    places: list[dict] = []
    devices: dict[str, dict] = {}
    steps = 0
    chats: dict[tuple, dict] = {}
    for s in spans:
        mins = _minutes(s, start, end)
        local_start = _clipped(s["start"], start, end)
        local_end = _clipped(s["end"], start, end)
        dev = devices.setdefault(s["device"], {"device": s["device"], "first": local_start, "last": local_start,
                                               "active_minutes": 0.0})
        dev["first"] = min(dev["first"], local_start)
        dev["last"] = max(dev["last"], local_end)
        kind = s["kind"]
        appid = str((s.get("detail") or {}).get("appid") or "") if kind == "game" else ""
        if appid and appid in relabel:                 # not a game: count it as time in that app
            s = {**s, "kind": "focus", "app": relabel[appid]}
            kind = "focus"
        if kind == "focus":
            apps[s["app"] or "unknown"] += mins
            dev["active_minutes"] += mins
        elif kind == "browse":
            if s["site"]:
                sites[s["site"]] += 1
            label = f"{s['title']} ({s['site']})" if s["title"] else s["site"]
            if label and label not in titles:
                titles.append(label)
        elif kind == "game":
            games[s["title"] or s["app"] or "a game"] += mins
            dev["active_minutes"] += mins
        elif kind == "media":
            media[s["title"] or "something"] += mins
        elif kind == "phone_app":
            phone_apps[s["title"] or s["app"] or "an app"] += mins
        elif kind == "activity":
            activity[s["title"] or "unknown"] += mins
        elif kind == "place":
            places.append({"place": s["title"], "from": local_start.strftime("%H:%M"),
                           "to": local_end.strftime("%H:%M"),
                           "minutes": round(mins)})
        elif kind == "message":
            chat = chats.setdefault((s["app"], s["title"]), {"app": s["app"], "chat": s["title"], "count": 0,
                                                             "first": local_start, "last": local_start, "messages": []})
            chat["count"] += 1
            chat["first"], chat["last"] = min(chat["first"], local_start), max(chat["last"], local_start)
            detail = s.get("detail") or {}
            if detail.get("text"):
                who = f"{detail['sender']}: " if detail.get("sender") else ""
                chat["messages"].append((local_start, f"{who}{detail['text']}"))
        elif kind == "steps":
            try:
                steps = max(steps, int(float(s["detail"].get("steps", s["title"] or 0))))
            except (TypeError, ValueError):
                pass
    return {
        "devices": [{**d, "first": d["first"].strftime("%H:%M"), "last": d["last"].strftime("%H:%M"),
                     "active_minutes": round(d["active_minutes"])} for d in devices.values()],
        "apps": _top(apps), "games": _top(games), "media": _top(media), "phone_apps": _top(phone_apps),
        "activity": _top(activity), "sites": [{"name": k, "visits": v} for k, v in
                                               sorted(sites.items(), key=lambda kv: -kv[1])[:TOP]],
        "pages": titles[:40], "places": places[:30], "steps": steps,
        "messages": [{"app": c["app"], "chat": c["chat"], "count": c["count"], "from": c["first"].strftime("%H:%M"),
                      "to": c["last"].strftime("%H:%M"),
                      "previews": [t for _, t in sorted(c["messages"])][-MESSAGE_PREVIEWS:]}
                     for c in sorted(chats.values(), key=lambda c: -c["count"])[:MESSAGE_CHATS]],
        "span_count": len(spans),
    }
