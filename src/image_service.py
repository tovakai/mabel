"""Pictures on request for other local apps (TK Index first), made on Mabel's GPU swap.

Another app can't run the swap itself: it can't see her model calls, and the swap stops
Strata. So it only asks (``routes/image_service_routes.py``), and Mabel decides when:

- **queued** jobs wait on disk until she's idle anyway (the heartbeat calls ``gate`` and
  ``process``), and then every waiting picture is made in **one** swap;
- **process now** (``start_now``) starts that batch straight away. It still waits for her
  running model calls (``gpu_swap.room`` drains them) and never runs while a game is on.

A job may ask for a transparent background: the prompt then asks for a plain one, and
``cut_out`` removes it afterwards (a flood fill from the edges, which suits flat pixel art).
If the background isn't plain enough to remove cleanly the picture is kept as it came.

State lives in ``data/image_service/`` (``jobs.json`` and ``results/``). Finished pictures
are kept for a day. The ledger only gets counts and the client name, never prompts.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

QUEUE_CAP = 20
KEEP_FOR = timedelta(days=1)
BATCH_WAIT = timedelta(minutes=2)       # let a few requests gather before an idle batch
MAX_PROMPT = 1000
SIZE_MIN, SIZE_MAX, SIZE_DEFAULT = 512, 1024, 768
PLAIN_BACKGROUND = ", single object, centered, isolated on a plain flat white background"
CUT_TOLERANCE = 40                      # max per-channel distance from the background colour
CUT_MIN_BORDER = 0.6                    # share of edge pixels that must be background

_lock = asyncio.Lock()                  # one batch at a time, in this process
_task: asyncio.Task | None = None


class Busy(RuntimeError):
    """The queue can't take or start work right now; the message says why."""


# ── storage ──────────────────────────────────────────────────────────────

