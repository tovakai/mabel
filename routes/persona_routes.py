"""Persona sidebar: view and edit Mabel's Phase 01 identity modules and mood.

Reading is for any signed-in user. Changing the stitching mode, editing a
module or pinning a mood is admin-only (single-user installs count as admin).
Module edits are validated with the same parser that serves them, so an edit
the composer would reject is refused here (422) instead of being saved.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth_helpers import get_current_user, require_user

_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")


class ModeBody(BaseModel):
    mode: str


class ModuleBody(BaseModel):
    text: str = Field(max_length=12000)


class WhisperBody(BaseModel):
    topic: str = Field(max_length=200)
    thought: str = Field(max_length=1000)
    reference: str = Field(default="", max_length=600)
    days: float = Field(default=14, gt=0, le=90)


class AutonomyBody(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    presence_entity: str | None = Field(default=None, max_length=120)


class NewsTopicBody(BaseModel):
    name: str
    search: str = ""
    feed: str = ""


class NewsBody(BaseModel):
    enabled: bool | None = None


class OutreachBody(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None


class DecisionBody(BaseModel):
    status: str
    reason: str = Field(default="", max_length=300)


class OverrideBody(BaseModel):
    mood: str
    hours: float = Field(default=2.0, gt=0, le=24)


def _is_admin(request: Request) -> bool:
    from src.tool_security import owner_is_admin_or_single_user
    return bool(owner_is_admin_or_single_user(get_current_user(request)))


def _require_admin(request: Request) -> None:
    if not _is_admin(request):
        raise HTTPException(403, "Only an admin can change Mabel's persona")


def _now() -> datetime:
    from src.user_time import user_timezone
    return datetime.now(user_timezone())


def _module_path(root: Path, kind: str, name: str) -> Path:
    if kind == "core":
        if name != "core":
            raise HTTPException(404, "Unknown module")
        return root / "core.md"
    folder = {"function": "functions", "mood": "moods"}.get(kind)
    if not folder or not _NAME.fullmatch(name):
        raise HTTPException(404, "Unknown module")
    return root / folder / f"{name}.md"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_REPO = Path(__file__).resolve().parents[1]
_reflect_task: dict = {}


def setup_persona_routes() -> APIRouter:
    router = APIRouter(prefix="/api/persona", tags=["persona"])

    def _composer():
        from src.persona import composer
        return composer()

    @router.get("")
    async def overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.persona import current_mode
        from src.persona import affect as affect_mod
        from src.persona.affect import SCHEDULE, scheduled_mood
        from src.persona.whisper import COOLDOWN_HOURS
        comp = _composer()
        local = _now()
        projection, state = comp.affect.project(local)
        library = comp.modules.load()
        root = comp.modules.root

        def entry(module, kind):
            path = Path(module.path)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            return {"id": module.id, "kind": kind, "name": module.id.split(".", 1)[-1],
                    "version": module.version, "sha": module.sha[:12], "ref": module.ref,
                    "mood_influence": module.meta.get("mood_influence"), "text": text}

        modules = []
        if library.core:
            modules.append(entry(library.core, "core"))
        modules += [entry(m, "function") for m in library.functions.values()]
        modules += [entry(m, "mood") for m in library.moods.values()]
        current = state.get("projection") or {}
        return {
            "mode": current_mode(),
            "can_manage": _is_admin(request),
            "module_root": str(root),
            "mood": {"current": projection.mood, "source": projection.source, "since": projection.since,
                     "energy": projection.energy, "scheduled": scheduled_mood(local),
                     "manual_until": current.get("manual_until") if projection.source == "manual" else None,
                     "local_time": local.strftime("%H:%M"),
                     "schedule": [{"from": f"{int(h):02d}:{int(round((h % 1) * 60)):02d}", "mood": m}
                                  for h, m in SCHEDULE]},
            "activity": {
                "conversation_tokens": round(state["activity"].get("conversation", 0)),
                "background_tokens": round(state["activity"].get("background", 0)),
                "last_interaction_at": state.get("last_interaction_at"),
                "gap_hours": round(projection.gap_hours, 1) if projection.gap_hours is not None else None,
                "rules": {"warm_up": affect_mod.WARM_UP_TOKENS, "lively": affect_mod.LIVELY_TOKENS,
                          "busy_background": affect_mod.BUSY_BACKGROUND_TOKENS},
            },
            "whisper": comp.whispers.active(local),
            "whisper_servable": comp.whispers.servable(local) is not None,
            "whisper_cooldown_hours": COOLDOWN_HOURS,
            "last_composition": comp.last.summary() if comp.last else None,
            "modules": modules,
            "errors": library.errors,
        }

    @router.put("/mode")
    async def set_mode(body: ModeBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.persona.composer import MODES
        mode = body.mode.strip().lower()
        if mode not in MODES:
            raise HTTPException(422, f"mode must be one of {', '.join(MODES)}")
        from src.settings import load_settings, save_settings
        current = dict(load_settings())
        current["persona_mode"] = mode
        save_settings(current)
        return {"mode": mode}

    @router.put("/modules/{kind}/{name}")
    async def save_module(kind: str, name: str, body: ModuleBody, request: Request,
                          _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.persona.modules import ModuleError, parse_module
        root = _composer().modules.root
        path = _module_path(root, kind, name)
        expected_id = "core" if kind == "core" else f"{kind}.{name}"
        try:
            module = parse_module(body.text, kind=kind, path=str(path))
        except ModuleError as error:
            raise HTTPException(422, str(error)) from None
        if module.id != expected_id:
            raise HTTPException(422, f"id must be {expected_id}")
        if path.is_symlink():
            raise HTTPException(422, "refusing to write through a symlink")
        _atomic_write(path, body.text if body.text.endswith("\n") else body.text + "\n")
        return {"ref": module.ref, "version": module.version}

    @router.put("/whisper")
    async def seed_whisper(body: WhisperBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.persona.whisper import WhisperError
        try:
            return _composer().whispers.seed(body.topic, body.thought, reference=body.reference,
                                             days=body.days, now=_now())
        except WhisperError as error:
            raise HTTPException(422, str(error)) from None

    @router.delete("/whisper")
    async def retire_whisper(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        _composer().whispers.retire(_now())
        return {"retired": True}

    # --- Research (Phase 3 Curiosity Drive) ------------------------------------------------

    def _research_paths():
        from src.curiosity.session import Paths
        return Paths()

    @router.get("/research")
    async def research_overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.curiosity import runner
        from src.curiosity.backlog import Backlog, articles
        paths = _research_paths()
        now = datetime.now().astimezone().isoformat()
        listed = []
        for item in sorted(articles(paths.articles), key=lambda a: str(a.get("created") or ""), reverse=True):
            listed.append({"file": item["path"].name, "title": item["path"].stem, "subject": item.get("subject"),
                           "question": item.get("question"), "why": item.get("why"), "status": item.get("status"),
                           "created": str(item.get("created") or ""), "chosen_from": item.get("chosen_from")})
        log = Backlog(paths.data / "backlog.json").load()
        questions = [{"question": q["question"], "subject": q["subject"], "eligible_after": q["eligible_after"],
                      "cooling": q["eligible_after"] > now} for q in log["questions"] if q["status"] == "open"]
        return {"can_manage": _is_admin(request), "status": runner.status(), "articles": listed,
                "open_questions": questions[::-1][:20],
                "spawned": [s["name"] for s in log["spawned"] if s["status"] == "open"][::-1][:20]}

    @router.get("/research/article")
    async def research_article(file: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        folder = _research_paths().articles.resolve()
        path = (folder / file).resolve()
        if path.parent != folder or path.suffix != ".md" or not path.is_file() or path.is_symlink():
            raise HTTPException(404, "Unknown article")
        text = path.read_text(encoding="utf-8")
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text else text
        return {"file": path.name, "markdown": body}

    @router.post("/research/run")
    async def research_run(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.curiosity import runner
        try:
            return runner.start()
        except runner.AlreadyRunning as error:
            raise HTTPException(409, str(error)) from None

    # --- Dottie's inbox (Phase 4) -----------------------------------------------------------

    @router.get("/inbox")
    async def inbox_overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.subconscious import loops
        from src.subconscious.inbox import default_inbox
        inbox = default_inbox()
        decided = [i for s in ("accepted", "rejected") for i in inbox.list(status=s, limit=15)]
        decided.sort(key=lambda i: i.get("decided_at") or "", reverse=True)
        return {"can_manage": _is_admin(request), "running": loops.running(), "last_runs": loops.last_runs(),
                "counts": inbox.counts(), "items": inbox.list(limit=60), "decided": decided[:15]}

    @router.post("/inbox/{item_id}/decide")
    async def inbox_decide(item_id: str, body: DecisionBody, request: Request,
                           _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.subconscious.inbox import InboxError, default_inbox
        if body.status not in ("accepted", "rejected"):
            raise HTTPException(422, "status must be accepted or rejected")
        try:
            return default_inbox().decide(item_id, body.status, by="user", reason=body.reason)
        except InboxError as error:
            raise HTTPException(404, str(error)) from None

    @router.post("/inbox/run")
    async def inbox_run(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.subconscious import loops
        if not loops.start():
            raise HTTPException(409, "Dottie is already working")
        return {"running": True}

    # --- Autonomy / heartbeat (Phase 5) -----------------------------------------------------

    @router.get("/autonomy")
    async def autonomy_overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        import asyncio
        from src.curiosity import runner
        from src.heartbeat import daemon, policy, signals
        from src.settings import get_setting
        sig = await asyncio.to_thread(signals.collect)
        state = daemon.load_state()
        preview = policy.decide(sig, state, now=datetime.now().astimezone(),
                                enabled=bool(get_setting("autonomy_enabled", True)),
                                paused=bool(get_setting("autonomy_paused", False)),
                                research_running=bool(runner.status().get("running")))
        return {"can_manage": _is_admin(request), **daemon.status(), "signals": sig.as_dict(),
                "decision_now": preview.as_dict(), "autonomous_running": runner.is_autonomous_running(),
                "presence_entity": str(get_setting("heartbeat_presence_entity", "") or "")}

    @router.put("/autonomy")
    async def autonomy_update(body: AutonomyBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.settings import load_settings, save_settings
        current = dict(load_settings())
        if body.enabled is not None:
            current["autonomy_enabled"] = body.enabled
        if body.paused is not None:
            current["autonomy_paused"] = body.paused
        if body.presence_entity is not None:
            entity = body.presence_entity.strip()
            if entity and not re.fullmatch(r"(?:person|device_tracker|binary_sensor)\.[a-z0-9_]+", entity):
                raise HTTPException(422, "entity must look like person.name or device_tracker.phone")
            current["heartbeat_presence_entity"] = entity
        save_settings(current)
        if body.paused or body.enabled is False:
            from src.curiosity import runner
            runner.yield_now("paused by you")
        return {"enabled": current.get("autonomy_enabled", True), "paused": current.get("autonomy_paused", False),
                "presence_entity": current.get("heartbeat_presence_entity", "")}

    @router.get("/timeline")
    async def timeline(request: Request, days: int = 7, _user: str = Depends(require_user)) -> dict[str, Any]:
        import asyncio
        from src.constants import DATA_DIR
        from src.persona.timeline import build
        items = await asyncio.to_thread(build, data_dir=Path(DATA_DIR), days=max(1, min(int(days), 30)))
        return {"items": items}

    # --- Reaching out (Phase 6) ---------------------------------------------------------------

    @router.get("/outreach")
    async def outreach_overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        import asyncio
        from src.curiosity import runner
        from src.heartbeat import signals
        from src.initiative import outreach
        from src.settings import get_setting
        data = outreach.default_store().load()
        model_free = (not signals.any_chat_active() and not runner.status().get("running")
                      and await asyncio.to_thread(signals.strata_up))
        has_phone = bool(outreach.home_assistant())
        blocked = outreach.gates(data, now=datetime.now().astimezone(), enabled=bool(get_setting("initiative_enabled", True)),
                                 paused=bool(get_setting("initiative_paused", False)), model_free=model_free,
                                 has_phone=has_phone)
        return {"can_manage": _is_admin(request), "enabled": bool(get_setting("initiative_enabled", True)),
                "paused": bool(get_setting("initiative_paused", False)), "gates": blocked,
                "limits": {"max_per_day": outreach.MAX_PER_DAY, "min_gap_hours": outreach.MIN_GAP.total_seconds() / 3600,
                           "cooldown_hours": outreach.COOLDOWN.total_seconds() / 3600},
                "sent": data["sent"][-10:][::-1], "reviews": data["reviews"][-10:][::-1],
                "last_result": outreach.last_result, "has_phone": has_phone}

    @router.put("/outreach")
    async def outreach_update(body: OutreachBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.settings import load_settings, save_settings
        current = dict(load_settings())
        if body.enabled is not None:
            current["initiative_enabled"] = body.enabled
        if body.paused is not None:
            current["initiative_paused"] = body.paused
        save_settings(current)
        return {"enabled": current.get("initiative_enabled", True), "paused": current.get("initiative_paused", False)}

    @router.post("/outreach/consider")
    async def outreach_consider(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.initiative import outreach
        if not outreach.consider_now():
            raise HTTPException(409, "She's already thinking about it")
        return {"started": True}

    # --- The daily news -----------------------------------------------------------------------

    def _news_dir():
        from src.constants import DATA_DIR
        return Path(DATA_DIR)

    @router.get("/news")
    async def news_overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.persona import news
        from src.settings import get_setting
        state = news.load_state(_news_dir())
        return {"can_manage": _is_admin(request), "enabled": bool(get_setting("news_enabled", True)),
                "running": news.running(), "hour": int(get_setting("news_hour", news.DEFAULT_HOUR)),
                "topics": state["topics"], "last_error": state.get("last_error"),
                "history": (state.get("history") or [])[-5:][::-1]}

    @router.put("/news")
    async def news_update(body: NewsBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.settings import load_settings, save_settings
        current = dict(load_settings())
        if body.enabled is not None:
            current["news_enabled"] = body.enabled
        save_settings(current)
        return {"enabled": current.get("news_enabled", True)}

    @router.post("/news/topics")
    async def news_add_topic(body: NewsTopicBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.persona import news
        try:
            return news.add_topic(_news_dir(), body.name, feed=body.feed, search=body.search)
        except news.NewsError as exc:
            raise HTTPException(400, str(exc))

    @router.delete("/news/topics/{topic_id}")
    async def news_remove_topic(topic_id: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        from src.persona import news
        if not news.remove_topic(_news_dir(), topic_id):
            raise HTTPException(404, "No such topic")
        return {"removed": True}

    # --- Self-reflection ----------------------------------------------------------------------

    @router.get("/reflections")
    async def reflections(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.persona.reflection import EVERY, SELF_DIR, last_reflection
        folder = _REPO.joinpath(*SELF_DIR)
        last = last_reflection()
        return {"can_manage": _is_admin(request), "dates": [p.stem for p in sorted(folder.glob("*.md"), reverse=True)[:30]],
                "every_days": EVERY.days, "last": last.isoformat() if last else None,
                "running": bool(_reflect_task.get("task") and not _reflect_task["task"].done()),
                "last_result": _reflect_task.get("result")}

    @router.get("/reflection/{date}")
    async def reflection(date: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.persona.reflection import SELF_DIR
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise HTTPException(404, "Unknown reflection")
        path = _REPO.joinpath(*SELF_DIR, f"{date}.md")
        if not path.is_file():
            raise HTTPException(404, "Unknown reflection")
        text = path.read_text(encoding="utf-8")
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text else text
        return {"date": date, "markdown": body}

    @router.post("/reflect")
    async def reflect_now(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        import asyncio
        if _reflect_task.get("task") and not _reflect_task["task"].done():
            raise HTTPException(409, "She's already reflecting")

        async def run():
            from src.persona.reflection import reflect
            try:
                _reflect_task["result"] = await reflect()
            except Exception as error:
                _reflect_task["result"] = {"status": "failed", "error": str(error)[:200]}
        _reflect_task["task"] = asyncio.get_running_loop().create_task(run())
        return {"started": True}

    @router.post("/mood-override")
    async def pin_mood(body: OverrideBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        comp = _composer()
        if body.mood not in comp.modules.load().moods:
            raise HTTPException(422, "Unknown mood")
        local = _now()
        projection = comp.affect.set_override(body.mood, local + timedelta(hours=body.hours), local)
        return {"mood": projection.mood, "source": projection.source}

    @router.delete("/mood-override")
    async def clear_mood(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        _require_admin(request)
        projection = _composer().affect.clear_override(_now())
        return {"mood": projection.mood, "source": projection.source}

    return router
