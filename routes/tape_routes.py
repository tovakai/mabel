"""/api/tapes: his DreamTape repo. MABEL-OS publishes tapes here from the PC and installs them
on any device (the deck can't build them itself).

A .dtape is a tar archive: tape.toml, tape.wasm, art/*.mpx|png, music/*.mtk. Nothing else is
accepted. Tapes live in data/tapes/<name>.dtape.

Browser sessions, or an API token with the "tapes" scope (token profile "tui").
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
import time
import tomllib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from src.auth_helpers import require_api_token_scope, require_user

TAPES_DIR = Path(__file__).resolve().parent.parent / "data" / "tapes"
NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BYTES = 32 * 1024 * 1024
ALLOWED = re.compile(r"^(tape\.toml|tape\.wasm|art/[A-Za-z0-9_.-]+\.(mpx|png)|music/[A-Za-z0-9_.-]+\.mtk)$")


def tapes_user(request: Request) -> str:
    if getattr(request.state, "api_token", False):
        return require_api_token_scope(request, "tapes") or ""
    return require_user(request)


def check_name(name: str) -> str:
    if not NAME.match(name):
        raise HTTPException(400, "a tape name is 1-64 letters, digits, - and _")
    return name


def read_tape(data: bytes) -> dict:
    """Validate a .dtape and return what its tape.toml says. Raises ValueError."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"a tape is at most {MAX_BYTES // (1024 * 1024)} MB")
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError as exc:
        raise ValueError(f"not a .dtape (tar) file: {exc}") from None
    info, has_wasm = {}, False
    with tar:
        for m in tar.getmembers():
            if m.isdir():
                if m.name.rstrip("/") not in ("art", "music"):
                    raise ValueError(f"unexpected folder in the tape: {m.name}")
                continue
            if not m.isfile() or not ALLOWED.match(m.name):
                raise ValueError(f"not allowed in a tape: {m.name}")
            if m.name == "tape.wasm":
                has_wasm = True
            if m.name == "tape.toml":
                try:
                    info = tomllib.loads(tar.extractfile(m).read().decode("utf-8"))
                except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                    raise ValueError(f"tape.toml: {exc}") from None
    if not has_wasm:
        raise ValueError("the tape has no tape.wasm")
    return info


def describe(path: Path) -> dict | None:
    try:
        data = path.read_bytes()
        info = read_tape(data)
    except (OSError, ValueError):
        return None
    return {
        "name": path.stem,
        "title": str(info.get("title") or path.stem),
        "author": str(info.get("author") or ""),
        "description": str(info.get("description") or ""),
        "screen": str(info.get("screen") or "fill"),
        "fps": info.get("fps", 30),
        "size": len(data),
        "sha": hashlib.sha256(data).hexdigest()[:16],
        "published": int(path.stat().st_mtime),
    }


def setup_tape_routes() -> APIRouter:
    router = APIRouter(tags=["tapes"])

    @router.get("/api/tapes")
    async def list_tapes(_user=Depends(tapes_user)):
        TAPES_DIR.mkdir(parents=True, exist_ok=True)
        tapes = [d for p in sorted(TAPES_DIR.glob("*.dtape")) if (d := describe(p))]
        return {"tapes": tapes}

    @router.get("/api/tapes/{name}.dtape")
    async def download(name: str, _user=Depends(tapes_user)):
        path = TAPES_DIR / f"{check_name(name)}.dtape"
        if not path.is_file():
            raise HTTPException(404, f"no tape called {name} on Mabel")
        return FileResponse(path, media_type="application/x-tar", filename=f"{name}.dtape")

    @router.put("/api/tapes/{name}")
    async def publish(name: str, request: Request, _user=Depends(tapes_user)):
        check_name(name)
        data = await request.body()
        try:
            read_tape(data)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        TAPES_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TAPES_DIR / f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, TAPES_DIR / f"{name}.dtape")  # never half a tape
        return describe(TAPES_DIR / f"{name}.dtape")

    @router.delete("/api/tapes/{name}")
    async def unpublish(name: str, _user=Depends(tapes_user)):
        path = TAPES_DIR / f"{check_name(name)}.dtape"
        if not path.is_file():
            raise HTTPException(404, f"no tape called {name} on Mabel")
        path.unlink()
        return {"removed": name}

    return router
