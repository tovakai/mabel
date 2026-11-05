"""Life log from Home Assistant: where Anthon is and what his phone says (no extra app).

Polls Home Assistant (through Mabel's own integration) every few minutes and
turns states into spans, using HA's ``last_changed`` as the span start:
- place: the person entity (home / a zone / away) and, if enabled in the
  Companion app, the geocoded address;
- steps, detected activity, current/last used app and phone media, from
  whichever Companion app sensors are enabled (they're discovered by name).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

POLL_S = 5 * 60
SENSORS = (  # (substring in the entity id, span kind)
    ("steps", "steps"), ("detected_activity", "activity"), ("last_used_app", "phone_app"),
    ("current_app", "phone_app"), ("media_session", "media"), ("geocoded_location", "place"),
)


def _states(ha: dict, get=None) -> list[dict]:
    if get is None:
        import httpx
        get = httpx.get
    response = get(f"{ha['base_url'].rstrip('/')}/api/states", headers={"Authorization": f"Bearer {ha['api_key']}"},
                   timeout=10)
    return response.json() if response.status_code == 200 else []


def spans_from_states(states: list[dict], *, person: str, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    trackers = [s["entity_id"].split(".", 1)[1] for s in states if s["entity_id"].startswith("device_tracker.")]
    phones = [t for t in trackers if "phone" in t] or trackers
    out = []
    for s in states:
        eid, state = s["entity_id"], str(s.get("state") or "")
        if state in ("unknown", "unavailable", ""):
            continue
        changed = s.get("last_changed") or now.isoformat()
        if eid == person:
            title = {"home": "home", "not_home": "away from home"}.get(state, state)
            out.append({"id": f"place:person:{changed}", "kind": "place", "start": changed, "end": now.isoformat(),
                        "title": title, "app": "home_assistant"})
            continue
        if not eid.startswith("sensor.") or not any(eid.startswith(f"sensor.{p}_") for p in phones):
            continue
        for needle, kind in SENSORS:
            if needle not in eid:
                continue
            if kind == "steps":
                try:
                    steps = int(float(state))
                except ValueError:
                    break
                day = now.astimezone().strftime("%Y-%m-%d")
                out.append({"id": f"steps:{day}", "kind": "steps", "start": changed, "end": now.isoformat(),
                            "title": str(steps), "detail": {"steps": steps}, "app": "phone"})
            elif kind == "media":
                attrs = s.get("attributes") or {}
                if state.lower() != "playing":
                    break
                title = " – ".join(x for x in (attrs.get("artist"), attrs.get("title")) if x) or state
                out.append({"id": f"media:{eid}:{changed}", "kind": "media", "start": changed, "end": now.isoformat(),
                            "title": title, "app": "phone"})
            elif kind == "place":
                out.append({"id": f"place:address:{changed}", "kind": "place", "start": changed, "end": now.isoformat(),
                            "title": re.sub(r"\s+", " ", state)[:200], "app": "geocoded"})
            else:
                out.append({"id": f"{kind}:{eid}:{changed}", "kind": kind, "start": changed, "end": now.isoformat(),
                            "title": state.replace("_", " ")[:120], "app": "phone"})
            break
    return out


async def poll_forever() -> None:
    from src.initiative.outreach import home_assistant
    from src.lifelog.store import default_store
    from src.settings import get_setting
    await asyncio.sleep(60)
    last_prune = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            if get_setting("lifelog_enabled", True) and not get_setting("lifelog_paused", False):
                ha = home_assistant()
                person = str(get_setting("heartbeat_presence_entity", "") or "")
                if ha:
                    states = await asyncio.to_thread(_states, ha)
                    spans = spans_from_states(states, person=person)
                    if spans:
                        default_store().ingest("phone", spans, source="home_assistant",
                                               exclude_apps=get_setting("lifelog_exclude_apps", []) or [])
            if loop.time() - last_prune > 24 * 3600:
                default_store().prune(int(get_setting("lifelog_retention_days", 30) or 30))
                last_prune = loop.time()
        except Exception:
            logger.warning("[lifelog] Home Assistant poll failed", exc_info=True)
        await asyncio.sleep(POLL_S)
