"""Install MABEL-OS (tui-rs/, the Rust build) on any device of his tailnet with one line:

    curl -fsSL https://tovakai.tail4312a5.ts.net/tui/install.sh | sh

GET  /tui/install.sh                       the installer, with this server's address filled in
GET  /tui/rs/mabel-os-<arch>                 the build for x86_64 or aarch64 (made by scripts/tui_rs_dist.sh)
GET  /tui/version                           the current build ids, for the update check (F12)
GET  /api/tui/whoami                        which tailnet login Tailscale Serve says the caller is
POST /api/tui/pair                          a "tui" profile API token for a device of an allowed login

The builds are made on the PC by scripts/tui_rs_dist.sh (into tui-rs/dist/); compiling is too
heavy to do inside a request. (The Python TUI in tui/ was retired 2026-11-19.)

None of these take a session or token (they're in app.py's AUTH_EXEMPT_EXACT). The script and
binaries hold no secrets. Pairing trusts Tailscale Serve's identity headers: the app is reached on
the tailnet only through `tailscale serve` (127.0.0.1:7000, tailnet only, no Funnel), which
proxies from loopback, sets X-Forwarded-For to the caller's tailnet address and sets
Tailscale-User-Login itself, replacing any the caller sent (verified 2026-11-18 from the deck;
tagged devices get none). The login must be in the setting
``tui_pair_logins`` ({login: owner}). A local process could forge the headers, but a local
process already has the PC.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

logger = logging.getLogger(__name__)
_REPO = Path(__file__).resolve().parents[1]
INDEX_PORT = 8765                         # tk-index over the tailnet (tailscale serve)
TOKEN_PROFILE = "tui"
PAIRS_PER_HOUR = 10

_TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
RS_DIST = _REPO / "tui-rs" / "dist"
RS_ARCHES = ("x86_64", "aarch64")
_pairs: list[float] = []


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# --- the builds (made by scripts/tui_rs_dist.sh) ---------------------------------------------

_rs_cache: dict[str, tuple[tuple, str]] = {}


def rs_binary(arch: str) -> Path:
    return RS_DIST / f"mabel-os-{arch}"


def rs_builds() -> dict[str, str]:
    """{arch: build id} for the Rust binaries there are: a hash of each file, recomputed only when
    its size or mtime moved."""
    builds = {}
    for arch in RS_ARCHES:
        path = rs_binary(arch)
        try:
            st = path.stat()
        except OSError:
            continue
        stamp = (st.st_size, st.st_mtime_ns)
        cached = _rs_cache.get(arch)
        if not cached or cached[0] != stamp:
            cached = (stamp, hashlib.sha256(path.read_bytes()).hexdigest()[:16])
            _rs_cache[arch] = cached
        builds[arch] = cached[1]
    return builds


# --- addresses and identity ----------------------------------------------------------------------

def base_url(request: Request) -> str:
    host = (request.headers.get("host") or "").split(":")[0]
    if host.endswith(".ts.net"):
        return f"https://{host}"
    return str(request.base_url).rstrip("/")


def index_url(base: str) -> str:
    return f"{base}:{INDEX_PORT}" if base.startswith("https://") and base.endswith(".ts.net") \
        else "http://127.0.0.1:8766"


def tailnet_login(request: Request) -> str | None:
    """The tailnet login Tailscale Serve vouches for, or None if this didn't come through Serve."""
    host = request.client.host if request.client else ""
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
    # mabel-web listens on 127.0.0.1 only, so Serve proxies from loopback, and uvicorn (proxy
    # headers trusted from 127.0.0.1 only) then reports the caller as X-Forwarded-For's address.
    if host not in ("127.0.0.1", "::1") and host != forwarded:
        return None
    try:
        if not any(ipaddress.ip_address(forwarded) in net for net in _TAILNET_NETS):
            return None
    except ValueError:
        return None
    login = (request.headers.get("tailscale-user-login") or "").strip().lower()
    return login or None                             # tagged devices carry no login