def _dir(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from src.constants import DATA_DIR
        data_dir = Path(DATA_DIR)
    return Path(data_dir) / "image_service"


def _load(data_dir: Path | None = None) -> list[dict]:
    try:
        jobs = json.loads((_dir(data_dir) / "jobs.json").read_text(encoding="utf-8")).get("jobs")
        return [j for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
    except (OSError, ValueError, AttributeError):
        return []


def _save(jobs: list[dict], data_dir: Path | None = None) -> None:
    folder = _dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "jobs.tmp"
    tmp.write_text(json.dumps({"jobs": jobs}, indent=1), encoding="utf-8")
    tmp.replace(folder / "jobs.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _ledger(kind: str, **meta) -> None:
    try:
        from src.agent_runtime.event_ledger import Event, Ledger
        from src.constants import DATA_DIR
        Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
            "runtime", kind, modules=tuple(f"{k}.{v}" for k, v in meta.items())))
    except Exception:
        logger.debug("[image_service] ledger event not written", exc_info=True)


def _clamp(value, default: int = SIZE_DEFAULT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    n = max(SIZE_MIN, min(SIZE_MAX, n))
    return n - n % 64


def prune(data_dir: Path | None = None, now: datetime | None = None) -> None:
    """Forget finished jobs (and their pictures) after a day."""
    now = now or _now()
    jobs, kept = _load(data_dir), []
    for job in jobs:
        ended = job.get("finished")
        if job.get("state") in ("done", "failed") and ended and datetime.fromisoformat(ended) < now - KEEP_FOR:
            if job.get("result"):
                (_dir(data_dir) / "results" / job["result"]).unlink(missing_ok=True)
            continue
        kept.append(job)
    if len(kept) != len(jobs):
        _save(kept, data_dir)


# ── asking ───────────────────────────────────────────────────────────────

def _configured_services() -> dict:
    from src import gpu_swap
    return gpu_swap.load_config().get("services") or {}


def enqueue(prompt: str, *, service: str = "image", width=None, height=None, transparent: bool = False,
            client: str = "", ref: str = "", data_dir: Path | None = None, now: datetime | None = None,
            services: dict | None = None) -> dict:
    """Queue one picture. Raises ValueError for a bad request and Busy when the queue is full."""
    prompt = " ".join(str(prompt or "").split())
    if not prompt:
        raise ValueError("prompt is empty")
    if len(prompt) > MAX_PROMPT:
        raise ValueError(f"prompt is longer than {MAX_PROMPT} characters")
    services = _configured_services() if services is None else services
    if not str(service).startswith("image") or service not in services:
        raise ValueError(f"no image service called '{service}'")
    now = now or _now()
    prune(data_dir, now)
    jobs = _load(data_dir)
    waiting = [j for j in jobs if j.get("state") in ("queued", "running")]
    if len(waiting) >= QUEUE_CAP:
        raise Busy(f"the queue is full ({QUEUE_CAP} pictures waiting)")
    job = {"id": secrets.token_hex(8), "state": "queued", "created": _iso(now), "service": service,
           "prompt": prompt, "width": _clamp(width), "height": _clamp(height), "transparent": bool(transparent),
           "client": str(client or "")[:40], "ref": str(ref or "")[:80]}
    jobs.append(job)
    _save(jobs, data_dir)
    _ledger("image_service.queued", client=job["client"] or "unknown")
    return {"job": job["id"], "position": len(waiting) + 1}


def _public(job: dict, position: int | None) -> dict:
    out = {"job": job["id"], "state": job["state"], "created": job.get("created"), "client": job.get("client"),
           "ref": job.get("ref"), "width": job.get("width"), "height": job.get("height"),
           "transparent": job.get("transparent", False)}
    if job["state"] == "queued":
        out["position"] = position
    if job["state"] == "done":
        out["finished"] = job.get("finished")
        out["background_removed"] = bool(job.get("background_removed"))
    if job["state"] == "failed":
        out["error"] = job.get("error") or "something went wrong"
        out["finished"] = job.get("finished")
    return out


def status(job_id: str, data_dir: Path | None = None) -> dict | None:
    queued = 0
    for job in _load(data_dir):
        if job.get("state") == "queued":
            queued += 1
        if job.get("id") == job_id:
            return _public(job, queued if job.get("state") == "queued" else None)
    return None


def result_path(job_id: str, data_dir: Path | None = None) -> Path | None:
    for job in _load(data_dir):
        if job.get("id") == job_id and job.get("state") == "done" and job.get("result"):
            path = _dir(data_dir) / "results" / job["result"]
            return path if path.is_file() else None
    return None


def gaming(data_dir: Path | None = None) -> str | None:
    """The game being played (game mode's state file), or None."""
    if data_dir is None:
        from src import gpu_swap
        data_dir = gpu_swap._data_dir()
    try:
        state = json.loads((Path(data_dir) / "gpu_swap" / "game_mode.json").read_text(encoding="utf-8"))
        return str(state.get("game") or "a game") if state else None
    except (OSError, ValueError):
        return None


def running() -> bool:
    return bool(_task and not _task.done()) or _lock.locked()


def overview(data_dir: Path | None = None) -> dict:
    jobs = _load(data_dir)
    queued = [j for j in jobs if j.get("state") == "queued"]
    return {"queued": len(queued), "running": running(), "cap": QUEUE_CAP,
            "oldest_queued": queued[0]["created"] if queued else None,
            "gaming": gaming(data_dir) is not None,
            "jobs": [_public(j, None) for j in jobs]}


# ── the transparent background ───────────────────────────────────────────

def cut_out(png: bytes, tolerance: int = CUT_TOLERANCE) -> bytes | None:
    """The picture with its plain background made transparent, or None if there isn't a plain one.

    The background colour is the median of the edge pixels; everything that colour and
    connected to an edge goes. Areas of that colour inside the object stay.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png)).convert("RGB")
    rgb = np.asarray(img).astype(np.int16)
    h, w = rgb.shape[:2]
    edge = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    bg = np.median(edge, axis=0)
    if (np.abs(edge - bg).max(axis=1) <= tolerance).mean() < CUT_MIN_BORDER:
        return None
    near = (np.abs(rgb - bg).max(axis=2) <= tolerance)
    mask = Image.fromarray(np.where(near, 255, 0).astype(np.uint8)).convert("L").copy()
    border = [(x, 0) for x in range(w)] + [(x, h - 1) for x in range(w)] \
        + [(0, y) for y in range(h)] + [(w - 1, y) for y in range(h)]
    for xy in border:
        if mask.getpixel(xy) == 255:
            ImageDraw.floodfill(mask, xy, 128, thresh=0)
    gone = np.asarray(mask) == 128
    share = gone.mean()
    if share < 0.03 or share > 0.97:            # nothing to cut, or it ate the object too
        return None
    rgba = np.dstack([rgb.astype(np.uint8), np.where(gone, 0, 255).astype(np.uint8)])
    out = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(out, format="PNG")
    return out.getvalue()


# ── making them ──────────────────────────────────────────────────────────

def _model_id(svc: dict) -> str:
    """The model's id as image_presets knows it: the folder name after --model."""
    cmd = [str(c) for c in svc.get("command") or []]
    if "--model" in cmd and cmd.index("--model") + 1 < len(cmd):
        return Path(cmd[cmd.index("--model") + 1]).name
    return ""


def _update(job_id: str, data_dir: Path | None, **changes) -> None:
    jobs = _load(data_dir)
    for job in jobs:
        if job.get("id") == job_id:
            job.update(changes)
            if changes.get("state") in ("done", "failed"):
                job["finished"] = _iso(_now())
                job.pop("prompt", None)          # the item's description isn't needed any more
    _save(jobs, data_dir)


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:200]


async def _render(client, url: str, model: str, job: dict) -> bytes:
    from src import image_presets
    prompt = job["prompt"] + (PLAIN_BACKGROUND if job.get("transparent") else "")
    full, negative = image_presets.apply(model, prompt)
    payload = {"model": model, "prompt": full, "n": 1, "size": f"{job['width']}x{job['height']}",
               "quality": "medium", "response_format": "b64_json"}
    if negative:
        payload["negative_prompt"] = negative
    resp = await client.post(url.rstrip("/") + "/v1/images/generations", json=payload)
    if resp.status_code != 200:
        raise RuntimeError(f"the image model answered {resp.status_code}")
    data = (resp.json().get("data") or [{}])[0]
    if not data.get("b64_json"):
        raise RuntimeError("the image model returned no picture")
    return base64.b64decode(data["b64_json"])


async def process(*, trigger: str = "idle", data_dir: Path | None = None, room=None, render=None) -> dict:
    """Make every queued picture, one GPU swap per service. Returns counts."""
    import httpx
    from src import gpu_swap
    room = room or gpu_swap.room
    render = render or _render
    done = failed = 0
    async with _lock:
        services = _configured_services()
        while True:
            queued = [j for j in _load(data_dir) if j.get("state") == "queued"]
            if not queued:
                break
            if gaming(data_dir):
                break                               # they stay queued for later
            service = queued[0]["service"]
            batch = [j for j in queued if j["service"] == service]
            svc = services.get(service)
            if not svc:
                for job in batch:
                    _update(job["id"], data_dir, state="failed", error=f"no image service called '{service}' any more")
                    failed += 1
                continue
            started = time.monotonic()
            _ledger("image_service.batch_started", service=service, count=len(batch), trigger=trigger)
            try:
                async with room(service) as url:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=30, read=300, write=30, pool=30)) as client:
                        for job in batch:
                            _update(job["id"], data_dir, state="running")
                            try:
                                png = await render(client, url, _model_id(svc), job)
                                cut = None
                                if job.get("transparent"):
                                    try:
                                        cut = await asyncio.to_thread(cut_out, png)
                                    except Exception:
                                        logger.warning("[image_service] background removal failed", exc_info=True)
                                folder = _dir(data_dir) / "results"
                                folder.mkdir(parents=True, exist_ok=True)
                                (folder / f"{job['id']}.png").write_bytes(cut or png)
                                _update(job["id"], data_dir, state="done", result=f"{job['id']}.png",
                                        background_removed=cut is not None)
                                done += 1
                            except Exception as exc:
                                logger.warning("[image_service] a picture failed: %s", _short_error(exc))
                                _update(job["id"], data_dir, state="failed", error=_short_error(exc))
                                failed += 1
            except Exception as exc:                 # the swap itself failed: these jobs can't run now
                why = _short_error(exc)
                logger.warning("[image_service] GPU swap for %s failed: %s", service, why)
                for job in batch:
                    current = status(job["id"], data_dir)
                    if current and current["state"] in ("queued", "running"):
                        _update(job["id"], data_dir, state="failed", error=f"couldn't get the GPU: {why}")
                        failed += 1
            _ledger("image_service.batch_finished", service=service, done=done, failed=failed,
                    seconds=round(time.monotonic() - started))
    return {"done": done, "failed": failed}


