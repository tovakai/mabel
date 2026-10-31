"""Safe terminal fallback for already authorized, bounded memory listings.

This is not tool discovery, an execution permission, or a replacement for
conversational synthesis. It only salvages a verified memory list after the
normal model-based final answer has failed.
"""
from __future__ import annotations

import re


_RECAP_REQUEST = re.compile(
    r"(?:\b(?:what\s+(?:do\s+)?you\s+(?:know|remember)\s+about\s+me|"
    r"tell\s+me\s+what\s+you\s+(?:know|remember)\s+about\s+me|"
    r"tell\s+me\s+everything\s+you\s+(?:know|remember)\s+about\s+me|"
    r"(?:list|show|view|summari[sz]e)\s+(?:all\s+)?(?:my\s+|the\s+)?(?:saved\s+)?memor(?:y|ies)|"
    r"what(?:'s|\s+is)\s+in\s+(?:my|your)\s+(?:saved\s+)?memory)\b)",
    re.IGNORECASE,
)


def memory_recap_evidence_fallback(user_text: str, summary: str, *, verified: bool) -> str:
    """Use prior read-only memory evidence, never invent facts or replay writes.

    The caller supplies the existing bounded summary only after successful,
    authorized manage_memory:list execution. We additionally require an
    explicit user request for this personal memory information.
    """
    if not verified or not isinstance(summary, str):
        return ""
    if not isinstance(user_text, str) or not _RECAP_REQUEST.search(user_text):
        return ""
    listing = summary.strip()
    if not (
        listing.startswith("Memory:")
        or listing == "No saved memories found."
    ):
        return ""
    # The upstream memory summary already caps the number of entries, but keep
    # a final output-size guard in case its formatting changes later.
    if len(listing) > 8000:
        listing = listing[:7900].rsplit("\n", 1)[0].rstrip() + "\n... (listing truncated)"
    if listing == "No saved memories found.":
        return listing
    return (
        "I retrieved these saved memories, but couldn't reliably turn them "
        "into a conversational summary. Here is the bounded listing itself:\n\n"
        + listing
        + "\n\nThese are stored entries, not a verified biography."
    )
