"""Keep an agent turn's working context across an approval pause.

An approval ends the HTTP request; the approval click starts a new one that is
rebuilt from saved chat history, which holds reply text but not tool calls or
tool results. Without this store the resumed turn has forgotten every file it
read and re-explores from scratch after each approval.

The store is server-only and in memory, bound to the approval id, owner and
session, and expires with the approval. It carries model context only: it is
never shown to the browser, never consulted for authorization, and restoring it
grants nothing. Restored tool results keep their untrusted-context metadata, so
the run-security gate observes them again on the next model request.
"""
from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

# Upper bound on restored context. Older tool results are elided first; the
# model is told to re-read only what it still needs.
MAX_CONTINUATION_CHARS = 60_000
_ELIDED = "[Earlier tool result omitted to fit context; re-read it only if you still need it.]"
_MAX_ENTRIES = 256


@dataclass(frozen=True)
class TurnContinuation:
    owner: str
    session_id: str
    messages: tuple[dict, ...]
    placeholder_call_id: str
    expires_at: float


_lock = threading.Lock()
_store: dict[str, TurnContinuation] = {}


def _content_chars(message: dict) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(str(block.get("text") or "")) for block in content if isinstance(block, dict))
    return 0


def _bounded(messages: list[dict]) -> list[dict]:
    total = sum(_content_chars(m) for m in messages)
    for message in messages:
        if total <= MAX_CONTINUATION_CHARS:
            break
        if message.get("role") == "tool" or (message.get("metadata") or {}).get("trusted") is False:
            before = _content_chars(message)
            if before > len(_ELIDED):
                message["content"] = _ELIDED
                total -= before - len(_ELIDED)
    return messages


def turn_work_messages(messages: list[dict], last_user: str) -> list[dict]:
    """The tool-call/result messages produced after the current user request."""
    needle = str(last_user or "").strip()[:200]
    if not needle:
        return []
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        if (message.get("metadata") or {}).get("trusted") is False:
            continue  # textual tool output wrapped as a user message
        content = message.get("content")
        text = content if isinstance(content, str) else " ".join(
            str(block.get("text") or "") for block in (content or []) if isinstance(block, dict)
        )
        if needle in text:
            return [m for m in messages[index + 1:] if isinstance(m, dict)]
    return []


def save(approval_id: str, *, owner: str, session_id: str, messages: list[dict],
         placeholder_call_id: str = "", expires_at: Optional[float] = None) -> bool:
    work = [m for m in messages if m.get("role") in {"assistant", "tool", "user"}]
    if not approval_id or not work:
        return False
    snapshot = _bounded(copy.deepcopy(work))
    for message in snapshot:
        message.pop("reasoning_content", None)
    now = time.time()
    entry = TurnContinuation(
        owner=str(owner or ""), session_id=str(session_id or ""), messages=tuple(snapshot),
        placeholder_call_id=str(placeholder_call_id or ""),
        expires_at=float(expires_at) if expires_at else now + 600,
    )
    with _lock:
        for key in [k for k, v in _store.items() if v.expires_at <= now]:
            _store.pop(key, None)
        while len(_store) >= _MAX_ENTRIES:
            _store.pop(next(iter(_store)))
        _store[str(approval_id)] = entry
    return True


def take(approval_id: Any, *, owner: Any, session_id: Any) -> Optional[TurnContinuation]:
    """One-use: return and remove the continuation if it belongs to this caller."""
    with _lock:
        entry = _store.pop(str(approval_id or ""), None)
    if entry is None or entry.expires_at <= time.time():
        return None
    if entry.owner != str(owner or "") or entry.session_id != str(session_id or ""):
        return None
    return entry


def restore(entry: TurnContinuation, approved_result_text: str) -> tuple[list[dict], bool]:
    """Messages to append on resume.

    The approval placeholder for the sealed call is replaced by the approved
    result, so the model sees one coherent call -> result. Returns whether that
    replacement happened; if not, the caller appends the result as before.
    """
    messages = copy.deepcopy(list(entry.messages))
    replaced = False
    if entry.placeholder_call_id:
        for message in reversed(messages):
            if message.get("role") == "tool" and message.get("tool_call_id") == entry.placeholder_call_id:
                message["content"] = approved_result_text
                replaced = True
                break
    return messages, replaced


def clear() -> None:
    with _lock:
        _store.clear()
