"""/api/beeper/*: Beeper Desktop's messaging API for MABEL-OS's INBOX (src/beeper.py has the details).

Browser sessions, or an API token with the "messages" scope (token profile "tui"). Only the
endpoints in src.beeper.ALLOWED pass; Beeper's own token is added here and never leaves the PC.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from src import beeper
from src.auth_helpers import require_api_token_scope, require_user

logger = logging.getLogger(__name__)


def messages_user(request: Request) -> str:
    if getattr(request.state, "api_token", False):
        return require_api_token_scope(request, "messages") or ""
    return require_user(request)


def setup_beeper_routes() -> APIRouter:
    router = APIRouter(tags=["beeper"])
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))

    @router.get("/api/beeper/status")
    async def status(_user=Depends(messages_user)):
        cfg = beeper.config()
        out = {"url": cfg["url"], "token": bool(cfg["token"]), "reachable": False, "version": None, "accounts": None}
        try:
            info = (await client.get(f"{cfg['url']}/v1/info", timeout=4)).json()
            out.update(reachable=True, version=info.get("app", {}).get("version"))
            if cfg["token"]:
                r = await client.get(f"{cfg['url']}/v1/accounts", headers={"Authorization": f"Bearer {cfg['token']}"})
                if r.status_code == 200:
                    data = r.json()
                    out["accounts"] = data if isinstance(data, list) else data.get("items", [])
                else:
                    out["token_error"] = r.status_code
        except Exception as exc:
            out["error"] = str(exc)[:200]
        return out

    @router.api_route("/api/beeper/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def relay(path: str, request: Request, _user=Depends(messages_user)):
        if not beeper.allowed(request.method, path):
            raise HTTPException(403, f"not relayed: {request.method} /{path}")
        cfg = beeper.config()
        if not cfg["token"]:
            raise HTTPException(503, "Beeper isn't connected yet: python -m src.beeper --set-token on the PC")
        try:
            r = await client.request(request.method, f"{cfg['url']}/{path.strip('/')}",
                                     params=list(request.query_params.multi_items()),
                                     content=await request.body() if request.method in ("POST", "PUT") else None,
                                     headers={"Authorization": f"Bearer {cfg['token']}",
                                              "Content-Type": request.headers.get("content-type", "application/json")})
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Beeper Desktop isn't answering at {cfg['url']}: {exc}")
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                return JSONResponse(r.json(), status_code=r.status_code)
            except ValueError:
                pass
        return Response(r.content, status_code=r.status_code, media_type=ctype or None)

    return router
