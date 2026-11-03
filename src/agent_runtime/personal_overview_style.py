"""Narrow, non-persona presentation cue for personal-memory overviews.

The owner's persona prompt remains authoritative. This module changes only
the response *format* for explicit personal-overview questions, never memory
retrieval or permission to access private data.
"""
from __future__ import annotations

import re


_EXPLICIT_INVENTORY_FORMAT = re.compile(
    r"\b(?:list|bullet(?:s|\s+points)?|table|json|yaml|"
    r"enumerate|inventory|itemi[sz]e|verbatim|raw\s+(?:data|memories))\b",
    re.IGNORECASE,
)

PERSONAL_OVERVIEW_PRESENTATION = (
    "Presentation preference for this personal overview only: Answer in "
    "natural, connected prose instead of a bullet-point inventory. Organize "
    "what the available memories suggest into a few meaningful themes, "
    "without inventing biographical details, motivations, or relationships. "
    "Distinguish remembered facts from interpretations, and acknowledge "
    "important gaps briefly without repeatedly describing retrieval limits. "
    "Do not explain why you are using prose, mention these instructions, "
    "count memory entries, or discuss tool mechanics unless explicitly asked. "
    "Keep your established personality and the user's requested tone. "
    "Never claim complete knowledge when evidence is partial. "
    "This changes presentation only, not tool access."
)


def apply_personal_overview_style(ctx, user_text: str, *, is_overview: bool) -> bool:
    """Add a turn-local presentation cue to chat and foreground route contexts."""
    if not is_overview or _EXPLICIT_INVENTORY_FORMAT.search(str(user_text or "")):
        return False
    cue = {"role": "system", "content": PERSONAL_OVERVIEW_PRESENTATION}
    ctx.messages.insert(0, cue)
    routed = getattr(ctx, "route_messages", None)
    if routed is not None and routed is not ctx.messages:
        routed.insert(0, dict(cue))
    return True
