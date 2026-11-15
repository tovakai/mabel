"""Hub: which local service windows exist and where to frame them (src/hub_services.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from src.auth_helpers import require_user
from src.hub_services import resolve


def setup_hub_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/hub/services")
    async def hub_services(request: Request, _user=Depends(require_user)):
        return {"services": resolve(request.headers.get("host"))}

    return router
