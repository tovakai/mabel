"""Install MABEL-OS (tui/) on any device of his tailnet with one line:

    curl -fsSL https://tovakai.tail4312a5.ts.net/tui/install.sh | sh

GET  /tui/install.sh                       the installer, with this server's address filled in
GET  /tui/mabel_tui-<ver>-py3-none-any.whl  the TUI as a wheel, built from tui/ (cached until a file changes)
GET  /tui/version                           the current build id, for the TUI's update check (F12)
GET  /api/tui/whoami                        which tailnet login Tailscale Serve says the caller is
POST /api/tui/pair                          a "tui" profile API token for a device of an allowed login

None of these take a session or token (they're in app.py's AUTH_EXEMPT_EXACT). The script and
wheel hold no secrets. Pairing trusts Tailscale Serve's identity headers: the app is reached on
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
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

logger = logging.getLogger(__name__)
_REPO = Path(__file__).resolve().parents[1]
TUI_DIR = _REPO / "tui"
INDEX_PORT = 8765                         # tk-index over the tailnet (tailscale serve)
TOKEN_PROFILE = "tui"
PAIRS_PER_HOUR = 10

_TAILNET_NETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
_build_lock = threading.Lock()
_pairs: list[float] = []


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# --- the wheel ---------------------------------------------------------------------------------

def _source_files() -> list[Path]:
    files = [TUI_DIR / "pyproject.toml"]
    for path in sorted((TUI_DIR / "mabel_tui").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            files.append(path)
    return files


_fp_cache: tuple[tuple, str] | None = None


def _fingerprint() -> str:
    """The build id: a hash of tui/'s contents (not mtimes, so touching a file isn't an update).
    Recomputed only when a file's size or mtime moved."""
    global _fp_cache
    files = _source_files()
    stamp = tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files)
    if _fp_cache and _fp_cache[0] == stamp:
        return _fp_cache[1]
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(TUI_DIR)).encode() + b"\0" + path.read_bytes() + b"\0")
    _fp_cache = (stamp, digest.hexdigest()[:16])
    return _fp_cache[1]


def _cache_dir() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "cache" / "tui-wheel"


def build_wheel() -> Path:
    """The current tui/ as a wheel, rebuilt only when a source file changed."""
    with _build_lock:
        out = _cache_dir() / _fingerprint()
        wheels = sorted(out.glob("mabel_tui-*.whl")) if out.is_dir() else []
        if wheels:
            return wheels[-1]
        uv = shutil.which("uv") or "/usr/bin/uv"
        tmp = out.with_name(out.name + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        result = subprocess.run([uv, "build", "--wheel", "--out-dir", str(tmp), str(TUI_DIR)],
                                capture_output=True, text=True, timeout=180)
        built = sorted(tmp.glob("mabel_tui-*.whl"))
        if result.returncode or not built:
            raise RuntimeError(f"uv build failed: {(result.stderr or result.stdout)[-400:]}")
        for old in _cache_dir().iterdir():           # keep only the newest build
            if old != tmp:
                shutil.rmtree(old, ignore_errors=True)
        tmp.rename(out)
        return out / built[-1].name


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

def install_script(base: str, wheel_name: str, build: str) -> str:
    return (_REPO / "scripts" / "tui_install.sh").read_text(encoding="utf-8") \
        .replace("@@BASE@@", base).replace("@@WHEEL@@", wheel_name).replace("@@BUILD@@", build)


def setup_tui_install_routes() -> APIRouter:
    router = APIRouter(tags=["tui-install"])

    @router.get("/tui/install.sh")
    def installer(request: Request):
        try:
            wheel = build_wheel()
        except Exception as exc:
            logger.warning("[tui-install] wheel build failed: %s", exc)
            raise HTTPException(503, "Couldn't build the TUI package on the server")
        return PlainTextResponse(install_script(base_url(request), wheel.name, wheel.parent.name),
                                 media_type="text/x-shellscript")

    @router.get("/tui/version")
    def version():
        return {"build": _fingerprint()}

    @router.get("/tui/{filename}")
    def wheel_file(filename: str):
        if not re.fullmatch(r"mabel_tui-[\w.]+-py3-none-any\.whl", filename):
            raise HTTPException(404)
        wheel = build_wheel()
        return FileResponse(wheel, filename=wheel.name, media_type="application/octet-stream")

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
