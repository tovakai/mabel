"""Run Dottie's maintenance loops. None of them use Mabel's (foreground) model.

- archivist: rule-based, cheap; safe to run often.
- scout: a few web searches and page fetches.
- dreamer: one call to Dottie's small model.
- stale: Archivist's stale-framing check of the notes about Mabel (Dottie's model; unchanged sections skipped).
- embed: refresh the vault index and embed missing/stale chunks with the Knowledge embeddings role
  (bge-m3 on Dottie's machine), so new notes become searchable by meaning too.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from .inbox import default_inbox

logger = logging.getLogger(__name__)
LOOPS = ("archivist", "scout", "dreamer", "stale", "embed")
_last: dict = {}


async def run(names=LOOPS, *, inbox=None, paths=None) -> dict:
    from src.curiosity.session import Paths
    inbox = inbox or default_inbox()
    paths = paths or Paths()
    results = {}
    for name in names:
        try:
            if name == "archivist":
                from . import archivist
                results[name] = archivist.run(inbox, paths.index, paths.data / "backlog.json")
            elif name == "scout":
                from . import scout
                results[name] = await scout.run(inbox, paths)
            elif name == "dreamer":
                from . import dreamer
                results[name] = await dreamer.run(inbox, owner=paths.owner)
            elif name == "embed":
                results[name] = await embed_vault(paths)
            elif name == "stale":
                from . import stale
                results[name] = await stale.run(inbox, source_vault=paths.source_vault, owner=paths.owner,
                                                state_path=paths.data.parent / "subconscious" / "stale_seen.json")
            else:
                raise ValueError(f"unknown loop {name}")
        except Exception as error:
            logger.warning("[subconscious] %s failed: %s", name, error, exc_info=True)
            results[name] = {"error": str(error)[:300]}
        _last[name] = {"at": datetime.now(timezone.utc).isoformat(), "result": results[name]}
    return results


EMBED_LIMIT = 1000


async def embed_vault(paths) -> dict:
    """Reconcile the vault index if notes changed, then embed what's missing (remote embedder only)."""
    import asyncio as _asyncio
    from src.knowledge_engine.index import VaultIndex
    from src.knowledge_engine.maintenance import embed_pending
    index = VaultIndex(paths.source_vault, owner=paths.owner)
    if index.needs_reconcile():
        await _asyncio.to_thread(index.reconcile)
    result = await embed_pending(index, owner=paths.owner, limit=EMBED_LIMIT)
    return {"generated": result["generated"], "coverage": result["coverage"]}


def last_runs() -> dict:
    return dict(_last)


# --- background runs -----------------------------------------------------------------------

import asyncio  # noqa: E402

_task: asyncio.Task | None = None
AUTO_SCHEDULE = {"archivist": 6 * 3600, "embed": 6 * 3600, "dreamer": 24 * 3600, "stale": 24 * 3600}   # scout only on request (web searches)
FIRST_DELAY_S = 600


def running() -> bool:
    return bool(_task and not _task.done())


def start(names=LOOPS) -> bool:
    """Run loops in the background; False if a run is already going."""
    global _task
    if running():
        return False
    _task = asyncio.get_running_loop().create_task(run(tuple(names)))
    return True


async def scheduler() -> None:
    """Cheap loops on a timer (archivist every 6 h, dreamer daily). Neither uses Mabel's model."""
    await asyncio.sleep(FIRST_DELAY_S)
    due = {name: 0.0 for name in AUTO_SCHEDULE}
    loop = asyncio.get_running_loop()
    while True:
        try:
            from src.settings import get_setting
            enabled = bool(get_setting("subconscious_auto_enabled", True))
        except Exception:
            enabled = True
        now = loop.time()
        names = [n for n, at in due.items() if now >= at]
        if enabled and names and not running():
            await run(tuple(names))
            for n in names:
                due[n] = now + AUTO_SCHEDULE[n]
        await asyncio.sleep(300)
