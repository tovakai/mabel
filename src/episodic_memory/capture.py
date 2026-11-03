"""Model-free capture of a persisted, completed chat turn into PENDING memory.

This is not consolidation: neither a model answer nor a user assertion becomes
a verified fact just because a completed turn was saved. Dottie may later
review these suggestions against their original session message receipts.
No user or Mabel vault is accessed, and nothing is injected into future chats.
"""
from __future__ import annotations

from pathlib import Path
import re

from .candidates import CandidateDraft, CandidateQueue, CapturePolicy

_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _excerpt(text: str, *, limit: int) -> str:
    # Preserve enough meaningful wording for later adjudication, without
    # copying arbitrary large transcripts into this staging database.
    return " ".join(str(text or "").split())[:limit]


def capture_completed_turn(
    *,
    storage_dir: str | Path,
    owner: str,
    session_id: str,
    assistant_message_id: str | int | None,
    user_message: str,
    assistant_message: str,
    policy: CapturePolicy,
    protected_roots: tuple[str | Path, ...] = (),
) -> dict | None:
    """Stage one source-linked candidate per persisted assistant message.

    Returns None for disabled, unpersisted or too-short turns. The source
    identity is the committed assistant DB message ID, not a generated
    transcript hash or an LLM claim. This call never writes Markdown.
    """
    if not isinstance(policy, CapturePolicy):
        raise TypeError("Trusted capture policy required")
    if not policy.episodic_enabled or not policy.memory_enabled or policy.incognito:
        return None
    if not _TOKEN.fullmatch(str(session_id or "")):
        return None
    if assistant_message_id is None or not _TOKEN.fullmatch(str(assistant_message_id)):
        return None
    user_excerpt = _excerpt(user_message, limit=500)
    assistant_excerpt = _excerpt(assistant_message, limit=500)
    # The early capture worker collects substantive turns for Dottie's later
    # significance filter; it does not make this judgement itself.
    if len(user_excerpt) < 16 or len(assistant_excerpt) < 24:
        return None
    source_ref = f"chat:{session_id}:message:{assistant_message_id}"
    draft = CandidateDraft(
        kind="experience",
        actor="mabel",
        source_kind="conversation",
        source_ref=source_ref,
        source_event_id=f"message:{assistant_message_id}",
        origin="completed_persisted_turn",
        summary=f"User said: {user_excerpt}\nMabel replied: {assistant_excerpt}",
    )
    queue = CandidateQueue(storage_dir, owner=owner, protected_roots=protected_roots)
    return queue.propose(draft, policy=policy)
