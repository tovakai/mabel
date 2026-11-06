"""Make room on the GPU: step Mabel's model aside, run something else, bring her back.

The 3080 can't hold the foreground model and an image model at once, so a GPU
service (image generation today, dreams later) runs inside ``room(service)``:

    stop Strata  ->  start the service  ->  wait until it answers  ->  (caller works)
    ->  stop the service  ->  start Strata again  ->  check she generates

Bringing her back always runs (``finally``), and a crashed swap is repaired on
the next app start (``recover``). Only one swap runs at a time. Strata is only
restarted if it was running before the swap.

Config lives in ``data/gpu_swap.json`` (local paths, not committed)::

    {"strata_profile": "launch-gyro-s-64k",
     "services": {"image": {"url": "http://127.0.0.1:8100", "ready_path": "/v1/models",
                            "command": ["/path/venv/bin/python", "scripts/diffusion_server.py", "--model", "..."],
                            "ready_timeout": 300}}}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()
_model_calls = 0                       # her model's in-flight background calls in this process
DRAIN_WAIT = 300                       # how long a swap waits for those to finish
SWAP_WAIT = 300                        # how long a model call waits for a swap to finish
STRATA_STOP_WAIT = 120
STRATA_START_WAIT = 900


class SwapError(RuntimeError):
    pass


def _data_dir() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR)


def _state_path() -> Path:
    return _data_dir() / "gpu_swap" / "state.json"


def load_config() -> dict:
    path = _data_dir() / "gpu_swap.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _strata_bin(cfg: dict) -> str:
    return cfg.get("strata_bin") or shutil.which("strata") or str(Path.home() / ".local/bin/strata")


def manages(url: str, cfg: dict | None = None) -> str | None:
    """The swap service whose endpoint ``url`` points at, if any."""
    cfg = load_config() if cfg is None else cfg
    target = urlparse(url or "")
    for name, svc in (cfg.get("services") or {}).items():
        own = urlparse(svc.get("url", ""))
        if own.hostname and (own.hostname, own.port) == (target.hostname, target.port):
            return name
    return None


def active() -> dict | None:
    """The swap in progress (service, phase, since), or None."""
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(state: dict | None) -> None:
    path = _state_path()
    if state is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)


def _ledger(kind: str, **meta) -> None:
    try:
        from src.agent_runtime.event_ledger import Event, Ledger
        Ledger(_data_dir() / "phase0" / "events.jsonl").append(Event.create(
            "runtime", kind, modules=tuple(f"{k}.{v}" for k, v in meta.items())))
    except Exception:
        pass


# ── Strata ──────────────────────────────────────────────────────────────

def _strata(cfg: dict, action: str, wait: int) -> dict:
    cmd = [_strata_bin(cfg), "runtime", action, "--profile", cfg["strata_profile"], "--json"]
    if action in ("start", "stop"):
        cmd += ["--wait", str(wait)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=wait + 60)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)}
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "error": (proc.stderr or proc.stdout)[-300:]}


def strata_running(cfg: dict) -> bool:
    return _strata(cfg, "status", 30).get("state") == "ready"


def _bring_strata_back(cfg: dict) -> bool:
    for attempt in (1, 2):
        res = _strata(cfg, "start", STRATA_START_WAIT)
        if res.get("ok") and res.get("state") == "ready":
            return True
        logger.error("gpu_swap: Strata start attempt %d failed: %s", attempt, res.get("error") or res)
        _strata(cfg, "cleanup", 60)
    return False


# ── The service process ─────────────────────────────────────────────────

def _start_service(name: str, svc: dict) -> subprocess.Popen:
    log_dir = _data_dir() / "gpu_swap"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"{name}.log", "ab")
    return subprocess.Popen(svc["command"], cwd=svc.get("cwd") or None, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, **(svc.get("env") or {})})


def _wait_ready(proc: subprocess.Popen, svc: dict) -> None:
    import httpx
    url = svc["url"].rstrip("/") + svc.get("ready_path", "/v1/models")
    deadline = time.monotonic() + float(svc.get("ready_timeout", 300))
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SwapError(f"service exited while loading (code {proc.returncode}); see data/gpu_swap/ log")
        try:
            if httpx.get(url, timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SwapError("service didn't become ready in time")


def _stop_pid(pid: int, grace: float = 20) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.5)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _reap(proc: subprocess.Popen) -> None:
    _stop_pid(proc.pid)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


# ── Her model's background calls ────────────────────────────────────────

@asynccontextmanager
async def model_call():
    """Wrap a background call to her model: it waits out a swap, and a swap waits for it."""
    global _model_calls
    deadline = time.monotonic() + SWAP_WAIT
    while active() and time.monotonic() < deadline:
        await asyncio.sleep(2)
    _model_calls += 1
    try:
        yield
    finally:
        _model_calls -= 1


async def _drain_model_calls() -> None:
    """Stopping Strata mid-generation cuts that reply short, so let running calls finish."""
    deadline = time.monotonic() + DRAIN_WAIT
    while _model_calls and time.monotonic() < deadline:
        await asyncio.sleep(1)
    if _model_calls:
        raise SwapError(f"her model is still busy ({_model_calls} call(s) running)")


# ── Public API ──────────────────────────────────────────────────────────

@asynccontextmanager
async def room(service: str):
    """Run ``service`` on the GPU for the duration of the block; Mabel's model steps aside."""
    cfg = load_config()
    svc = (cfg.get("services") or {}).get(service)
    if not svc or not cfg.get("strata_profile"):
        raise SwapError(f"no GPU swap configured for '{service}'")
    async with _LOCK:
        lock_file = await asyncio.to_thread(_cross_process_lock)
        try:
            async with _swapped(cfg, service, svc) as url:
                yield url
        finally:
            lock_file.close()