def start_now(*, data_dir: Path | None = None) -> dict:
    """"Process queue now": start a batch in the background. Raises Busy with the reason if it can't."""
    if running():
        raise Busy("already making pictures")
    game = gaming(data_dir)
    if game:
        raise Busy(f"a game is running ({game}); the pictures stay queued")
    queued = sum(1 for j in _load(data_dir) if j.get("state") == "queued")
    if not queued:
        raise Busy("nothing is queued")
    _start("now", data_dir)
    return {"started": True, "queued": queued}


def _start(trigger: str, data_dir: Path | None) -> asyncio.Task:
    global _task
    _task = asyncio.create_task(process(trigger=trigger, data_dir=data_dir))
    _task.add_done_callback(lambda t: not t.cancelled() and t.exception() and logger.warning(
        "[image_service] batch failed: %s", t.exception()))
    return _task


def start_idle(sig, *, now: datetime | None = None, research_running: bool = False) -> bool:
    """From the heartbeat: start the batch if the gate is clear. True while pictures are being made."""
    if running():
        return True
    if gate(sig, now=now, research_running=research_running):
        return False
    _start("idle", None)
    return True


# ── the idle batch (from the heartbeat) ──────────────────────────────────

def gate(sig, *, data_dir: Path | None = None, now: datetime | None = None,
         research_running: bool = False) -> list[str]:
    """Reasons not to make the queued pictures right now (empty = go)."""
    from src.persona import dreams
    now = now or _now()
    reasons = []
    queued = [j for j in _load(data_dir) if j.get("state") == "queued"]
    if not queued:
        return ["nothing queued"]
    if now - datetime.fromisoformat(queued[0]["created"]) < BATCH_WAIT:
        reasons.append("letting a few requests gather")
    if running():
        reasons.append("already making pictures")
    try:
        from src import gpu_swap
        if gpu_swap.active():
            reasons.append("the GPU is already swapped")
    except Exception:
        reasons.append("GPU swap unavailable")
    if gaming(data_dir):
        reasons.append("a game is running")
    if dreams.running():
        reasons.append("she's dreaming")
    if research_running:
        reasons.append("a research session is running")
    if sig.chat_active is not False:
        reasons.append("a chat is running (or unknown)")
    if sig.idle != "idle" or (sig.idle_minutes or 0) < dreams.MIN_IDLE_MINUTES:
        reasons.append(f"Anthon hasn't been away {dreams.MIN_IDLE_MINUTES} min")
    if sig.gpu != "free":
        reasons.append(f"GPU not free ({sig.gpu_reason or sig.gpu})")
    if not sig.strata:
        reasons.append("her model isn't up")
    return reasons
