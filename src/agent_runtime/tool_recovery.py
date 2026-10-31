"""Bounded execution recovery. Failures never supply authority or resource scope."""
from dataclasses import dataclass, field
from functools import wraps

from src.turn_contract import WORKSPACE_READ_TOOLS

# Only harness-authored structural fields qualify. Error text is untrusted.
SAFE_ALTERNATIVE_FAILURES = frozenset({
    "containment_unavailable", "tool_unavailable", "missing_optional_dependency",
})


def normalize_failure(result):
    result = dict(result)
    containment = result.get("containment") or {}
    if not isinstance(containment, dict):
        containment = {}
    if (containment.get("executed") is False
            and containment.get("contained") is False
            and containment.get("unenforced_required")):
        result.setdefault("failure_kind", "containment_unavailable")
        result.setdefault("executed", False)
    if result.get("failure_kind") in SAFE_ALTERNATIVE_FAILURES:
        result["recovery"] = "authorized_alternative_only"
    elif result.get("failure_kind"):
        result["recovery"] = "stop_denied_operation" if result.get("blocked") or result["failure_kind"].endswith("denied") else "correct_arguments_or_report"
    if result.get("error") or result.get("blocked") or result.get("approval_required") or result.get("exit_code") not in (None, 0):
        category = result.get("failure_reason") or result.get("failure_kind")
        if result.get("approval_required"):
            category = "approval_required"
        elif result.get("policy") == "exact_tool_approval":
            category = "approval_mismatch"
        elif result.get("policy") == "external_untrusted_context":
            category = "user_policy_denied"
        result["failure"] = {
            "category": category or "execution_failure",
            "execution": "not_executed" if result.get("executed") is False or result.get("blocked") else "unknown_or_partial",
            "recovery": result.get("recovery", "report_or_reconcile"),
        }
    return result


def normalize_tool_failures(func):
    """Normalize every dispatcher return, including pre-execution denials."""
    @wraps(func)
    async def wrapped(*args, **kwargs):
        description, result = await func(*args, **kwargs)
        block = kwargs.get('block') or (args[0] if args else None)
        if getattr(block, 'tool_type', '') == 'vault_audit':
            from src.agent_runtime.structured_audits import active_audit
            audit = active_audit()
            if audit is not None:
                audit.observe_result(result, getattr(block, 'content', ''))
                if audit.report is None and not audit.blocker:
                    audit.blocker = 'Native audit failed: no validated producer result.'
        return description, normalize_failure(result)
    return wrapped


@dataclass
class ToolRecovery:
    """Per-agent-invocation state; no side-effecting retries or new grants."""
    unavailable: set = field(default_factory=set)
    pending: tuple = ()
    replans: int = 0
    limit: int = 2

    def observe(self, tool, result, *, authority, contract):
        result = normalize_failure(result)
        if result.get("failure_kind") not in SAFE_ALTERNATIVE_FAILURES:
            if result.get("failure_kind") or result.get("blocked"):
                self.pending = ()
            if result.get("exit_code") == 0 and tool in WORKSPACE_READ_TOOLS:
                self.pending = ()
            return
        if result.get("executed") is not False:
            return  # Partial/uncertain execution requires separate reconciliation.
        self.unavailable.add(tool)
        # Require an explicit contract and authority; never guess permissions.
        if authority is None or contract is None or authority.block_all:
            return
        granted = {g.tool for g in authority.grants if g.actions is None and g.inputs is None}
        alternatives = WORKSPACE_READ_TOOLS & granted & contract.offered - authority.denied - self.unavailable
        self.pending = tuple(sorted(alternatives))

    def replan(self):
        if not self.pending or self.replans >= self.limit:
            return None
        self.replans += 1
        return (
            "An execution capability was unavailable before execution. Continue the original "
            "objective using already authorized native tools: " + ", ".join(self.pending) +
            ". Prefer vault_audit for bulk frontmatter checks. Preserve all resource and policy "
            "restrictions; do not retry unavailable tools or substitute for a denied operation. "
            "If these readers cannot satisfy the objective, explain the remaining blocker."
        )


# Recoverable tool failures must go back to the model. Authorization denials
# and blocked calls stay terminal. Identical retries are capped by the caller.
_RECOVERABLE_KINDS = frozenset({
    "tool_discovery_failed",
    "unknown_tool",
    "unavailable_tool",
    "invalid_arguments",
    "invalid_schema",
    "catalog_unavailable",
    "audit_scope_mismatch",
    "audit_receipt_invalid_id",
    "audit_receipt_expired",
    "audit_receipt_missing",
    "audit_receipt_scope_mismatch",
    "vault_unconfigured",
    "knowledge_retrieval_failed",
    "execution_failure",
})


def failure_is_recoverable(result) -> bool:
    """True when the model should see the error and may try a different call."""
    if not isinstance(result, dict):
        return False
    if result.get("blocked") or result.get("approval_required"):
        return False
    if result.get("recoverable") is False:
        return False
    if result.get("recoverable") is True:
        return True
    kind = str(result.get("failure_kind") or result.get("error_code") or "")
    if kind in _RECOVERABLE_KINDS:
        return True
    if kind.endswith("denied"):
        return False
    return False


def ensure_visible_failure(answer, failures) -> str:
    """Never present an empty completed reply after a failed tool."""
    text = str(answer or "").strip()
    if text:
        return text
    for item in reversed(list(failures or [])):
        if not isinstance(item, dict):
            continue
        failed = (
            item.get("exit_code") not in (None, 0)
            or item.get("failure_kind")
            or item.get("error")
            or item.get("error_code")
        )
        if not failed:
            continue
        tool = str(item.get("tool") or "tool").strip() or "tool"
        code = str(item.get("failure_kind") or item.get("error_code") or "execution_failure")
        detail = str(item.get("output") or item.get("error") or item.get("message") or code).strip()
        if detail:
            return f"{tool} failed ({code}): {detail[:500]}"
    return text


class RepeatedFailureGuard:
    """Bound identical failed attempts. The limit includes the attempts already made."""

    def __init__(self, limit: int = 2):
        self.limit = max(1, int(limit))
        self.counts: dict = {}

    def allow(self, signature: str) -> bool:
        count = self.counts.get(signature, 0) + 1
        self.counts[signature] = count
        return count <= self.limit
