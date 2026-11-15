"""Music: Mabel's player talks to Navidrome through here (src/navidrome.py).

- GET  /api/music/status            connected? server version
- POST /api/music/config            connect (admin; tests the login before saving)
- DELETE /api/music/config          disconnect (admin)
- GET  /api/music/rest/<method>     allow-listed Subsonic reads, JSON body of subsonic-response
- POST /api/music/rest/<method>     allow-listed writes (star, scrobble, playlists)
- GET  /api/music/cover/<id>        cover art bytes
- GET  /api/music/stream/<id>       audio, Range passed through so seeking works

Stream and cover are "passive" (src/interactive_gate.py) and exempt from the
request timeout (app.py): a song playing must not pause her background work.
"""
from __future__ import annotations

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from core.middleware import require_admin
from src import navidrome as nd
from src.auth_helpers import require_user

ITEM_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
PASS_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")

_stream_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """Shared client for long-lived streams (no read timeout: a song can pause)."""
    global _stream_client
    if _stream_client is None or _stream_client.is_closed:
        _stream_client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0))
    return _stream_client


class ConfigBody(BaseModel):
    url: str = Field(max_length=300)
    user: str = Field(max_length=120)
    password: str = Field(default="", max_length=300)   # empty: keep the saved one


def _fail(exc: nd.MusicError):
    raise HTTPException(exc.status, str(exc))


def _check_id(item_id: str) -> str:
    if not ITEM_ID.match(item_id or ""):
        raise HTTPException(400, "bad id")
    return item_id


async def _proxy_binary(request: Request, method: str, params: list[tuple[str, str]], *, cache: str):
    cfg = nd.load_config()
    if not cfg.configured:
        raise HTTPException(409, "Music isn't connected to Navidrome yet.")
    headers = {}
    if rng := request.headers.get("range"):
        headers["Range"] = rng
    client = _client()
    req = client.build_request("GET", nd.rest_url(cfg, method),
                               params=list(nd.auth_params(cfg).items()) + params, headers=headers)
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Couldn't reach Navidrome ({type(exc).__name__})") from exc
    ctype = upstream.headers.get("content-type", "")
    if upstream.status_code >= 400 or "json" in ctype or "xml" in ctype:
        # Subsonic reports errors as a JSON/XML body, not as audio.
        body = await upstream.aread()
        await upstream.aclose()
        message = "Navidrome refused"
        try:
            nd.unwrap(httpx.Response(200, content=body).json())
        except nd.MusicError as exc:
            message = str(exc)
        except Exception:
            pass
        raise HTTPException(502 if upstream.status_code < 400 else upstream.status_code, message)
    out = {k: v for k in PASS_HEADERS if (v := upstream.headers.get(k))}
    # Already-final bytes: keep GZipMiddleware off them (it would drop
    # Content-Length, and with it the duration and seeking).
    out["content-encoding"] = "identity"
    out["cache-control"] = cache
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code, headers=out,
                             background=BackgroundTask(upstream.aclose))


def setup_music_routes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/music/status")
    async def status(_user=Depends(require_user)):
        cfg = nd.load_config()
        out = {"configured": cfg.configured, "url": cfg.url, "user": cfg.user}
        if not cfg.configured:
            return out
        try:
            body = await nd.call(cfg, "ping")
            out.update(ok=True, server=body.get("serverVersion") or body.get("version"),
                       open_subsonic=bool(body.get("openSubsonic")))
        except nd.MusicError as exc:
            out.update(ok=False, error=str(exc))
        return out

    @router.post("/api/music/config")
    async def configure(body: ConfigBody, _admin=Depends(require_admin)):
        try:
            url = nd.normalize_url(body.url)
        except nd.MusicError as exc:
            _fail(exc)
        password = body.password or nd.load_config().password
        if not body.user.strip() or not password:
            raise HTTPException(400, "User and password are needed.")
        cfg = nd.Config(url=url, user=body.user.strip(), password=password)
        try:
            ping = await nd.call(cfg, "ping")
        except nd.MusicError as exc:
            _fail(exc)
        nd.save_config(cfg.url, cfg.user, cfg.password)
        return {"ok": True, "server": ping.get("serverVersion") or ping.get("version")}

    @router.delete("/api/music/config")
    async def disconnect(_admin=Depends(require_admin)):
        nd.clear_config()
        return {"ok": True}

    @router.get("/api/music/rest/{method}")
    async def rest_read(method: str, request: Request, _user=Depends(require_user)):
        if method not in nd.READ_METHODS:
            raise HTTPException(404 if method not in nd.WRITE_METHODS else 405, "not allowed")
        try:
            return await nd.call(nd.load_config(), method, nd.clean_params(request.query_params.multi_items()))
        except nd.MusicError as exc:
            _fail(exc)

    @router.post("/api/music/rest/{method}")
    async def rest_write(method: str, request: Request, _user=Depends(require_user)):
        if method not in nd.WRITE_METHODS:
            raise HTTPException(404, "not allowed")
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            raise HTTPException(400, "Expected a JSON object")
        pairs = []
        for key, value in data.items():
            for v in (value if isinstance(value, list) else [value]):
                if v is not None:
                    pairs.append((str(key), str(v).lower() if isinstance(v, bool) else str(v)))
        try:
            return await nd.call(nd.load_config(), method, nd.clean_params(pairs))
        except nd.MusicError as exc:
            _fail(exc)

    @router.get("/api/music/cover/{item_id}")
    async def cover(item_id: str, request: Request, size: int = 300, _user=Depends(require_user)):
        size = max(32, min(int(size), 1200))
        return await _proxy_binary(request, "getCoverArt", [("id", _check_id(item_id)), ("size", str(size))],
                                   cache="private, max-age=604800")

    @router.get("/api/music/stream/{song_id}")
    async def stream(song_id: str, request: Request, maxBitRate: int = 0, _user=Depends(require_user)):
        params = [("id", _check_id(song_id))]
        if maxBitRate > 0:
            params.append(("maxBitRate", str(min(maxBitRate, 320))))
        return await _proxy_binary(request, "stream", params, cache="private, no-store")

    return router
