"""Admin-only Gyro-S lifecycle controls for the local Strata installation.

The HTTP API stays alive while a model is suspended, so resume can use
POST /v1/load instead of needlessly restarting the server.  A fully stopped
server is launched with the user's saved Strata preset (``PRESET``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException

from core.middleware import require_admin

logger = logging.getLogger(__name__)

STRATA_URL = "http://127.0.0.1:8080"
EXPECTED_MODEL = "qwen3.8-flash-next-gyro-gyro-s"
# The saved Strata preset Mabel's start button launches (64k context since
# 2026-11-03; set MABEL_STRATA_PRESET=gyro-s-32k to go back).
PRESET = os.getenv("MABEL_STRATA_PRESET", "gyro-s-64k").strip() or "gyro-s-64k"
STARTUP_GRACE_SECONDS = 120
_launch_lock = asyncio.Lock()
_launch_process: subprocess.Popen | None = None
_launch_started_at = 0.0


def _enabled() -> bool:
    return os.getenv("MABEL_STRATA_CONTROL_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _headers() -> dict[str, str]:
    key = os.getenv("STRATA_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


def _expected_model(model: object) -> bool:
    name = str(model or "").strip().lower()
    return name in {EXPECTED_MODEL, "gyro-s", "gyro-s-strata"} or name.endswith("/gyro-s")


def _status_payload(state: str, **extra: object) -> dict:
    return {"enabled": True, "state": state, "model": "Gyro-S", **extra}


def _still_starting() -> bool:
    if not _launch_started_at or time.monotonic() - _launch_started_at > STARTUP_GRACE_SECONDS:
        return False
    # If the CLI itself died with a failure, don't pretend startup continues.
    return _launch_process is None or _launch_process.poll() is None or _launch_process.returncode == 0


async def _probe() -> dict:
    try:
        async with httpx.AsyncClient(timeout=2.5, trust_env=False) as client:
            response = await client.get(f"{STRATA_URL}/health", headers=_headers())
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return _status_payload("starting" if _still_starting() else "offline")
    except httpx.HTTPError:
        return _status_payload("unavailable")

    if response.status_code == 401:
        return _status_payload("unauthorized")
    if response.status_code != 200:
        return _status_payload("unexpected-service")

    try:
        health = response.json()
    except ValueError:
        return _status_payload("unexpected-service")
    if not isinstance(health, dict) or health.get("service") != "strata":
        return _status_payload("unexpected-service")
    name = health.get("model")
    if not _expected_model(name):
        return _status_payload("wrong-model", active_model=str(name or "unknown"))
    return _status_payload("ready" if health.get("loaded") is True else "suspended")


def _executable() -> str:
    explicit = os.getenv("MABEL_STRATA_EXECUTABLE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise HTTPException(503, "MABEL_STRATA_EXECUTABLE is not executable")
        return str(path)
    installed = Path.home() / ".local" / "bin" / "strata"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    found = shutil.which("strata")
    if found:
        return found
    raise HTTPException(503, "Strata launcher not found; set MABEL_STRATA_EXECUTABLE")


async def _control(action: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=120.0, trust_env=False) as client:
            response = await client.post(
                f"{STRATA_URL}/v1/{action}",
                headers=_headers(),
                json={},
            )
    except httpx.HTTPError as exc:
        logger.warning("Strata %s request failed: %s", action, type(exc).__name__)
        raise HTTPException(502, "Cannot reach the Strata control API") from exc

    if response.status_code == 401:
        raise HTTPException(502, "Strata rejected the API key; set STRATA_API_KEY for Mabel")
    if response.status_code == 409:
        raise HTTPException(409, "Strata has an active request; try again when it finishes")
    if response.status_code == 404:
        raise HTTPException(502, "Strata's /v1/load and /v1/unload API is unavailable; update Strata")
    if not response.is_success:
        logger.warning("Strata %s returned HTTP %s", action, response.status_code)
        raise HTTPException(502, f"Strata {action} failed (HTTP {response.status_code})")
    return await _probe()


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(404, "Strata control is not enabled")


def setup_strata_routes() -> APIRouter:
    router = APIRouter(prefix="/api/strata", tags=["strata"], dependencies=[Depends(require_admin)])

    @router.get("/status")
    async def status():
        if not _enabled():
            return {"enabled": False}
        return await _probe()

    @router.post("/start")
    async def start():
        global _launch_process, _launch_started_at
        _require_enabled()
        async with _launch_lock:
            current = await _probe()
            state = current["state"]
            if state == "ready" or state == "starting":
                return current
            if state == "suspended":
                return await _control("load")
            if state != "offline":
                raise HTTPException(409, f"Cannot start Gyro-S: Strata is {state}")

            command = [_executable(), "launch", "--preset", PRESET, "--no-tui", "--yes"]
            try:
                _launch_process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                logger.exception("Could not launch Strata Gyro-S")
                raise HTTPException(503, "Could not start the Strata launcher") from exc
            _launch_started_at = time.monotonic()
            logger.info("Started Strata launcher using preset %s (pid %s)", PRESET, _launch_process.pid)
            return _status_payload("starting")

    @router.post("/suspend")
    async def suspend():
        _require_enabled()
        async with _launch_lock:
            current = await _probe()
            state = current["state"]
            if state in {"offline", "suspended"}:
                return current
            if state != "ready":
                raise HTTPException(409, f"Cannot suspend Gyro-S: Strata is {state}")
            return await _control("unload")

    return router
