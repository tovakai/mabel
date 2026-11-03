"""Narrow, non-persona presentation cue for personal-memory overviews.

The owner's persona prompt remains authoritative. This module changes only
the response *format* for explicit personal-overview questions, never memory
retrieval or permission to access private data.
"""
from __future__ import annotations

import re
import json


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


_PERSONAL_RECAP_QUESTION = re.compile(
    r"\b(?:what (?:do )?you (?:know|remember) about me|"
    r"tell me (?:what|all|everything) you (?:know|remember) about me)\b",
    re.IGNORECASE,
)


def personal_overview_prose_requested(user_text: str) -> bool:
    """The request is a personal interpretation, not an explicit inventory."""
    text = str(user_text or "")
    return bool(_PERSONAL_RECAP_QUESTION.search(text)) and not bool(
        _EXPLICIT_INVENTORY_FORMAT.search(text)
    )


def personal_overview_read_evidence(user_text: str, tool_events: list) -> str:
    """Only successful memory-list outputs from THIS run qualify for synthesis.

    No new read or permission is introduced; the caller already executed these
    tools through the existing authorization boundary.
    """
    if not personal_overview_prose_requested(user_text):
        return ""
    for event in reversed(tool_events or []):
        if not isinstance(event, dict):
            continue
        if event.get("tool") != "manage_memory" or event.get("exit_code") != 0:
            continue
        if event.get("blocked") or event.get("error") or event.get("approval_required"):
            continue
        command = str(event.get("command") or "").strip()
        try:
            args = json.loads(command)
            action = str(args.get("action") or "").lower() if isinstance(args, dict) else ""
        except (ValueError, TypeError):
            action = command.splitlines()[0].lower() if command else ""
        if action not in {"list", "index"}:
            continue
        evidence = event.get("output")
        if isinstance(evidence, str) and evidence.strip():
            return evidence[:8000]
    return ""
