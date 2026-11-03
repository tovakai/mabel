"""Opaque exact-action approvals with explicit task and chat scopes.

The server seals and claims the displayed action exactly once. Missing-authority
consent can cover repeated uses of that exact operation and its sealed resources
for a bounded task/chat lifetime. Legacy external-context consent bypasses only
that gate; it never supplies missing request authority. Browser fields are
display copies, never authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from src.tool_approval_scopes import (
    CHAT_SESSION_APPROVAL_DECISION,
    DENY_APPROVAL_DECISION,
    TASK_APPROVAL_DECISION,
    SINGLE_ACTION_APPROVAL_DECISION,
    ToolApprovalScope,
    scope_for_decision,
)
from src.tool_capabilities import ToolCapabilities, capabilities_for_action
from src.agent_runtime.authority import RequestAuthority

if TYPE_CHECKING:
    from src.agent_runtime.resource_binding import BoundFilesystemOperation
    from src.agent_runtime.remote_resources import BoundBackendOperation
    from src.agent_runtime.owned_resources import BoundOwnedOperation
    from src.agent_runtime.process_resources import BoundProcessOperation
    from src.browser_identity import BoundBrowserOperation


DEFAULT_APPROVAL_TTL_SECONDS = 10 * 60
DEFAULT_MAX_PENDING_APPROVALS = 2048


def _normalized_owner(owner: Any) -> str:
    return str(owner or "").strip().casefold()


def _normalized_workspace(workspace: Any) -> str:
    if not isinstance(workspace, str) or not workspace.strip():
        return ""
    return os.path.realpath(os.path.expanduser(workspace))


_MAX_APPROVAL_SELECTED_TOOLS = 512
_MAX_APPROVAL_TOOL_NAME_CHARS = 512
_MAX_APPROVAL_CONTINUATION_QUERY_CHARS = 4000


def _normalized_selected_tools(
    selected_tools: Any,
    *,
    required_tool: Any = None,
) -> tuple[str, ...]:
    if isinstance(selected_tools, str):
        selected_tools = (selected_tools,)
    try:
        values = selected_tools or ()
        names = {
            name.strip()
            for name in values
            if (
                isinstance(name, str)
                and name.strip()
                and len(name.strip()) <= _MAX_APPROVAL_TOOL_NAME_CHARS
            )
        }
        required_name = str(required_tool or "").strip()
        if required_name and len(required_name) <= _MAX_APPROVAL_TOOL_NAME_CHARS:
            names.add(required_name)
        ordered = sorted(names)
        if len(ordered) <= _MAX_APPROVAL_SELECTED_TOOLS:
            return tuple(ordered)
        kept = ordered[:_MAX_APPROVAL_SELECTED_TOOLS]
        if required_name and required_name in names and required_name not in kept:
            kept[-1] = required_name
            kept.sort()
        return tuple(kept)
    except TypeError:
        return ()


def _normalized_continuation_query(value: Any) -> str:
    # The query is server-derived from the interrupted run and already lives in
    # session history. Keep the pending copy bounded because approvals are held
    # in memory until consumed or expired.
    return str(value or "").strip()[:_MAX_APPROVAL_CONTINUATION_QUERY_CHARS]


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def document_content_digest(content: Any) -> str:
    """Return the stable server-side fingerprint used to seal a document."""
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _binding_payload(
    *,
    owner: Any,
    session_id: Any,
    origin_run_id: Any,
    tool_name: Any,
    content: Any,
    workspace: Any,
    document_id: Any,
    document_version: Any,
    document_digest: Any,
    external_untrusted_context_seen: bool,
    selected_tools: Any,
    continuation_query: Any,
    effects: tuple[str, ...],
    result_integrity: str,
    request_authority: RequestAuthority | None = None,
    resource_operation=None,
    backend_operation=None,
    owned_operation=None,
    process_operation=None,
    browser_operation=None,
    authority_gap: bool = False,
) -> dict[str, Any]:
    return {
        "authority_gap": bool(authority_gap),
        "owner": _normalized_owner(owner),
        "session_id": str(session_id or ""),
        "origin_run_id": str(origin_run_id or ""),
        "tool_name": str(tool_name or ""),
        "content": str(content or ""),
        "workspace": _normalized_workspace(workspace),
        "document_id": str(document_id or ""),
        "document_version": (
            int(document_version) if document_version is not None else None
        ),
        "document_digest": str(document_digest or "").strip().lower(),
        "external_untrusted_context_seen": bool(external_untrusted_context_seen),
        "selected_tools": list(
            _normalized_selected_tools(selected_tools, required_tool=tool_name)
        ),
        "continuation_query": _normalized_continuation_query(continuation_query),
        "effects": list(effects),
        "result_integrity": str(result_integrity),
        "request_authority": request_authority.to_dict() if request_authority is not None else None,
        "resource_operation": resource_operation.to_dict() if resource_operation is not None else None,
        "backend_operation": backend_operation.to_dict() if backend_operation is not None else None,
        "owned_operation": owned_operation.to_dict() if owned_operation is not None else None,
        "process_operation": process_operation.to_dict() if process_operation is not None else None,
        "browser_operation": browser_operation.to_dict() if browser_operation is not None else None,
    }


@dataclass(frozen=True)
class PendingToolApproval:
    approval_id: str
    owner: str
    session_id: str
    origin_run_id: str
    tool_name: str
    content: str
    workspace: str
    document_id: str
    document_version: int | None
    document_digest: str
    external_untrusted_context_seen: bool
    effects: tuple[str, ...]
    result_integrity: str
    digest: str
    created_at: float
    expires_at: float
    # Server-only continuation state. Both fields are digest-bound and never
    # exposed in the browser payload.
    selected_tools: tuple[str, ...] = ()
    continuation_query: str = ""
    # The originating user request is internal continuation context only; it
    # is never displayed or treated as authorization for the sealed action.
    request_text: str = ""
    request_authority: RequestAuthority | None = None
    # Server-resolved targets at proposal time; never read from the approval UI.
    resource_operation: BoundFilesystemOperation | None = None
    backend_operation: BoundBackendOperation | None = None
    owned_operation: BoundOwnedOperation | None = None
    process_operation: BoundProcessOperation | None = None
    browser_operation: BoundBrowserOperation | None = None
    # Digest-bound. True when the run-security gate allowed the call and only
    # a missing request-authority grant blocked it. Approving admits exactly
    # this sealed operation; it never bypasses a gate for later calls.
    authority_gap: bool = False

    def public_payload(self, *, reason: str | None = None) -> dict[str, Any]:
        if self.authority_gap:
            return {
                "kind": "tool_approval",
                "approval_id": self.approval_id,
                "session_id": self.session_id,
                "question": "Allow this action?",
                "description": reason or (
                    "Your request did not cover this action, so it needs your approval."
                ),
                "authority_gap": True,
                "expires_at": self.expires_at,
                "options": [
                    {
                        "label": "Allow once",
                        "value": SINGLE_ACTION_APPROVAL_DECISION,
                        "description": (
                            "Run exactly this action, then continue. Later actions "
                            "your request did not cover will ask again."
                        ),
                    },
                    {"label":"Allow this exact operation for this task", "value":TASK_APPROVAL_DECISION,
                     "description":"Same tool, arguments, backend and sealed resources only; expires with this task or the displayed approval lifetime."},
                    {"label":"Allow this exact operation for this chat", "value":CHAT_SESSION_APPROVAL_DECISION,
                     "description":"Same operation and sealed resources only, while this card remains in this chat and until the displayed expiry. Other operations ask again."},
                    {
                        "label": "Deny",
                        "value": DENY_APPROVAL_DECISION,
                        "description": "Do not run the proposed action.",
                    },
                ],
                "action": self._public_action(),
            }
        return {
            "kind": "tool_approval",
            "approval_id": self.approval_id,
            # The browser already owns this chat id. Persisting it with the
            # resolved card lets history-derived session grants remain bound to
            # this exact chat and prevents inheritance by a forked session.
            "session_id": self.session_id,
            "question": "Allow this task to continue?",
            "description": reason or (
                "Untrusted context influenced this run, so continuing with "
                "otherwise-gated actions needs your explicit approval."
            ),
            "options": [
                {"label":"Allow once", "value":SINGLE_ACTION_APPROVAL_DECISION,
                 "description":"Execute only this sealed action; keep other approval gates active."},
                {
                    "label": "Allow for this task",
                    "value": TASK_APPROVAL_DECISION,
                    "description": (
                        "Execute the sealed action and allow every otherwise-gated "
                        "action needed to finish this request. Current tool, account, "
                        "workspace, and sandbox restrictions still apply."
                    ),
                },
                {
                    "label": "Allow for this chat session",
                    "value": CHAT_SESSION_APPROVAL_DECISION,
                    "description": (
                        "Execute the sealed action and stop asking at this gate for "
                        "later requests in this chat. Current tool, account, workspace, "
                        "and sandbox restrictions still apply."
                    ),
                },
                {
                    "label": "Deny",
                    "value": DENY_APPROVAL_DECISION,
                    "description": "Do not execute the proposed action.",
                },
            ],
            "action": self._public_action(),
        }

    def _public_action(self) -> dict[str, Any]:
        return {
            "tool": self.tool_name,
            # Show the complete sealed input so approval never hides
            # trailing lines.  This is not read back as authority.
            "content": self.content,
            "digest": self.digest[:16],
            "effects": list(self.effects),
            "workspace": self.workspace or None,
            "document_id": self.document_id or None,
            "document_version": self.document_version,
            "resources": self.public_resources(),
            "execution_environment": self.execution_environment(),
        }

    def public_resources(self):
        resources = []
        if self.resource_operation is not None:
            resources.extend({'path':b.resource.path, 'role':b.role} for b in self.resource_operation.bindings)
        if self.process_operation is not None and self.process_operation.launch is not None:
            resources.append({'path':self.process_operation.launch.scope.root.path, 'role':'process_workspace'})
        if self.owned_operation is not None:
            resources.extend({'path':r.record_id, 'role':r.namespace} for r in self.owned_operation.resources)
        if self.backend_operation is not None:
            from src.agent_runtime.resources import ExternalResource
            backend = self.backend_operation.resource
            if isinstance(backend, ExternalResource):
                # endpoint_id is a credential-free origin, never the original
                # configured URL/path/query, credentials or incarnation token.
                resources.append({'path':backend.endpoint_id, 'role':backend.namespace + ':' + backend.server_id})
        return resources

    def execution_environment(self):
        if self.process_operation is not None and self.process_operation.launch is not None:
            from src.agent_tools.subprocess_tools import _owned_spec
            from src.tool_execution import _agent_subprocess_env
            from src.containment import probe
            scope = self.process_operation.launch.scope
            try:
                available = probe(_owned_spec(scope.root.path, _agent_subprocess_env(), scope.max_runtime_s,
                    tuple(r.path for r in scope.runtime_roots)))
                return f"Required containment: {', '.join(sorted(scope.required))}; mechanism: {available.mechanism}; available: {available.contained}"
            except (ValueError, OSError, RuntimeError):
                return "Required process containment is unavailable; approval cannot bypass it"
        if self.resource_operation is not None:
            return "Native filesystem access confined to sealed workspace resources"
        if self.backend_operation is not None:
            from src.agent_runtime.resources import NativeBackendResource
            return "Native application operation; no shell execution" if isinstance(self.backend_operation.resource, NativeBackendResource) else "External backend; execution isolation is managed by that backend"
        return "Unavailable: backend identity could not be sealed"


@dataclass
class ExactToolApproval:
    """A consumed exact first action plus an explicit continuation scope."""

    pending: PendingToolApproval
    scope: ToolApprovalScope = ToolApprovalScope.TASK
    # Only legacy external-context consent can bypass that gate for the
    # resumed task. Missing-authority consent always revalidates exact inputs
    # and resources; it never grants a blanket continuation bypass.
    allow_remaining_actions: bool = True
    _claimed: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def grants_chat_session(self) -> bool:
        return self.scope is ToolApprovalScope.CHAT_SESSION

    def _matches_unlocked(
        self,
        *,
        owner: Any,
        session_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
    ) -> bool:
        if self._claimed:
            return False
        capabilities = capabilities_for_action(tool_name, content)
        effects = tuple(sorted(effect.value for effect in capabilities.effects))
        result_integrity = capabilities.result_integrity.value
        if (
            effects != self.pending.effects
            or result_integrity != self.pending.result_integrity
        ):
            return False
        expected = _binding_payload(
            owner=owner,
            session_id=session_id,
            origin_run_id=self.pending.origin_run_id,
            tool_name=tool_name,
            content=content,
            workspace=workspace,
            document_id=self.pending.document_id,
            document_version=self.pending.document_version,
            document_digest=self.pending.document_digest,
            external_untrusted_context_seen=(
                self.pending.external_untrusted_context_seen
            ),
            selected_tools=self.pending.selected_tools,
            continuation_query=self.pending.continuation_query,
            effects=effects,
            result_integrity=result_integrity,
            request_authority=self.pending.request_authority,
            resource_operation=self.pending.resource_operation,
            backend_operation=self.pending.backend_operation,
            owned_operation=self.pending.owned_operation,
            process_operation=self.pending.process_operation,
            browser_operation=self.pending.browser_operation,
            authority_gap=self.pending.authority_gap,
        )
        return _canonical_digest(expected) == self.pending.digest

    def matches(
        self,
        *,
        owner: Any,
        session_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
    ) -> bool:
        with self._lock:
            return self._matches_unlocked(
                owner=owner,
                session_id=session_id,
                tool_name=tool_name,
                content=content,
                workspace=workspace,
            )

    def claim(
        self,
        *,
        owner: Any,
        session_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
    ) -> bool:
        with self._lock:
            if not self._matches_unlocked(
                owner=owner,
                session_id=session_id,
                tool_name=tool_name,
                content=content,
                workspace=workspace,
            ):
                return False
            self._claimed = True
            return True


class ToolApprovalStore:
    """Thread-safe pending approval registry with destructive consumption."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS,
        max_pending: int = DEFAULT_MAX_PENDING_APPROVALS,
    ):
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._max_pending = max(1, int(max_pending))
        self._pending: dict[str, PendingToolApproval] = {}
        self._denied_operations = {}
        self._scoped = {}
        self._lock = threading.Lock()

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            approval_id
            for approval_id, pending in self._pending.items()
            if pending.expires_at <= now
        ]
        for approval_id in expired:
            self._pending.pop(approval_id, None)

    def operation_was_denied(self, authority, operation):
        if authority is None:
            return False
        key = (authority.owner, authority.session_id, authority.workspace, authority.request_id,
               operation.tool, operation.input)
        with self._lock:
            now = time.time()
            self._denied_operations = {k:v for k,v in self._denied_operations.items() if v > now}
            return key in self._denied_operations

    def create(
        self,
        *,
        owner: Any,
        session_id: Any,
        origin_run_id: Any,
        tool_name: Any,
        content: Any,
        workspace: Any,
        document_id: Any = None,
        document_version: Any = None,
        document_digest: Any = None,
        selected_tools: Any = None,
        continuation_query: Any = None,
        external_untrusted_context_seen: bool,
        capabilities: ToolCapabilities,
        request_text: Any = "",
        request_authority: RequestAuthority | None = None,
        client_runtime_context: dict | None = None,
        authority_gap: bool = False,
    ) -> PendingToolApproval:
        if request_authority is not None and not isinstance(request_authority, RequestAuthority):
            raise TypeError("Approval authority must be server-owned RequestAuthority")
        now = time.time()
        from src.agent_runtime.authority import ExactOperation
        from src.agent_runtime.resource_binding import NATIVE_FILESYSTEM_TOOLS, resolve_filesystem_operation
        from src.agent_runtime.resources import FilesystemRoot
        resource_operation = None
        backend_operation = None
        owned_operation = None
        process_operation = None
        browser_operation = None
        from src.agent_runtime.remote_resources import BoundBackendOperation, resolve_backend
        from src.agent_runtime.owned_resources import needs_owned_binding, resolve_owned_operation
        from src.agent_runtime.resources import NativeBackendResource
        try:
            operation = ExactOperation.normalize(tool_name, content)
            backend = resolve_backend(operation.transport_tool, context=client_runtime_context, content=operation.input, owner=_normalized_owner(owner))
            if request_authority is not None and request_authority.inherited:
                if not request_authority.permits(operation) or backend not in request_authority.backend_resources:
                    raise ValueError("Child approval exceeds originating authority")
            backend_operation = BoundBackendOperation(backend,
                request_authority.request_id if request_authority is not None else "",
                _normalized_owner(owner), str(session_id or ""), operation.transport_tool, operation.input)
            from src.agent_runtime.process_resources import (
                LAUNCH_TOOLS, needs_process_binding, resolve_process_operation, seal_launch_scope)
            if request_authority is not None and needs_process_binding(operation, backend):
                process_authority = request_authority
                if (not request_authority.inherited and operation.tool in LAUNCH_TOOLS
                        and not any(s.backend == backend for s in request_authority.launch_scopes)):
                    # Launch scopes exist only for granted bash/python, but the
                    # approval exists to admit an ungranted one. Seal its launch
                    # boundary now, in the request's workspace, the same way an
                    # ungranted filesystem action gets its root below (#6635).
                    # With no workspace bound, bash/python run in the agent
                    # workspace (agent_cwd), so the launch is sealed there.
                    roots = request_authority.resource_roots
                    if not roots:
                        from src.tool_execution import default_agent_workspace
                        roots = (FilesystemRoot.seal(workspace or default_agent_workspace(),
                                                     owner=_normalized_owner(owner)),)
                    if len(roots) == 1:
                        from dataclasses import replace
                        process_authority = replace(
                            request_authority, launch_scopes=(seal_launch_scope(backend, roots[0]),))
                process_operation = resolve_process_operation(process_authority, operation, backend)
            from src.browser_identity import native_browser, resolve_browser_operation
            if native_browser(operation, backend):
                if request_authority is None:
                    raise ValueError("Browser approval requires originating resource authority")
                browser_operation = resolve_browser_operation(request_authority, operation)
            if isinstance(backend, NativeBackendResource) and needs_owned_binding(operation):
                resolved_owned = resolve_owned_operation(operation, owner=_normalized_owner(owner),
                    thread_id=str(session_id or ""), request_id=backend_operation.request_id,
                    document_id=document_id)
                if request_authority is not None and request_authority.inherited:
                    if not all(any(scope.permits(r) for scope in request_authority.owned_scopes) for r in resolved_owned.resources):
                        raise ValueError("Child approval exceeds originating record scope")
                owned_operation = resolved_owned
                if owned_operation.document_id:
                    document_id = owned_operation.document_id
                    document_version = owned_operation.document_version
                    document_digest = owned_operation.document_digest
        except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
            pass  # Unresolved proposals are never reconstructed at execution.
        if tool_name in NATIVE_FILESYSTEM_TOOLS and backend_operation is not None and isinstance(backend_operation.resource, NativeBackendResource):
            try:
                roots = request_authority.resource_roots if request_authority is not None else ()
                if not roots and (request_authority is None or not request_authority.inherited):
                    from src.tool_execution import default_agent_workspace
                    roots = (FilesystemRoot.seal(workspace or default_agent_workspace(),
                                                 owner=_normalized_owner(owner)),)
                resource_operation = resolve_filesystem_operation(
                    ExactOperation.normalize(tool_name, content), roots=roots, workspace=workspace or "",
                    request_id=request_authority.request_id if request_authority is not None else "")
            except (ValueError, TypeError, OSError, RuntimeError):
                # An unresolved proposal may be displayed, but it cannot execute
                # after approval by reconstructing its targets at claim time.
                pass
        effects = tuple(sorted(effect.value for effect in capabilities.effects))
        result_integrity = capabilities.result_integrity.value
        payload = _binding_payload(
            owner=owner,
            session_id=session_id,
            origin_run_id=origin_run_id,
            tool_name=tool_name,
            content=content,
            workspace=workspace,
            document_id=document_id,
            document_version=document_version,
            document_digest=document_digest,
            external_untrusted_context_seen=external_untrusted_context_seen,
            selected_tools=selected_tools,
            continuation_query=continuation_query,
            effects=effects,
            result_integrity=result_integrity,
            request_authority=request_authority,
            resource_operation=resource_operation,
            backend_operation=backend_operation,
            owned_operation=owned_operation,
            process_operation=process_operation,
            browser_operation=browser_operation,
            authority_gap=authority_gap,
        )
        pending = PendingToolApproval(
            approval_id=secrets.token_urlsafe(32),
            owner=payload["owner"],
            session_id=payload["session_id"],
            origin_run_id=payload["origin_run_id"],
            tool_name=payload["tool_name"],
            content=payload["content"],
            workspace=payload["workspace"],
            document_id=payload["document_id"],
            document_version=payload["document_version"],
            document_digest=payload["document_digest"],
            external_untrusted_context_seen=payload[
                "external_untrusted_context_seen"
            ],
            effects=effects,
            result_integrity=result_integrity,
            digest=_canonical_digest(payload),
            created_at=now,
            expires_at=now + self._ttl_seconds,
            selected_tools=tuple(payload["selected_tools"]),
            continuation_query=payload["continuation_query"],
            request_text=str(request_text or ""),
            request_authority=request_authority,
            resource_operation=resource_operation,
            backend_operation=backend_operation,
            owned_operation=owned_operation,
            process_operation=process_operation,
            browser_operation=browser_operation,
            authority_gap=payload["authority_gap"],
        )
        with self._lock:
            self._purge_expired_locked(now)
            # The chat UI exposes one pending card per session, so supersede an
            # older action there. Headless/manual-test callers use an empty
            # session id; keep independent origin runs separate so two skill
            # tests owned by the same user cannot invalidate each other.
            superseded = [
                approval_id
                for approval_id, existing in self._pending.items()
                if (
                    existing.owner == pending.owner
                    and existing.session_id == pending.session_id
                    and (
                        bool(pending.session_id)
                        or existing.origin_run_id == pending.origin_run_id
                    )
                )
            ]
            for approval_id in superseded:
                self._pending.pop(approval_id, None)
            while len(self._pending) >= self._max_pending:
                oldest_id = min(
                    self._pending,
                    key=lambda approval_id: self._pending[approval_id].created_at,
                )
                self._pending.pop(oldest_id, None)
            self._pending[pending.approval_id] = pending
        return pending

    def consume(
        self,
        approval_id: Any,
        *,
        decision: Any,
        owner: Any,
        session_id: Any,
        allow_continuation: bool = True,
    ) -> ExactToolApproval | None:
        """Consume a pending approval.

        ``allow_continuation`` is the caller's assertion that it owns a
        resumable conversation the granted scope can apply to. Callers without
        one (the skill tester, unattended audits) pass ``False`` and get the
        original one-use grant, so a button labelled "Allow once" cannot widen
        into a run-long bypass just because the chat card reuses the same wire
        value.
        """
        normalized = str(decision or "").strip().lower()
        if normalized not in {SINGLE_ACTION_APPROVAL_DECISION, TASK_APPROVAL_DECISION,
                              CHAT_SESSION_APPROVAL_DECISION, DENY_APPROVAL_DECISION}:
            return None  # A malformed reconnect must not consume a pending card.
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            approval_key = str(approval_id or "")
            pending = self._pending.get(approval_key)
            if pending is None:
                return None
            if (
                pending.owner != _normalized_owner(owner)
                or pending.session_id != str(session_id or "")
            ):
                # Authentication is checked before destructive consumption so
                # a leaked/guessed opaque id cannot be used to invalidate
                # another owner's pending action.
                return None
            self._pending.pop(approval_key, None)
            if str(decision or "").strip().lower() == "deny" and pending.request_authority is not None:
                from src.agent_runtime.authority import ExactOperation
                op = ExactOperation.normalize(pending.tool_name, pending.content)
                auth = pending.request_authority
                key = (auth.owner, auth.session_id, auth.workspace, auth.request_id, op.tool, op.input)
                self._denied_operations[key] = now + self._ttl_seconds
                for grant_id, (previous, _, _) in list(self._scoped.items()):
                    if (previous.owner, previous.session_id, previous.workspace, previous.tool_name, previous.content) == (pending.owner, pending.session_id, pending.workspace, pending.tool_name, pending.content):
                        self._scoped.pop(grant_id, None)
                while len(self._denied_operations) > self._max_pending:
                    self._denied_operations.pop(next(iter(self._denied_operations)))
        normalized_decision = str(decision or "").strip().lower()
        scope = scope_for_decision(normalized_decision)
        if scope is None:
            return None
        if pending.authority_gap and allow_continuation and scope is not ToolApprovalScope.SINGLE_ACTION:
            with self._lock:
                self._scoped[pending.approval_id] = (pending, scope, secrets.token_urlsafe(32))
                while len(self._scoped) > self._max_pending:
                    self._scoped.pop(next(iter(self._scoped)))
        if not allow_continuation or scope is ToolApprovalScope.SINGLE_ACTION:
            # An authority-gap card allows only "this action": it admits the sealed
            # action and leaves every gate armed for later calls.
            return ExactToolApproval(
                pending,
                scope=ToolApprovalScope.SINGLE_ACTION,
                allow_remaining_actions=False,
            )
        return ExactToolApproval(
            pending,
            scope=scope,
            allow_remaining_actions=not pending.authority_gap,
        )

    def scope_proof(self, approval_id):
        with self._lock:
            value = self._scoped.get(str(approval_id))
            if value is None:
                return None
            _, scope, opaque = value
            return {'approval_id':str(approval_id), 'scope':scope.value, 'token':opaque}

    def peek(self, approval_id: Any) -> PendingToolApproval | None:
        now = time.time()
        with self._lock:
            self._purge_expired_locked(now)
            return self._pending.get(str(approval_id or ""))

    def retire_for_session(self, *, owner: Any, session_id: Any) -> bool:
        """Discard pending actions superseded by an ordinary user turn.

        Returns whether any retired action carried external provenance, so the
        caller can preserve that security state without treating the new user
        message as an approval continuation.
        """
        now = time.time()
        normalized_owner = _normalized_owner(owner)
        normalized_session = str(session_id or "")
        if not normalized_session:
            return False
        with self._lock:
            self._purge_expired_locked(now)
            retired_ids = [
                approval_id
                for approval_id, pending in self._pending.items()
                if (
                    pending.owner == normalized_owner
                    and pending.session_id == normalized_session
                )
            ]
            carried_taint = any(
                self._pending[approval_id].external_untrusted_context_seen
                for approval_id in retired_ids
            )
            for approval_id in retired_ids:
                self._pending.pop(approval_id, None)
        return carried_taint


