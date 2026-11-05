"""Run one curiosity session in the background, from the Persona sidebar.

At most one session runs at a time (it occupies the foreground model for a
few minutes). Progress is the session's own checkpoint: which steps are done.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .session import Paths, Session

logger = logging.getLogger(__name__)

STEPS = ("candidates", "choice", "evidence", "draft", "quotes", "write")
_state: dict = {"task": None, "session": None, "error": None, "started_at": None, "finished_at": None}


class AlreadyRunning(RuntimeError):
    pass


def status() -> dict:
    task, session = _state["task"], _state["session"]
    running = bool(task and not task.done())
    out = {"running": running, "error": _state["error"], "started_at": _state["started_at"],
           "finished_at": _state["finished_at"], "session": None}
    if session is not None:
        done = [step for step in STEPS if session.done(step) is not None]
        choice = session.done("choice") or {}
        out["session"] = {"id": session.id, "steps_done": done,
                          "next_step": next((s for s in STEPS if s not in done), None),
                          "subject": choice.get("subject"), "question": choice.get("question"),
                          "why": choice.get("why"), "result": session.done("write"),
                          "dottie_decisions": choice.get("dottie_decisions") or []}
    return out


async def _run(session: Session) -> None:
    try:
        await session.run()
    except Exception as error:   # surfaced in status; the checkpoint allows a resume
        logger.warning("[curiosity] session %s failed: %s", session.id, error, exc_info=True)
        _state["error"] = str(error)[:300]
    finally:
        _state["finished_at"] = datetime.now(timezone.utc).isoformat()


def start(*, resume: str | None = None) -> dict:
    if _state["task"] and not _state["task"].done():
        raise AlreadyRunning("a curiosity session is already running")
    session = Session(Paths(), session_id=resume)
    _state.update(session=session, error=None, finished_at=None,
                  started_at=datetime.now(timezone.utc).isoformat())
    _state["task"] = asyncio.get_running_loop().create_task(_run(session))
    return status()