def _cross_process_lock():
    """Blocks until no other process (e.g. the image MCP server) is mid-swap."""
    import fcntl
    path = _data_dir() / "gpu_swap" / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


@asynccontextmanager
async def _swapped(cfg: dict, service: str, svc: dict):
    started = time.monotonic()
    was_running = await asyncio.to_thread(strata_running, cfg)
    state = {"service": service, "phase": "stepping_aside", "pid": None, "restore_strata": was_running,
             "since": datetime.now(timezone.utc).isoformat()}
    _write_state(state)
    _ledger("gpu.swap_started", service=service)
    proc = None
    try:
        await _drain_model_calls()             # state is written first, so no new call slips in
        if was_running:
            res = await asyncio.to_thread(_strata, cfg, "stop", STRATA_STOP_WAIT)
            if not res.get("ok"):
                raise SwapError(f"couldn't stop Strata: {res.get('error') or res}")
        proc = await asyncio.to_thread(_start_service, service, svc)
        _write_state({**state, "phase": "loading", "pid": proc.pid})
        await asyncio.to_thread(_wait_ready, proc, svc)
        _write_state({**state, "phase": "working", "pid": proc.pid})
        yield svc["url"]
    finally:
        _write_state({**state, "phase": "coming_back", "pid": proc.pid if proc else None})
        if proc:
            await asyncio.to_thread(_reap, proc)
        back = True
        if was_running:
            back = await asyncio.to_thread(_bring_strata_back, cfg)
        _write_state(None)
        seconds = round(time.monotonic() - started)
        _ledger("gpu.swap_finished", service=service, strata="back" if back else "failed")
        logger.info("gpu_swap: %s done in %ss (Strata %s)", service, seconds, "back" if back else "FAILED")


def recover() -> str | None:
    """After a crash mid-swap: stop the leftover service and bring Strata back."""
    state = active()
    if not state:
        return None
    cfg = load_config()
    if state.get("pid"):
        _stop_pid(int(state["pid"]))
    back = True
    if state.get("restore_strata", True) and cfg.get("strata_profile") and not strata_running(cfg):
        back = _bring_strata_back(cfg)
    _write_state(None)
    _ledger("gpu.swap_recovered", service=state.get("service", "?"), strata="back" if back else "failed")
    return state.get("service")
