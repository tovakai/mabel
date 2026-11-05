"""Phase 2: feed finished work into the affect state (cheap, no model calls).

Conversation turns and background work are recorded as separate activity
kinds. Token counts are estimated from the visible text (about 4 characters
per token): what was actually said, not prompt or reasoning overhead.
Failures are logged and never affect the chat.
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def estimate_tokens(*texts: str) -> int:
    return sum(len(t or "") for t in texts) // 4


def _now() -> datetime:
    from src.user_time import user_timezone
    return datetime.now(user_timezone())


def note_completed_turn(user_message: str, assistant_message: str) -> None:
    """A finished chat turn: conversation activity, last-interaction time, whisper mention."""
    try:
        from .composer import composer, current_mode
        if current_mode() == "off":
            return
        comp, now = composer(), _now()
        comp.affect.record_activity("conversation", estimate_tokens(user_message, assistant_message), now)
        if comp.whispers.note_turn(f"{user_message}\n{assistant_message}", now):
            logger.info("[persona] curiosity whisper came up; cooling off")
    except Exception:
        logger.warning("[persona] could not record conversation activity", exc_info=True)


def note_background_work(tokens: int) -> None:
    """Work done without the user in the conversation (Dottie; later self-directed research)."""
    try:
        from .composer import composer, current_mode
        if current_mode() == "off" or tokens <= 0:
            return
        composer().affect.record_activity("background", tokens, _now())
    except Exception:
        logger.warning("[persona] could not record background activity", exc_info=True)
