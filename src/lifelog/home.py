"""Life log from Home Assistant: where Anthon is and what his phone says (no extra app).

Polls Home Assistant (through Mabel's own integration) every few minutes and
turns states into spans, using HA's ``last_changed`` as the span start:
- place: the person entity (home / a zone / away) and, if enabled in the
  Companion app, the geocoded address;
- steps, detected activity, current/last used app and phone media, from
  whichever Companion app sensors are enabled (they're discovered by name);
- messages: every notification from WhatsApp, Messenger and Discord (or the apps
  in ``lifelog_message_apps``), read from the Companion app's "Last notification"
  sensor *history*, since the sensor itself only holds the newest one. Chats named
  in ``lifelog_exclude_chats`` are skipped.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

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


MESSAGE_APPS = {"com.whatsapp": "WhatsApp", "com.whatsapp.w4b": "WhatsApp Business",
                "com.facebook.orca": "Messenger", "com.facebook.mlite": "Messenger", "com.discord": "Discord"}
_SUMMARY = re.compile(r"^\d+ (new )?messages?( from \d+ chats?)?$|^checking for new messages", re.I)
_message_cursor: dict[str, str] = {}


def notification_entities(states: list[dict]) -> list[str]:
    return [s["entity_id"] for s in states
            if s["entity_id"].startswith("sensor.") and s["entity_id"].endswith("last_notification")]


def _history(ha: dict, entities: list[str], since: datetime, now: datetime, get=None) -> list[dict]:
    """Every state change of ``entities`` since ``since``, with attributes."""
    if get is None:
        import httpx
        get = httpx.get
    response = get(f"{ha['base_url'].rstrip('/')}/api/history/period/{since.isoformat()}",
                   params={"filter_entity_id": ",".join(entities), "end_time": now.isoformat(),
                           "significant_changes_only": "0"},
                   headers={"Authorization": f"Bearer {ha['api_key']}"}, timeout=20)
    if response.status_code != 200:
        return []
    return [state for series in response.json() for state in series]


def message_spans(changes: list[dict], *, apps: dict | None = None, exclude_chats=()) -> list[dict]:
    """Notification history → message spans (one per notification, deduplicated by app/chat/text/time)."""
    apps = apps or MESSAGE_APPS
    skip = {c.strip().lower() for c in exclude_chats if str(c).strip()}
    out, seen = [], set()
    for change in changes:
        attrs = change.get("attributes") or {}
        app = apps.get(str(attrs.get("package") or ""))
        if not app or attrs.get("is_ongoing") is True:
            continue
        chat = str(attrs.get("android.conversationTitle") or attrs.get("android.title") or "").strip()
        sender = str(attrs.get("android.title") or "").strip()
        text = str(attrs.get("android.text") or attrs.get("android.bigText") or change.get("state") or "").strip()
        if not text or text in ("unknown", "unavailable") or _SUMMARY.search(text) or chat.lower() == app.lower():
            continue
        if chat.lower() in skip or sender.lower() in skip:
            continue
        at = change.get("last_changed") or change.get("last_updated")
        if not at:
            continue
        key = (app, chat, text, at[:16])
        if key in seen:
            continue
        seen.add(key)
        detail = {"text": " ".join(text.split())[:200]}
        if sender and sender != chat:
            detail["sender"] = sender[:120]
        if attrs.get("android.isGroupConversation"):
            detail["group"] = True
        digest = hashlib.sha1(f"{chat}\n{text}".encode()).hexdigest()[:10]
        out.append({"id": f"msg:{app}:{at}:{digest}", "kind": "message",
                    "start": at, "end": at, "title": chat[:200] or sender[:200] or app, "app": app,
                    "detail": detail})
    return out


def _message_apps() -> dict:
    from src.settings import get_setting
    extra = get_setting("lifelog_message_apps", {}) or {}
    return {**MESSAGE_APPS, **{str(k): str(v) for k, v in extra.items()}} if isinstance(extra, dict) else MESSAGE_APPS


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
                    entities = notification_entities(states)
                    if entities:
                        now = datetime.now(timezone.utc)
                        key = ",".join(entities)
                        since = datetime.fromisoformat(_message_cursor[key]) if key in _message_cursor \
                            else now - timedelta(hours=1)
                        changes = await asyncio.to_thread(_history, ha, entities, since - timedelta(minutes=2), now)
                        spans += message_spans(changes, apps=_message_apps(),
                                               exclude_chats=get_setting("lifelog_exclude_chats", []) or [])
                        _message_cursor[key] = now.isoformat()
                    if spans:
                        default_store().ingest("phone", spans, source="home_assistant",
                                               exclude_apps=get_setting("lifelog_exclude_apps", []) or [])
            if loop.time() - last_prune > 24 * 3600:
                default_store().prune(int(get_setting("lifelog_retention_days", 30) or 30))
                last_prune = loop.time()
        except Exception:
            logger.warning("[lifelog] Home Assistant poll failed", exc_info=True)
        await asyncio.sleep(POLL_S)