tool_approval_store = ToolApprovalStore()


def approval_request_result(pending, *, reason=None, client_runtime_context=None):
    """Do not present an executable approval when its resources cannot be sealed.

    Both interactive runtimes use this existing-store adapter. An approval never
    creates a backend, resource identity or a weaker containment mechanism.
    """
    from src.agent_runtime.authority import ExactOperation
    from src.agent_runtime.resource_binding import NATIVE_FILESYSTEM_TOOLS
    from src.agent_runtime.process_resources import needs_process_binding
    from src.agent_runtime.resources import NativeBackendResource
    try:
        operation = ExactOperation.normalize(pending.tool_name, pending.content)
        authority = pending.request_authority
        if authority is not None and (not authority.bound_to(owner=pending.owner, session_id=pending.session_id, workspace=pending.workspace)
                                      or authority.restricted(operation)
                                      or (authority.inherited and not authority.permits(operation))):
            return {'error':'The operation is denied by current authority or request binding.',
                    'exit_code':1, 'blocked':True, 'failure_kind':'request_authority_denied', 'executed':False}
        backend = pending.backend_operation
        if backend is None:
            raise ValueError('The proposed backend identity could not be sealed')
        backend.validate(client_runtime_context)
        if authority is not None and authority.disable_mcp and getattr(backend.resource, 'namespace', '') == 'mcp':
            return {'error':'MCP is disabled by current policy.', 'exit_code':1, 'blocked':True,
                    'failure_kind':'user_policy_denied', 'executed':False}
        if needs_process_binding(operation, backend.resource):
            if pending.process_operation is None:
                raise ValueError('The proposed process/job identity could not be sealed')
            pending.process_operation.validate()
            if pending.process_operation.launch is not None:
                from src.agent_tools.subprocess_tools import _owned_spec
                from src.tool_execution import _agent_subprocess_env
                from src.containment import probe
                scope = pending.process_operation.launch.scope
                spec = _owned_spec(scope.root.path, _agent_subprocess_env(), scope.max_runtime_s,
                                   tuple(r.path for r in scope.runtime_roots))
                available = probe(spec)
                if not available.contained:
                    return {'error':'Required containment is unavailable; approval cannot bypass it.',
                            'exit_code':1, 'failure_kind':'containment_unavailable', 'executed':False,
                            'containment':{'executed':False, 'contained':False,
                                           'unenforced_required':list(available.unenforced_required)}}
        from src.agent_runtime.owned_resources import needs_owned_binding
        from src.browser_identity import native_browser
        if isinstance(backend.resource, NativeBackendResource) and needs_owned_binding(operation):
            if pending.owned_operation is None:
                raise ValueError('The proposed owned-record identity could not be sealed')
            pending.owned_operation.validate()
        if native_browser(operation, backend.resource):
            if pending.browser_operation is None:
                raise ValueError('The proposed browser session/page could not be sealed')
            pending.browser_operation.validate()
        if operation.tool in NATIVE_FILESYSTEM_TOOLS and isinstance(backend.resource, NativeBackendResource):
            if pending.resource_operation is None:
                raise ValueError('The proposed filesystem resource could not be sealed')
            pending.resource_operation.validate()
    except (ValueError, TypeError, OSError, RuntimeError) as error:
        return {'error':str(error), 'exit_code':1, 'blocked':True,
                'failure_kind':'resource_identity_denied', 'executed':False}
    return {'output':'Waiting for an exact user approval.', 'exit_code':None, 'executed':False,
            'approval_required':True, 'ask_user':pending.public_payload(reason=reason)}


