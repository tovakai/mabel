"""One-shot, server-owned continuation for a reviewed vault batch preview.

A preview is *not* a write grant. Only a subsequent, explicit, trusted user
confirmation can turn its exact apply arguments into a single-use operation.
Nothing in model-generated tool arguments can create or widen that authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from threading import Lock
from time import monotonic

_TTL_SECONDS = 900
_MAX_PREVIEWS = 128
_MAX_ACTIVE = 128
_PLAN_ID = re.compile(r"[0-9a-f]{64}\Z")
_CONFIRMATIONS = frozenset({
    "apply", "apply it", "apply the changes", "apply the preview",
    "apply the batch", "yes apply", "yes, apply",
    "yes, apply it", "yes, apply the changes",
    "go ahead and apply", "go ahead and apply it",
    "proceed with the batch", "apply the previewed changes",
})


@dataclass(frozen=True)
class _Preview:
    owner: str
    session_id: str
    workspace: str
    apply_input: str
    expires_at: float


@dataclass
class _Active:
    apply_input: str
    expires_at: float
    used: bool = False


_lock = Lock()
_previews: dict[tuple[str, str, str], _Preview] = {}
_active: dict[str, _Active] = {}


def _scope(owner, session_id, workspace):
    # Never carry authority between anonymous/unbound agent runs.
    if not all(isinstance(v, str) and v.strip() for v in (owner, session_id, workspace)):
        return None
    return (owner.strip().casefold(), session_id, workspace)


def _cleanup(now):
    for key, value in tuple(_previews.items()):
        if value.expires_at <= now:
            _previews.pop(key, None)
    for key, value in tuple(_active.items()):
        if value.expires_at <= now:
            _active.pop(key, None)


def remember_preview(*, owner, session_id, workspace, payload, result, authority):
    """Retain a successfully authorized preview. Returns canonical apply args.

    This is invoked by the server's tool handler after planning, never from a
    request-supplied plan_id alone.
    """
    from src.agent_runtime.authority import ExactOperation, RequestAuthority
    from src.agent_tools.vault_batch_tools import _validated_args

    scope = _scope(owner, session_id, workspace)
    if scope is None or not isinstance(payload, dict) or not isinstance(result, dict):
        return None
    if result.get("status") != "preview" or not result.get("to_change"):
        return None
    plan_id = result.get("plan_id")
    if not isinstance(plan_id, str) or not _PLAN_ID.fullmatch(plan_id):
        return None
    if not isinstance(authority, RequestAuthority) or not authority.bound_to(
        owner=owner, session_id=session_id, workspace=workspace
    ):
        return None
    preview_input = json.dumps(payload, ensure_ascii=False)
    if not authority.permits(ExactOperation.normalize("vault_batch_edit", preview_input)):
        return None
    args = _validated_args(payload)
    if args["mode"] != "preview":
        return None
    apply_args = {**args, "mode": "apply", "plan_id": plan_id}
    apply_input = ExactOperation.normalize(
        "vault_batch_edit", json.dumps(apply_args, ensure_ascii=False)
    ).input
    now = monotonic()
    with _lock:
        _cleanup(now)
        if len(_previews) >= _MAX_PREVIEWS:
            _previews.pop(next(iter(_previews)))
        _previews[scope] = _Preview(*scope, apply_input, now + _TTL_SECONDS)
    return apply_args


def activate_explicit_confirmation(*, request_text, owner, session_id, workspace, request_id):
    """Reserve a preview for one trusted, unambiguous confirmation turn.

    The caller must *also* validate current policy and install a single exact
    OperationGrant. This function never grants permission itself.
    """
    if not isinstance(request_text, str) or not isinstance(request_id, str) or not request_id:
        return None
    text = re.sub(r"[.!]+$", "", request_text.strip().casefold()).strip()
    if text not in _CONFIRMATIONS:
        return None
    scope = _scope(owner, session_id, workspace)
    if scope is None:
        return None
    now = monotonic()
    with _lock:
        _cleanup(now)
        preview = _previews.pop(scope, None)
        if preview is None:
            return None
        if len(_active) >= _MAX_ACTIVE:
            _active.pop(next(iter(_active)))
        _active[request_id] = _Active(preview.apply_input, now + _TTL_SECONDS)
        return preview.apply_input


def consume_if_continuation(*, request_id, operation):
    """None for regular requests, True exactly once, False for mismatch/replay.

    This is checked at the central dispatcher, even when the exact authority
    grant remains present for multiple model rounds in the same user turn.
    """
    if not isinstance(request_id, str) or not request_id.startswith("batch:"):
        return None
    now = monotonic()
    with _lock:
        _cleanup(now)
        active = _active.get(request_id)
        if active is None:
            # A missing/expired/evicted continuation must NEVER become a
            # normal request just because its in-memory claim disappeared.
            return False
        if active.used or operation.tool != "vault_batch_edit" or operation.input != active.apply_input:
            return False
        active.used = True
        return True