def owner_for(login: str | None) -> str | None:
    allowed = _setting("tui_pair_logins", {}) or {}
    if not login or not isinstance(allowed, dict):
        return None
    return {str(k).strip().lower(): str(v) for k, v in allowed.items()}.get(login)


_DEVICE = re.compile(r"[^A-Za-z0-9._-]+")


def device_name(raw: str | None) -> str:
    name = re.sub(r"-{2,}", "-", _DEVICE.sub("-", str(raw or ""))).strip("-.")[:40]
    return name or "device"


def mint_token(owner: str, device: str) -> dict:
    """A new tui-profile token for this device; an older one with the same name is revoked."""
    import bcrypt
    from core.database import ApiToken, get_db_session
    from routes.api_token_routes import TOKEN_PROFILES
    name = f"Mabel TUI · {device}"
    raw = "ody_" + secrets.token_urlsafe(32)
    with get_db_session() as db:
        for old in db.query(ApiToken).filter(ApiToken.name == name, ApiToken.owner == owner,
                                             ApiToken.is_active.is_(True)).all():
            old.is_active = False
        token_id = str(uuid.uuid4())[:8]
        db.add(ApiToken(id=token_id, owner=owner, name=name,
                        token_hash=bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode(),
                        token_prefix=raw[:8], scopes=",".join(TOKEN_PROFILES[TOKEN_PROFILE]), is_active=True))
    return {"id": token_id, "name": name, "token": raw}


# --- the installer ---------------------------------------------------------------------------------

def install_script(base: str, rs: dict[str, str] | None = None) -> str:
    rs_line = " ".join(f"{arch}:{build_id}" for arch, build_id in (rs or {}).items())
    return (_REPO / "scripts" / "tui_install.sh").read_text(encoding="utf-8") \
        .replace("@@BASE@@", base).replace("@@RS_BUILDS@@", rs_line)


def setup_tui_install_routes() -> APIRouter:
    router = APIRouter(tags=["tui-install"])

    @router.get("/tui/install.sh")
    def installer(request: Request):
        return PlainTextResponse(install_script(base_url(request), rs_builds()), media_type="text/x-shellscript")

    @router.get("/tui/version")
    def version():
        # "build" is what the retired Python TUI compares with its own: a value it never has, so
        # any Python copy still out there offers F12, and that installer run moves it to Rust.
        return {"build": "rust", "rs": rs_builds()}

    @router.get("/tui/rs/{filename}")
    def rust_binary(filename: str):
        match = re.fullmatch(r"mabel-os-(x86_64|aarch64)", filename)
        path = rs_binary(match.group(1)) if match else None
        if path is None or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=filename, media_type="application/octet-stream")

    @router.get("/api/tui/whoami")
    def whoami(request: Request):
        login = tailnet_login(request)
        return {"login": login, "can_pair": bool(owner_for(login))}

    @router.post("/api/tui/pair")
    async def pair(request: Request):
        login = tailnet_login(request)
        owner = owner_for(login)
        if not owner:
            logger.info("[tui-install] pairing refused for %s", login or "a caller without a tailnet login")
            raise HTTPException(403, "This device's tailnet login isn't allowed to pair"
                                if login else "Pairing works only over the tailnet, from a device signed in as a person")
        now = time.time()
        _pairs[:] = [t for t in _pairs if now - t < 3600]
        if len(_pairs) >= PAIRS_PER_HOUR:
            raise HTTPException(429, "Too many pairings this hour")
        _pairs.append(now)
        try:
            body = await request.json()
        except Exception:
            body = {}
        device = device_name((body or {}).get("device") if isinstance(body, dict) else None)
        token = mint_token(owner, device)
        try:
            invalidate = getattr(request.app.state, "invalidate_token_cache", None)
            invalidate and invalidate()
        except Exception:
            pass
        logger.info("[tui-install] paired %s for %s (token %s)", device, login, token["id"])
        base = base_url(request)
        return {"url": base, "index_url": index_url(base), "token": token["token"], "name": token["name"]}

    return router
