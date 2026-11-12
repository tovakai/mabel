"""Life log API: collectors send activity here; the Persona sidebar reads it.

Collectors authenticate with an API token carrying the ``lifelog:write`` scope
(create one per device under Settings -> API tokens, profile "lifelog_collector").
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from src.auth_helpers import get_current_user, require_api_token_scope, require_user

_REPO = Path(__file__).resolve().parents[1]


class IngestBody(BaseModel):
    device: str = Field(max_length=40)
    spans: list[dict] = Field(default_factory=list)


class GameModeBody(BaseModel):
    enabled: bool | None = None
    awake_appids: list[str] | None = None        # games she stays loaded for (game mode's ignore_appids)


GAMES_DAYS = 60


def _local_device() -> str:
    """The name this PC's own collector reports under (game mode only sees games on this PC, not the Frame's)."""
    import json
    import os
    path = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "mabel-collector" / "config.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("device") or "desktop")
    except (OSError, ValueError, AttributeError):
        return "desktop"


def _played_games(store, now: datetime, device: str | None = None) -> list[dict]:
    """Games a collector has seen lately, one per Steam app id, most recently played first."""
    games: dict[str, dict] = {}
    for s in store.spans(now - timedelta(days=GAMES_DAYS), now, kinds=["game"]):
        if device and s.get("device") != device:
            continue
        appid = str((s.get("detail") or {}).get("appid") or "").strip()
        if not appid:
            continue
        g = games.setdefault(appid, {"appid": appid, "title": "", "minutes": 0.0, "last": ""})
        start, end = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
        g["minutes"] += max(0.0, (end - start).total_seconds() / 60)
        if s["end"] >= g["last"]:
            g["last"], g["title"] = s["end"], s["title"] or g["title"] or f"Steam app {appid}"
    return [{**g, "minutes": round(g["minutes"])} for g in sorted(games.values(), key=lambda g: g["last"], reverse=True)]


class LifelogSettings(BaseModel):
    paused: bool | None = None
    exclude_sites: list[str] | None = None
    exclude_apps: list[str] | None = None


def _setting(key, default):
    from src.settings import get_setting
    return get_setting(key, default)


def setup_lifelog_routes() -> APIRouter:
    router = APIRouter(prefix="/api/lifelog", tags=["lifelog"])

    def _is_admin(request: Request) -> bool:
        from src.tool_security import owner_is_admin_or_single_user
        return bool(owner_is_admin_or_single_user(get_current_user(request)))

    @router.post("/ingest")
    async def ingest(body: IngestBody, request: Request) -> dict[str, Any]:
        owner = require_api_token_scope(request, "lifelog:write")
        if not owner:
            raise HTTPException(401, "Sign in or use a lifelog:write API token")
        if not _setting("lifelog_enabled", True):
            raise HTTPException(503, "The life log is switched off")
        if _setting("lifelog_paused", False):
            return {"stored": 0, "skipped": len(body.spans), "paused": True}
        from src.lifelog.store import LifelogError, default_store
        try:
            return default_store().ingest(body.device, body.spans, token_id=getattr(request.state, "api_token_id", None),
                                          exclude_sites=_setting("lifelog_exclude_sites", []) or [],
                                          exclude_apps=_setting("lifelog_exclude_apps", []) or [])
        except LifelogError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/collector.py", response_class=PlainTextResponse)
    async def collector_script(request: Request) -> str:
        require_api_token_scope(request, "lifelog:write")
        return (_REPO / "scripts" / "lifelog_collector.py").read_text(encoding="utf-8")

    @router.get("/overview")
    async def overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.lifelog.store import default_store
        from src.lifelog.summary import day_bounds, summarize
        store = default_store()
        start, end = day_bounds(datetime.now().astimezone())
        return {"can_manage": _is_admin(request), "enabled": bool(_setting("lifelog_enabled", True)),
                "paused": bool(_setting("lifelog_paused", False)), "devices": store.devices(),
                "exclude_sites": _setting("lifelog_exclude_sites", []) or [],
                "exclude_apps": _setting("lifelog_exclude_apps", []) or [],
                "today": summarize(store.spans(start, end), start=start, end=end),
                "retention_days": int(_setting("lifelog_retention_days", 30) or 30),
                "collector_path": str(_REPO / "scripts" / "lifelog_collector.py"),
                "day_start": start.isoformat()}

    @router.get("/days")
    async def days(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        folder = _REPO / "journal" / "anthon"
        return {"days": [p.stem for p in sorted(folder.glob("*.md"), reverse=True)[:30]]}

    @router.get("/day/{date}")
    async def day(date: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            raise HTTPException(404, "Unknown day")
        path = _REPO / "journal" / "anthon" / f"{date}.md"
        if not path.is_file():
            raise HTTPException(404, "Not written yet")
        text = path.read_text(encoding="utf-8")
        body = text.split("\n---\n", 1)[1] if text.startswith("---\n") and "\n---\n" in text else text
        return {"date": date, "markdown": body}

    @router.put("/settings")
    async def update(body: LifelogSettings, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        if not _is_admin(request):
            raise HTTPException(403, "Only an admin can change the life log")
        from src.settings import load_settings, save_settings
        current = dict(load_settings())
        if body.paused is not None:
            current["lifelog_paused"] = body.paused
        if body.exclude_sites is not None:
            current["lifelog_exclude_sites"] = sorted({s for s in (re.sub(r"[^a-z0-9.-]", "", x.lower().strip())
                                                                    for x in body.exclude_sites) if s})[:200]
        if body.exclude_apps is not None:
            current["lifelog_exclude_apps"] = sorted({x.strip()[:80] for x in body.exclude_apps if x.strip()})[:200]
        save_settings(current)
        return {"paused": current.get("lifelog_paused", False), "exclude_sites": current.get("lifelog_exclude_sites", []),
                "exclude_apps": current.get("lifelog_exclude_apps", [])}

    @router.get("/games")
    async def games(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        """Game mode: which games unload her model, and which she stays awake for."""
        import json
        from src import gpu_swap
        from src.lifelog.store import default_store
        cfg = gpu_swap.game_mode_settings()
        awake = {str(a) for a in cfg["ignore_appids"]}
        played = _played_games(default_store(), datetime.now(timezone.utc), device=_local_device())
        known = {g["appid"] for g in played}
        played += [{"appid": a, "title": f"Steam app {a}", "minutes": 0, "last": ""} for a in sorted(awake - known)]
        try:
            resting = json.loads((gpu_swap._data_dir() / "gpu_swap" / "game_mode.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            resting = None
        return {"can_manage": _is_admin(request), "enabled": bool(cfg["enabled"]),
                "games": [{**g, "awake": g["appid"] in awake} for g in played],
                "resting": {"game": resting.get("game"), "since": resting.get("since")} if resting else None}

    @router.put("/games")
    async def update_games(body: GameModeBody, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        if not _is_admin(request):
            raise HTTPException(403, "Only an admin can change game mode")
        from src import gpu_swap
        changes: dict[str, Any] = {}
        if body.enabled is not None:
            changes["enabled"] = body.enabled
        if body.awake_appids is not None:
            ids = {a.strip() for a in body.awake_appids if a.strip().isdigit() and len(a.strip()) <= 20}
            changes["ignore_appids"] = sorted(ids, key=int)[:200]
        cfg = gpu_swap.update_game_mode(**changes) if changes else gpu_swap.game_mode_settings()
        return {"enabled": bool(cfg["enabled"]), "awake_appids": [str(a) for a in cfg["ignore_appids"]]}

    return router
