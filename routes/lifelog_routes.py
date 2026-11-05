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

    return router
