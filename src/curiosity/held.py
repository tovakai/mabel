"""Messages that arrive while Mabel is off researching: "busy, back soon", then a real answer.

While a curiosity session runs it occupies the foreground model, so a chat
message would only queue behind it (or time out). Instead:

1. the message is saved as usual and she replies at once with a short canned
   "I'm busy, I'll answer when I'm back" line (no model call; the model is busy);
2. the message is held as a background job on that chat (several messages to
   the same chat are held together and answered together);
3. when the session ends, the existing background-delivery loop asks her for a
   real reply (persona, chat history, no tools) and posts it into the chat.
"""
from __future__ import annotations

import logging
import random
import re
import uuid

logger = logging.getLogger(__name__)

TOOL = "held_message"
HELD_TEXT_LIMIT = 8000
BUSY_LINES = (
    "I'm fucking busy right now{about}, but I'll answer as soon as I'm back. I promise.",
    "Hold that thought, I'm elbow-deep in research{about}. You're first in line when I surface. Promise.",
    "Can't talk, I'm off reading{about}. I saw your message though, I'll answer the second I'm back.",
    "Busy busy{about}. Don't go anywhere, I'm answering this as soon as I'm done. Promise.",
)


def research_subject() -> tuple[bool, str | None]:
    """(running, subject) for the current curiosity session."""
    try:
        from . import runner
        status = runner.status()
    except Exception:
        return False, None
    session = status.get("session") or {}
    return bool(status.get("running")), session.get("subject")


def busy_line(subject: str | None, rng: random.Random | None = None) -> str:
    about = f" (it's {subject}, don't ask)" if subject else ""
    return (rng or random).choice(BUSY_LINES).format(about=about)


def hold(session_id: str, owner: str, message: str) -> str:
    """Hold ``message`` for this chat; returns the background job id (one open job per chat)."""
    from core import database
    text = str(message or "").strip()[:HELD_TEXT_LIMIT]
    with database.SessionLocal() as db:
        session = db.get(database.Session, session_id)
        if session is None or session.owner != owner:
            raise ValueError("Origin chat not found")
        job = (db.query(database.BackgroundToolJob)
               .filter_by(session_id=session_id, owner=owner, tool=TOOL, status="running").first())
        if job:
            job.query = (job.query + "\n\n" + text)[-HELD_TEXT_LIMIT:]
            db.commit()
            return job.id
        job_id = f"held-{uuid.uuid4().hex[:16]}"
        db.add(database.BackgroundToolJob(id=job_id, session_id=session_id, owner=owner, tool=TOOL,
                                          query=text, rounds=0, status="running"))
        db.commit()
        return job_id


def ready_to_answer(job) -> bool:
    """Resolver for the delivery loop: answer once no curiosity session is running."""
    running, _ = research_subject()
    return not running


async def answer(session, payload: dict) -> str:
    """Her real reply to the held message(s), in persona, from the chat's own history."""
    from datetime import datetime
    from src.ai_interaction import get_session_manager
    from src.llm_core import llm_call_async
    from src.research_utils import strip_thinking
    sm = get_session_manager()
    live = sm.get_session(session.id) if sm else None
    if live is None or live.owner != session.owner:
        raise ValueError("Origin chat changed")
    history = [m for m in live.get_context_messages() if m.get("role") in ("user", "assistant")][-24:]
    held = str(payload.get("query") or "")
    system, fragment = None, None
    try:
        from src.persona import composer, current_mode
        from src.turn_contract import requested_capabilities
        from src.user_time import user_timezone
        if current_mode() == "on":
            comp = composer().compose(held, set(requested_capabilities(held)), session.id,
                                      local_now=datetime.now(user_timezone()), mode="on")
            system, fragment = comp.core_text, comp.fragment
    except Exception:
        logger.debug("[held] persona composition unavailable", exc_info=True)
    if not system:
        system = "You are Mabel, the user's companion. Reply in your own voice."
    _, subject = research_subject()
    note = ("[Context, not part of the user's messages: you were away on a research session"
            + (f" (about {subject})" if subject else "") + ". While you were busy, the user sent the "
            "message(s) above and you told them you'd answer when you're back. You're back now: answer "
            "them properly, the way you normally would; a quick nod to being back is fine. You can't use "
            "tools in this reply; if something needs looking up, say you'll check it if they want.]")
    messages = [{"role": "system", "content": system}, *history]
    if fragment:
        messages.append({"role": "user", "content": "[Context — your current mode and mood, from your own "
                                                     "configuration; not part of the user's message]\n" + fragment})
    messages.append({"role": "user", "content": note})
    headers = getattr(live, "headers", None) or {}
    reply = await llm_call_async(session.endpoint_url, session.model, messages, headers=headers,
                                 temperature=0.7, max_tokens=4000, timeout=150, max_retries=1,
                                 workload="background", session_id=session.id)
    text = strip_thinking(str(reply or "")).strip()
    text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    if not text:
        raise ValueError("empty held reply")
    return text
