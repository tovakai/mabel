"""Canonical history compaction after a successful provider context recovery.

The provider retry stream may trim a wire snapshot. The agent loop must not
adopt that snapshot as canonical state: it strips ``_protected`` markers and
other harness metadata. Instead, apply the learned limit to the live message
list through ``trim_for_context``, which preserves protected tails and tool
call/result integrity.
"""
from __future__ import annotations

from typing import Any, Optional


def compact_canonical_messages_for_limit(
    messages: list[dict],
    context_limit: int,
    *,
    tools: Optional[list] = None,
    max_tokens: int = 1024,
) -> list[dict]:
    """Return canonical messages compacted to fit ``context_limit``.

    Empty/invalid limits leave the input unchanged. The returned list may share
    nested objects with the input when no trim is required.
    """
    try:
        limit = int(context_limit)
    except (TypeError, ValueError):
        return list(messages or [])
    if limit <= 0:
        return list(messages or [])
    from src.context_compactor import trim_for_context
    from src.generation_budget import (
        context_safety_margin,
        estimate_tool_schema_tokens,
    )

    try:
        recovery_max = int(max_tokens or 1024)
    except (TypeError, ValueError):
        recovery_max = 1024
    reserve = min(max(recovery_max, 512), 2048) + context_safety_margin(limit)
    # Keep history room on small recovered windows so the next round cannot
    # immediately re-overflow after a "successful" commit.
    reserve = min(reserve, max(256, limit // 2))
    message_budget = max(1, limit - estimate_tool_schema_tokens(tools))
    return trim_for_context(
        list(messages or []),
        message_budget,
        reserve_tokens=reserve,
    )


def answering_recovery_bag(
    bags: dict[int, Any] | None,
    candidate_index: int | None,
) -> Optional[dict]:
    """Return the applied recovery bag for the answering candidate, if any."""
    if not isinstance(bags, dict):
        return None
    try:
        index = int(candidate_index if candidate_index is not None else 0)
    except (TypeError, ValueError):
        index = 0
    bag = bags.get(index)
    if isinstance(bag, dict) and bag.get("applied") and not bag.get("committed"):
        return bag
    return None