from contextlib import contextmanager
from contextvars import ContextVar
_APPROVAL_HISTORY = ContextVar('interactive_approval_history', default=None)


@contextmanager
def bind_approval_history(history_session, enabled):
    if not enabled or _APPROVAL_HISTORY.get() is not None:
        yield
        return
    token = _APPROVAL_HISTORY.set((history_session,))
    try:
        yield
    finally:
        _APPROVAL_HISTORY.reset(token)


def _history_has_scope(history_session, proof):
    for item in getattr(history_session, 'history', ()) or ():
        get = item.get if isinstance(item, dict) else lambda key, default=None:getattr(item, key, default)
        if get('role') != 'assistant':
            continue
        metadata = get('metadata') or {}
        if not isinstance(metadata, dict):
            continue
        for event in metadata.get('tool_events', ()):
            card = event.get('ask_user') if isinstance(event, dict) else None
            if isinstance(card, dict) and card.get('_scope_grant') == proof and card.get('approval_id') == proof['approval_id'] and card.get('resolved') == CHAT_SESSION_APPROVAL_DECISION:
                return True
    return False


def _pending_digest(pending):
    from inspect import signature
    return _canonical_digest(_binding_payload(**{name:getattr(pending, name) for name in signature(_binding_payload).parameters}))


def scoped_exact_approval(authority, tool_name, content, *, security_context, client_runtime_context=None):
    """Reuse only the user's exact reviewed operation, never a tool-wide bypass.

    Resumption creates a fresh one-use reservation with the original resource
    ceilings. A selected schema or arbitrary model output cannot create scope.
    """
    from dataclasses import replace
    from src.agent_runtime.authority import ExactOperation
    history = _APPROVAL_HISTORY.get()
    if history is None or authority is None or authority.inherited:
        return None
    try:
        operation = ExactOperation.normalize(tool_name, content)
        if authority.restricted(operation) or tool_approval_store.operation_was_denied(authority, operation):
            return None
        with tool_approval_store._lock:
            candidates = list(tool_approval_store._scoped.items())
        for grant_id, (pending, scope, opaque) in reversed(candidates):
            if pending.expires_at <= time.time():
                with tool_approval_store._lock:
                    tool_approval_store._scoped.pop(grant_id, None)
                continue
            if (pending.owner, pending.session_id, pending.workspace) != (authority.owner, authority.session_id, authority.workspace):
                continue
            if ExactOperation.normalize(pending.tool_name, pending.content) != operation:
                continue
            proof = {'approval_id':grant_id, 'scope':scope.value, 'token':opaque}
            if scope is ToolApprovalScope.TASK:
                if pending.request_authority is None or pending.request_authority.request_id != authority.request_id:
                    continue
            elif not _history_has_scope(history[0], proof):
                continue
            if security_context.external_untrusted_context_seen and not pending.external_untrusted_context_seen:
                continue  # A newly tainted run needs a newly informed decision.
            backend = pending.backend_operation
            if backend is None:
                continue
            backend.validate(client_runtime_context)
            rebound = {'request_authority':authority, 'backend_operation':replace(backend, request_id=authority.request_id)}
            for attr in ('resource_operation', 'owned_operation', 'browser_operation'):
                bound = getattr(pending, attr)
                if bound is not None:
                    bound.validate()
                    rebound[attr] = replace(bound, request_id=authority.request_id)
            process = pending.process_operation
            if process is not None:
                if process.launch is not None:
                    from src.agent_runtime.process_resources import BoundProcessOperation, digest
                    from uuid import uuid4
                    process.launch.scope.validate()
                    launch = replace(process.launch, request_id=authority.request_id, generation=uuid4().hex,
                        ceiling_digest=digest(json.dumps(authority.to_dict(), sort_keys=True)))
                    rebound['process_operation'] = BoundProcessOperation(operation, authority.request_id,
                        authority.owner, authority.session_id, launch)
                else:
                    process.validate()
                    rebound['process_operation'] = replace(process, request_id=authority.request_id)
            reviewed = replace(pending, **rebound)
            reviewed = replace(reviewed, digest=_pending_digest(reviewed))
            return ExactToolApproval(reviewed, scope=ToolApprovalScope.SINGLE_ACTION, allow_remaining_actions=False)
    except (ValueError, TypeError, OSError, RuntimeError, AttributeError):
        return None  # Changed resources never silently receive replacement grants.
    return None


def rejection_context(pending, authority):
    """A user decision is control state, not a fabricated executed tool call."""
    if authority is None or (pending.owner, pending.session_id, pending.workspace) != (authority.owner, authority.session_id, authority.workspace):
        raise ValueError('Approval rejection identity mismatch')
    if pending.request_authority is not None and pending.request_authority.request_id != authority.request_id:
        raise ValueError('Approval rejection belongs to another task')
    return {'role':'system', '_harness_control':True, 'content':json.dumps({
        'type':'operation_rejected', 'tool':pending.tool_name, 'arguments':pending.content,
        'failure_kind':'user_denied', 'executed':False,
        'instruction':'The user denied this exact operation. Continue the original task using permitted alternatives; do not repeat the denied operation.'})}
