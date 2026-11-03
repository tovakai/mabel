"""Conservative per-turn prompt-surface stabilization for self-hosted web agents.

Cache-affinity is only a *hint* to the local inference backend. Keeping an
identical tool-schema prefix removes one avoidable cache-busting source, but
never creates a permission grant or promises a cache hit.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json


def digest_schema_surface(schemas: list[dict] | None) -> str:
    """Diagnostic fingerprint only. Never log schema bodies or credentials."""
    body = json.dumps(schemas or [], sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return sha256(body).hexdigest()[:16]


def digest_system_prefix(messages: list[dict] | None) -> str:
    """Digest byte-relevant system-role text at the start of the message list."""
    head=[]
    for message in messages or ():
        if not isinstance(message, dict) or message.get("role")!="system":
            break
        head.append({"role":"system","content":message.get("content")})
    return sha256(json.dumps(head,sort_keys=True,separators=(",",":"),
                             ensure_ascii=False,default=str).encode()).hexdigest()[:16]


def fast_web_round_budget(
    model: str, max_tokens: int, thinking_mode: str | None,
    *, round_num: int, pure_web_turn: bool, has_tools: bool,
) -> tuple[int,str | None]:
    """Cap non-final Qwen web-tool continuations without changing other turns.

    Explicit thinking=on always wins. We never set a global reasoning budget
    or impose this on coding, artifact generation, research, or final synthesis.
    """
    requested = int(max_tokens or 0)
    normalized = str(thinking_mode or "").strip().lower()
    eligible = (
        pure_web_turn and has_tools and round_num > 1
        and "qwen3.8-flash-next" in str(model).casefold()
        and normalized != "on" and requested > 0
    )
    if not eligible:
        return requested, thinking_mode
    return min(requested, 1536), "off"


class StableWebSchemas:
    """Freeze a single authorized web surface if later relevance merely shrinks it.

    This does not enlarge executable permissions. If any frozen name is now
    denied, dropped by contract, or unavailable, use the newly filtered
    inventory instead. A model/endpoint/tool-surface change also resets the
    cache candidate. Never freeze when task approval, force-final, artifact
    repair, or capability recovery intentionally changed the workflow.
    """

    def __init__(self) -> None:
        self._baseline: list[dict] | None = None
        self._route_key: tuple | None = None

    def choose(
        self, schemas: list[dict] | None, *,
        route_key: tuple, round_num: int, pure_web_turn: bool,
        safe_to_freeze: bool, allowed_names: set[str],
    ) -> tuple[list[dict], str]:
        current = list(schemas or ())
        names = [
            schema.get("function",{}).get("name", schema.get("name"))
            for schema in current
        ]
        if not pure_web_turn or not safe_to_freeze:
            return current, "dynamic_required"
        if self._baseline is None:
            if round_num != 1:
                return current, "no_baseline"
            self._baseline = deepcopy(current)
            self._route_key = route_key
            return current, "baseline"
        if route_key != self._route_key:
            return current, "route_changed"
        original = [
            schema.get("function",{}).get("name",schema.get("name"))
            for schema in self._baseline
        ]
        if any(name not in allowed_names for name in original):
            return current, "authorization_changed"
        if current == self._baseline:
            return current, "unchanged"
        if not set(names).issubset(set(original)):
            return current, "capability_added"
        return deepcopy(self._baseline), "stable_web_prefix"
