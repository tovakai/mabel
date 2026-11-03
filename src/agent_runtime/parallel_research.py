"""Concurrent web + vault research coordinator.

One admitted ``research_parallel`` call fans out to the existing ``web_search``
dispatcher plus either fast indexed ``vault_knowledge_search`` or deep
``dispatch_dottie``. Children keep the same authority, policy, resource binding,
and accounting as direct calls. This module does not invent queries, bypass
admission, write the vault, or run a second synthesis LLM.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)

PARALLEL = "research_parallel"
_ACTIVE: ContextVar[bool] = ContextVar("parallel_research_active", default=False)

# Fair combined package budget. Pointers survive when bodies are trimmed.
_MAX_PACKAGE_BYTES = 24000
_PER_BRANCH_FLOOR = 6000
_WEB_TIMEOUT_S = 45.0
_VAULT_TIMEOUT_S = 120.0
_VAULT_FAST_TIMEOUT_S = 20.0
_FAST_TOP_K = 8
_FAST_QUERY_MAX = 500

# One-shot sealed sibling results for approval/resumption of the exact parent op.
_SEAL_TTL_S = 900.0
_SEAL_MAX = 64
_seal_lock = Lock()
_seals: dict[str, "_ParallelSeal"] = {}


@dataclass
class _ParallelSeal:
    owner: str
    session_id: str
    request_id: str
    operation_digest: str
    completed: dict[str, dict] = field(default_factory=dict)
    pending_branch: Optional[str] = None
    pending_tool: Optional[str] = None
    pending_input: Optional[str] = None
    expires_at: float = 0.0
    consumed: bool = False


def _operation_digest(args: Mapping[str, Any]) -> str:
    body = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _seal_key(owner: str, session_id: str, request_id: str, digest: str) -> str:
    return "|".join((owner.casefold(), session_id, request_id, digest))


def _cleanup_seals(now: float) -> None:
    for key, seal in tuple(_seals.items()):
        if seal.consumed or seal.expires_at <= now:
            _seals.pop(key, None)
    while len(_seals) > _SEAL_MAX:
        _seals.pop(next(iter(_seals)))


def remember_completed_branches(
    *,
    owner: str,
    session_id: str,
    request_id: str,
    args: Mapping[str, Any],
    completed: Mapping[str, dict],
    pending_branch: Optional[str] = None,
    pending_tool: Optional[str] = None,
    pending_arguments: Optional[dict] = None,
) -> None:
    """Seal successful sibling evidence for one exact parent operation resume."""
    if not owner or not session_id or not request_id or not completed:
        return
    digest = _operation_digest(args)
    pending_input = None
    if pending_tool and isinstance(pending_arguments, dict):
        from src.agent_runtime.authority import ExactOperation
        try:
            pending_input = ExactOperation.normalize(
                pending_tool, json.dumps(pending_arguments, ensure_ascii=False)
            ).input
        except (TypeError, ValueError):
            pending_input = None
    now = time.monotonic()
    with _seal_lock:
        _cleanup_seals(now)
        _seals[_seal_key(owner, session_id, request_id, digest)] = _ParallelSeal(
            owner=owner.casefold(),
            session_id=session_id,
            request_id=request_id,
            operation_digest=digest,
            completed={name: dict(section) for name, section in completed.items()},
            pending_branch=pending_branch,
            pending_tool=pending_tool,
            pending_input=pending_input,
            expires_at=now + _SEAL_TTL_S,
        )


def consume_sealed_branches(
    *,
    owner: str,
    session_id: str,
    request_id: str,
    args: Mapping[str, Any],
) -> Optional[_ParallelSeal]:
    """Return and keep a matching unconsumed seal for the exact parent operation."""
    if not owner or not session_id or not request_id:
        return None
    digest = _operation_digest(args)
    key = _seal_key(owner, session_id, request_id, digest)
    now = time.monotonic()
    with _seal_lock:
        _cleanup_seals(now)
        seal = _seals.get(key)
        if seal is None or seal.consumed or seal.expires_at <= now:
            return None
        if (
            seal.owner != owner.casefold()
            or seal.session_id != session_id
            or seal.request_id != request_id
            or seal.operation_digest != digest
        ):
            return None
        return seal


def mark_seal_consumed(seal: Optional[_ParallelSeal]) -> None:
    if seal is None:
        return
    with _seal_lock:
        seal.consumed = True
        key = _seal_key(seal.owner, seal.session_id, seal.request_id, seal.operation_digest)
        _seals.pop(key, None)


def clear_seals_for_tests() -> None:
    with _seal_lock:
        _seals.clear()


def _parse_args(content: str) -> dict:
    raw = (content or "").strip() or "{}"
    try:
        args = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"research_parallel requires a JSON object: {error}") from error
    if not isinstance(args, dict):
        raise ValueError("research_parallel arguments must be an object")
    if set(args) - {"mode", "web_query", "vault_objective", "vault_constraints", "vault_strategy"}:
        raise ValueError("Unknown research_parallel argument")
    mode = str(args.get("mode") or "both").strip().casefold()
    if mode not in {"web", "vault", "both"}:
        raise ValueError("mode must be web, vault, or both")
    strategy_raw = args.get("vault_strategy", "fast")
    if strategy_raw in (None, ""):
        strategy_raw = "fast"
    vault_strategy = str(strategy_raw).strip().casefold()
    if vault_strategy not in {"fast", "deep"}:
        raise ValueError("vault_strategy must be fast or deep")
    if mode == "web" and "vault_strategy" in args and args.get("vault_strategy") not in (None, ""):
        # Explicit strategy with no vault branch is rejected to avoid silent no-ops.
        raise ValueError("vault_strategy is only valid for vault/both modes")
    web_query = args.get("web_query")
    vault_objective = args.get("vault_objective")
    constraints = args.get("vault_constraints")
    if constraints is None:
        constraints = {}
    if not isinstance(constraints, dict):
        raise ValueError("vault_constraints must be an object")
    if set(constraints) - {"path_prefix", "required_sources", "max_hops"}:
        raise ValueError("Unknown vault_constraints field")
    if mode in {"web", "both"}:
        if not isinstance(web_query, str) or not 1 <= len(web_query.strip()) <= 2000:
            raise ValueError("web_query is required for web/both modes")
    elif web_query not in (None, ""):
        raise ValueError("web_query is only valid for web/both modes")
    if mode in {"vault", "both"}:
        if not isinstance(vault_objective, str) or not 1 <= len(vault_objective.strip()) <= 2000:
            raise ValueError("vault_objective is required for vault/both modes")
    elif vault_objective not in (None, ""):
        raise ValueError("vault_objective is only valid for vault/both modes")
    if "required_sources" in constraints and type(constraints.get("required_sources")) is not bool:
        raise ValueError("required_sources must be boolean")
    hops = constraints.get("max_hops", 1)
    if hops is not None and (type(hops) is not int or not 0 <= hops <= 3):
        raise ValueError("max_hops must be an integer from 0 to 3")
    return {
        "mode": mode,
        "vault_strategy": vault_strategy if mode in {"vault", "both"} else "fast",
        "web_query": (web_query or "").strip() if isinstance(web_query, str) else "",
        "vault_objective": (vault_objective or "").strip() if isinstance(vault_objective, str) else "",
        "vault_constraints": {
            key: constraints[key]
            for key in ("path_prefix", "required_sources", "max_hops")
            if key in constraints
        },
    }


def _vault_child_tool(strategy: str) -> str:
    return "vault_knowledge_search" if strategy == "fast" else "dispatch_dottie"


def _build_vault_child_args(args: Mapping[str, Any]) -> dict:
    """Build the exact child tool arguments for the selected vault strategy."""
    strategy = args["vault_strategy"]
    constraints = args.get("vault_constraints") or {}
    if strategy == "deep":
        vault_args = {"research_objective": args["vault_objective"]}
        if constraints:
            vault_args["constraints"] = dict(constraints)
            vault_args["constraints"].setdefault("max_hops", 1)
            vault_args["constraints"].setdefault("required_sources", True)
        return vault_args
    # Fast: indexed lexical search only — no Dottie LLM, embeddings, or reranker.
    query = args["vault_objective"].strip()[:_FAST_QUERY_MAX]
    vault_args = {
        "query": query,
        "mode": "lexical",
        "top_k": _FAST_TOP_K,
        "use_reranking": False,
    }
    prefix = constraints.get("path_prefix")
    if isinstance(prefix, str) and prefix.strip():
        vault_args["path_prefix"] = prefix.strip()
    return vault_args


def _vault_source_count(section: Mapping[str, Any] | None) -> int:
    if not isinstance(section, Mapping):
        return 0
    if section.get("strategy") == "fast" or "matches" in section:
        return len(section.get("matches") or [])
    return len(section.get("findings") or [])


def research_shares_foreground_endpoint(owner: str = "") -> tuple[bool, dict]:
    """Return whether the research role would contend with Mabel's primary chat model."""
    from src import model_roles

    detail = {"research_url": None, "default_url": None, "shared": False, "research_available": False}
    try:
        research = model_roles.resolve(
            model_roles.primary_assignment("research", owner), owner, required="tools"
        )
        detail["research_url"] = research.url
        detail["research_available"] = True
        detail["research_model"] = research.assignment.model
    except (model_roles.RoleUnavailable, ValueError, TypeError):
        return False, detail
    try:
        default = model_roles.resolve(model_roles.primary_assignment("default", owner), owner)
        detail["default_url"] = default.url
        detail["default_model"] = default.assignment.model
    except (model_roles.RoleUnavailable, ValueError, TypeError):
        return False, detail
    shared = bool(detail["research_url"] and detail["research_url"] == detail["default_url"])
    detail["shared"] = shared
    return shared, detail


def _authority_permits(
    tool: str,
    arguments: dict,
    authority,
    *,
    tool_policy=None,
    disabled_tools=None,
) -> bool:
    """Mirror execute_tool_block admission: restrict then ExactOperation.permits."""
    if authority is None:
        return False
    from src.agent_runtime.authority import ExactOperation

    try:
        operation = ExactOperation.normalize(tool, json.dumps(arguments, ensure_ascii=False))
    except (TypeError, ValueError):
        return False
    if hasattr(authority, "restrict"):
        authority = authority.restrict(tool_policy, disabled_tools or ())
    if authority.restricted(operation):
        return False
    return bool(authority.permits(operation))


@contextmanager
def _authorized_child_turn_scope(child_tools: list[str]) -> Iterator[frozenset[str]]:
    """Derive nested turn selection from the request catalog — never from model args.

    Compact/conversational turns often discover only ``research_parallel``.
    ``TurnContract.permits`` then allows the parent via ``catalog.selected`` while
    children remain outside ``offered | selected``, so nested ``execute_tool_block``
    fails with turn_contract_denied even when RequestAuthority already grants those
    children. Promote only names already present in the policy-filtered catalog
    inventory (executable for this request). Temporary; does not mint grants.
    """
    from src.agent_runtime.tool_catalog import active_catalog

    catalog = active_catalog()
    added: list[str] = []
    if catalog is not None:
        for name in child_tools:
            if not isinstance(name, str) or not name or name in catalog.selected:
                continue
            if name not in catalog.inventory:
                continue
            catalog.selected.add(name)
            added.append(name)
    try:
        yield frozenset(added)
    finally:
        if catalog is not None and added:
            catalog.selected.difference_update(added)


def _child_failure(result: Mapping[str, Any] | None, *, default: str = "backend_failure") -> str:
    if not isinstance(result, Mapping):
        return default
    if result.get("approval_required"):
        return "approval_required"
    if result.get("blocked") or result.get("failure_kind") in {
        "request_authority_denied",
        "user_denied",
        "turn_contract_denied",
        "resource_identity_denied",
    }:
        return "authorization_denied"
    if result.get("failure_kind") == "research_budget_exceeded":
        return "budget_exceeded"
    kind = str(result.get("failure_kind") or "")
    if "timeout" in kind.casefold() or "cancel" in kind.casefold():
        return kind.casefold()
    if result.get("exit_code") not in (None, 0) or result.get("error"):
        return default
    return ""


def _parse_child_output(result: Mapping[str, Any]) -> Any:
    output = result.get("output")
    if isinstance(output, (dict, list)):
        return output
    if isinstance(output, str) and output.strip():
        try:
            return json.loads(output)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {"raw": output[:4000]}
    return {}


def _web_branch_payload(result: Mapping[str, Any], *, status: str, error: str = "") -> dict:
    parsed = _parse_child_output(result) if status in {"complete", "empty", "partial"} else {}
    results = []
    if isinstance(parsed, dict):
        for item in parsed.get("results") or parsed.get("entries") or ():
            if not isinstance(item, dict):
                continue
            results.append({
                "title": str(item.get("title") or "")[:300],
                "url": str(item.get("url") or item.get("link") or "")[:2000],
                "snippet": str(item.get("snippet") or item.get("content") or item.get("body") or "")[:1200],
                "engine": str(item.get("engine") or item.get("source") or "")[:120],
            })
        if not results and isinstance(parsed.get("raw"), str):
            results.append({"title": "", "url": "", "snippet": parsed["raw"][:1200], "engine": ""})
    payload = {"status": status, "results": results}
    if error:
        payload["error"] = error[:500]
        payload["failure_kind"] = _child_failure(result) or "backend_failure"
    if result.get("approval_required"):
        payload["approval_required"] = True
        if isinstance(result.get("ask_user"), dict):
            payload["ask_user"] = result["ask_user"]
    return payload


def _deep_vault_branch_payload(result: Mapping[str, Any], *, status: str, error: str = "") -> dict:
    parsed = _parse_child_output(result) if status in {"complete", "empty", "partial", "degraded"} else {}
    findings = []
    if isinstance(parsed, dict):
        for item in parsed.get("findings") or ():
            if not isinstance(item, dict):
                continue
            evidence = []
            for ref in item.get("evidence") or ():
                if not isinstance(ref, dict):
                    continue
                evidence.append({
                    "note_id": str(ref.get("note_id") or "")[:200],
                    "rel_path": str(ref.get("rel_path") or "")[:500],
                    "start_line": ref.get("start_line"),
                    "end_line": ref.get("end_line"),
                    "quote": str(ref.get("quote") or "")[:800],
                    "content_hash": str(ref.get("content_hash") or "")[:128],
                })
            findings.append({
                "claim": str(item.get("claim") or "")[:1000],
                "confidence": str(item.get("confidence") or "")[:64],
                "evidence": evidence,
            })
        local = parsed.get("local_retrieval")
        if not findings and isinstance(local, dict):
            for match in local.get("matches") or ():
                if not isinstance(match, dict):
                    continue
                findings.append({
                    "claim": str(match.get("snippet") or match.get("text") or match.get("excerpt") or "")[:1000],
                    "confidence": "local_retrieval",
                    "evidence": [{
                        "note_id": str(match.get("note_id") or "")[:200],
                        "rel_path": str(match.get("rel_path") or "")[:500],
                        "start_line": match.get("start_line"),
                        "end_line": match.get("end_line"),
                        "quote": str(match.get("snippet") or match.get("text") or match.get("excerpt") or "")[:800],
                        "content_hash": str(match.get("content_hash") or "")[:128],
                    }],
                })
    payload = {
        "status": status,
        "strategy": "deep",
        "kind": "verified_findings",
        "findings": findings,
        "unresolved_questions": (
            [str(v)[:500] for v in (parsed.get("unresolved_questions") or [])[:12]]
            if isinstance(parsed, dict) else []
        ),
    }
    if isinstance(parsed, dict) and parsed.get("worker_execution"):
        worker = parsed["worker_execution"]
        if isinstance(worker, dict):
            payload["worker_execution"] = {
                key: worker.get(key)
                for key in (
                    "status", "phase", "stream_events", "tool_reads",
                    "evidence_chunks", "model_inference_confirmed", "failure_type",
                )
                if key in worker
            }
    if error:
        payload["error"] = error[:500]
        payload["failure_kind"] = _child_failure(result) or "backend_failure"
    if result.get("approval_required"):
        payload["approval_required"] = True
        if isinstance(result.get("ask_user"), dict):
            payload["ask_user"] = result["ask_user"]
    return payload


def _fast_vault_branch_payload(result: Mapping[str, Any], *, status: str, error: str = "") -> dict:
    """Index candidates only — never presented as Dottie-verified claims."""
    parsed = _parse_child_output(result) if status in {"complete", "empty", "partial", "failed"} else {}
    matches = []
    if isinstance(parsed, dict):
        for item in parsed.get("matches") or ():
            if not isinstance(item, dict):
                continue
            score = item.get("score", item.get("fusion_score", item.get("rank")))
            try:
                score_val = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_val = None
            matches.append({
                "note_id": str(item.get("note_id") or "")[:200],
                "rel_path": str(item.get("rel_path") or "")[:500],
                "title": str(item.get("title") or "")[:300],
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "excerpt": str(item.get("excerpt") or item.get("snippet") or item.get("text") or "")[:800],
                "content_hash": str(item.get("content_hash") or "")[:128],
                "score": score_val,
            })
    payload = {
        "status": status,
        "strategy": "fast",
        "kind": "index_candidates",
        "matches": matches,
        # Keep findings empty so callers cannot mistake candidates for verified claims.
        "findings": [],
        "retrieval_mode": (
            str(parsed.get("mode") or "lexical")[:32] if isinstance(parsed, dict) else "lexical"
        ),
    }
    if error:
        payload["error"] = error[:500]
        payload["failure_kind"] = _classify_fast_failure_kind(result, error)
    if result.get("approval_required"):
        payload["approval_required"] = True
        if isinstance(result.get("ask_user"), dict):
            payload["ask_user"] = result["ask_user"]
    return payload


def _classify_fast_failure_kind(result: Mapping[str, Any], error: str = "") -> str:
    failure = _child_failure(result) or "backend_failure"
    blob = " ".join((
        failure,
        str(result.get("failure_kind") or ""),
        str(result.get("error") or ""),
        error,
    )).casefold()
    if "index" in blob or "not indexed" in blob or "last_scan" in blob:
        return "index_unavailable"
    if failure:
        return failure
    return "backend_failure"


def _vault_branch_payload(
    result: Mapping[str, Any],
    *,
    status: str,
    error: str = "",
    strategy: str = "deep",
) -> dict:
    if strategy == "fast":
        return _fast_vault_branch_payload(result, status=status, error=error)
    return _deep_vault_branch_payload(result, status=status, error=error)


def _classify_web_result(result: Mapping[str, Any]) -> tuple[str, str]:
    failure = _child_failure(result)
    if failure == "approval_required":
        return "approval_required", "Web search needs approval before it can run."
    if failure == "authorization_denied":
        return "denied", str(result.get("error") or "Web search was not authorized.")
    if failure == "cancelled":
        return "cancelled", "Web search was cancelled."
    if failure == "timeout":
        return "timeout", "Web search timed out."
    if failure:
        return "failed", str(result.get("error") or "Web search failed.")
    payload = _web_branch_payload(result, status="complete")
    if not payload["results"]:
        return "empty", ""
    return "complete", ""


def _classify_vault_result(result: Mapping[str, Any], *, strategy: str = "deep") -> tuple[str, str]:
    failure = _child_failure(result)
    label = "Vault index search" if strategy == "fast" else "Vault research"
    if failure == "approval_required":
        return "approval_required", f"{label} needs approval before it can run."
    if failure == "authorization_denied":
        return "denied", str(result.get("error") or f"{label} was not authorized.")
    if failure == "cancelled":
        return "cancelled", f"{label} was cancelled."
    if failure == "timeout":
        return "timeout", f"{label} timed out."
    if failure:
        kind = _classify_fast_failure_kind(result) if strategy == "fast" else failure
        message = str(result.get("error") or f"{label} failed.")
        if strategy == "fast" and kind == "index_unavailable":
            return "failed", message[:500]
        return "failed", message[:500]
    parsed = _parse_child_output(result)
    if not isinstance(parsed, dict):
        return "failed", f"{label} returned an invalid package."
    if strategy == "fast":
        matches = parsed.get("matches") if isinstance(parsed.get("matches"), list) else []
        if not matches:
            return "empty", ""
        return "complete", ""
    worker = parsed.get("worker_execution") if isinstance(parsed.get("worker_execution"), dict) else {}
    worker_status = str(worker.get("status") or "")
    if parsed.get("status") == "degraded" or worker_status in {"not_started", "failed"}:
        reason = str(
            parsed.get("reason")
            or (parsed.get("unresolved_questions") or ["Vault worker unavailable"])[0]
            or "Vault worker unavailable; local retrieval only."
        )
        # Unavailable/failed Dottie must not look like a successful empty search.
        return "degraded", reason[:500]
    payload = _vault_branch_payload(result, status="complete", strategy="deep")
    if isinstance(parsed, dict) and parsed.get("status") == "partial":
        if payload["findings"]:
            return "partial", ""
        questions = parsed.get("unresolved_questions") or []
        if questions or worker_status:
            return "partial", str(questions[0] if questions else "Vault research was partial.")[:500]
        return "partial", "Vault research was partial."
    if not payload["findings"]:
        # Genuine empty verified findings after a healthy worker are not failures.
        return "empty", ""
    return "complete", ""


def _truncate_branch(section: dict, *, keep_keys: tuple[str, ...], body_key: str, limit: int) -> dict:
    """Shrink bodies while preserving source pointers."""
    encoded = json.dumps(section, ensure_ascii=False).encode()
    if len(encoded) <= limit:
        return section
    trimmed = dict(section)
    items = list(trimmed.get(body_key) or [])
    while items and len(json.dumps(trimmed, ensure_ascii=False).encode()) > limit:
        last = dict(items[-1])
        if body_key == "results":
            snippet = str(last.get("snippet") or "")
            if len(snippet) > 160:
                last["snippet"] = snippet[: max(80, len(snippet) // 2)] + "…"
                items[-1] = last
            else:
                items.pop()
        elif body_key == "matches":
            excerpt = str(last.get("excerpt") or "")
            if len(excerpt) > 160:
                last["excerpt"] = excerpt[: max(80, len(excerpt) // 2)] + "…"
                items[-1] = last
            else:
                # Preserve note_id/rel_path identity even when dropping bodies.
                items.pop()
        else:
            claim = str(last.get("claim") or "")
            evidence = list(last.get("evidence") or [])
            if evidence and len(str(evidence[-1].get("quote") or "")) > 120:
                ref = dict(evidence[-1])
                quote = str(ref.get("quote") or "")
                ref["quote"] = quote[: max(60, len(quote) // 2)] + "…"
                evidence[-1] = ref
                last["evidence"] = evidence
                items[-1] = last
            elif len(claim) > 160:
                last["claim"] = claim[: max(80, len(claim) // 2)] + "…"
                items[-1] = last
            else:
                items.pop()
        trimmed[body_key] = items
        trimmed["truncated"] = True
    # Always keep status / failure metadata even if the body list is empty.
    for key in keep_keys:
        if key in section:
            trimmed[key] = section[key]
    return trimmed


def _apply_output_budget(package: dict) -> dict:
    encoded = json.dumps(package, ensure_ascii=False).encode()
    if len(encoded) <= _MAX_PACKAGE_BYTES:
        package["metrics"]["output_bytes"] = len(encoded)
        return package
    web_budget = vault_budget = max(_PER_BRANCH_FLOOR, _MAX_PACKAGE_BYTES // 2)
    if isinstance(package.get("web"), dict):
        package["web"] = _truncate_branch(
            package["web"], keep_keys=("status", "error", "failure_kind", "approval_required", "ask_user"),
            body_key="results", limit=web_budget,
        )
    if isinstance(package.get("vault"), dict):
        vault = package["vault"]
        body_key = "matches" if vault.get("strategy") == "fast" or "matches" in vault else "findings"
        package["vault"] = _truncate_branch(
            vault,
            keep_keys=(
                "status", "error", "failure_kind", "approval_required", "ask_user",
                "unresolved_questions", "worker_execution", "strategy", "kind", "retrieval_mode",
            ),
            body_key=body_key, limit=vault_budget,
        )
    package["metrics"]["output_bytes"] = len(json.dumps(package, ensure_ascii=False).encode())
    package["metrics"]["truncated"] = True
    if package.get("status") == "complete":
        package["status"] = "partial"
    return package


async def _emit(ctx: Mapping[str, Any], event: dict) -> None:
    callback = ctx.get("progress_cb") if isinstance(ctx, Mapping) else None
    if callback is None:
        return
    payload = {"parallel_research_event": True, **event}
    try:
        await callback(payload)
    except Exception:
        logger.debug("parallel research diagnostic emit failed", exc_info=True)


async def _run_child(
    tool: str,
    arguments: dict,
    ctx: Mapping[str, Any],
    *,
    timeout_s: float,
) -> dict:
    from src.tool_execution import execute_tool_block
    from src.tool_types import ToolBlock
    from src.tool_capabilities import ToolRunSecurityContext

    authority = ctx.get("request_authority")
    security = ctx.get("security_context")
    if not isinstance(security, ToolRunSecurityContext):
        security = ToolRunSecurityContext()
    block = ToolBlock(tool, json.dumps(arguments, ensure_ascii=False))
    try:
        async with asyncio.timeout(timeout_s):
            _, result = await execute_tool_block(
                block,
                owner=ctx.get("owner"),
                session_id=ctx.get("session_id"),
                workspace=getattr(authority, "workspace", None) or ctx.get("workspace"),
                request_authority=authority,
                security_context=security,
                disabled_tools=ctx.get("disabled_tools"),
                tool_policy=ctx.get("tool_policy"),
                client_runtime_context=ctx.get("client_runtime_context"),
            )
    except TimeoutError:
        return {
            "error": f"{tool} timed out after {int(timeout_s)}s",
            "exit_code": 1,
            "executed": False,
            "failure_kind": "timeout",
        }
    except asyncio.CancelledError:
        return {
            "error": f"{tool} was cancelled",
            "exit_code": 1,
            "executed": False,
            "failure_kind": "cancelled",
        }
    return result if isinstance(result, dict) else {
        "error": f"{tool} returned an invalid result",
        "exit_code": 1,
        "executed": False,
        "failure_kind": "backend_failure",
    }


async def execute(content: str, ctx: Mapping[str, Any]) -> dict:
    if _ACTIVE.get():
        return {
            "error": "research_parallel cannot nest inside another parallel research call",
            "exit_code": 1,
            "executed": False,
            "failure_kind": "recursion_denied",
        }
    try:
        args = _parse_args(content)
    except ValueError as error:
        return {"error": str(error), "exit_code": 1, "executed": False, "failure_kind": "invalid_arguments"}

    authority = ctx.get("request_authority")
    mode = args["mode"]
    vault_strategy = args["vault_strategy"]
    vault_tool = _vault_child_tool(vault_strategy)
    owner = str(ctx.get("owner") or getattr(authority, "owner", "") or "")
    session_id = str(ctx.get("session_id") or getattr(authority, "session_id", "") or "")
    request_id = str(getattr(authority, "request_id", "") or "")
    shared, route_detail = research_shares_foreground_endpoint(owner)
    # Fast vault never touches the research LLM, so endpoint sharing cannot
    # force sequential execution for ordinary conversational retrieval.
    plan_concurrent = mode == "both" and (vault_strategy == "fast" or not shared)

    started = time.monotonic()
    token = _ACTIVE.set(True)
    events: list[dict] = []
    unresolved: list[str] = []
    web_section = None
    vault_section = None
    approval_bubble = None
    branch_windows: dict[str, tuple[float, float]] = {}
    seal = consume_sealed_branches(
        owner=owner, session_id=session_id, request_id=request_id, args=args,
    )
    reused_branches: set[str] = set()

    async def note(event: dict) -> None:
        events.append(event)
        await _emit(ctx, event)

    try:
        await note({
            "operation": "parallel_research_started",
            "mode": mode,
            "vault_strategy": vault_strategy if mode in {"vault", "both"} else None,
            "planned_concurrent": plan_concurrent,
            "shared_endpoint": shared,
            "resume_seal": bool(seal and seal.completed),
        })

        web_args = {"query": args["web_query"]} if mode in {"web", "both"} else None
        vault_args = _build_vault_child_args(args) if mode in {"vault", "both"} else None

        # Resume: reuse sealed sibling evidence; never re-run completed branches.
        if seal is not None:
            if "web" in seal.completed and web_args is not None:
                web_section = dict(seal.completed["web"])
                reused_branches.add("web")
                await note({
                    "operation": "parallel_branch_completed",
                    "branch": "web",
                    "status": web_section.get("status"),
                    "reused": True,
                    "source_count": len(web_section.get("results") or []),
                })
            if "vault" in seal.completed and vault_args is not None:
                vault_section = dict(seal.completed["vault"])
                reused_branches.add("vault")
                await note({
                    "operation": "parallel_branch_completed",
                    "branch": "vault",
                    "status": vault_section.get("status"),
                    "reused": True,
                    "vault_strategy": vault_section.get("strategy") or vault_strategy,
                    "source_count": _vault_source_count(vault_section),
                })

        # Authorization is checked before restricted work starts. A compound
        # call must never turn two unauthorized operations into one authorized one.
        policy = ctx.get("tool_policy")
        disabled = ctx.get("disabled_tools")
        web_allowed = True
        vault_allowed = True
        if web_args is not None and "web" not in reused_branches:
            web_allowed = _authority_permits(
                "web_search", web_args, authority, tool_policy=policy, disabled_tools=disabled,
            )
            if not web_allowed:
                web_section = {
                    "status": "denied",
                    "results": [],
                    "error": "web_search is outside request authority for this turn",
                    "failure_kind": "authorization_denied",
                }
                unresolved.append("Web branch unauthorized")
                await note({
                    "operation": "parallel_branch_failed",
                    "branch": "web",
                    "status": "denied",
                    "source_count": 0,
                })
        if vault_args is not None and "vault" not in reused_branches:
            vault_allowed = _authority_permits(
                vault_tool, vault_args, authority, tool_policy=policy, disabled_tools=disabled,
            )
            if not vault_allowed:
                vault_section = {
                    "status": "denied",
                    "strategy": vault_strategy,
                    "kind": "index_candidates" if vault_strategy == "fast" else "verified_findings",
                    "findings": [],
                    "error": f"{vault_tool} is outside request authority for this turn",
                    "failure_kind": "authorization_denied",
                }
                if vault_strategy == "fast":
                    vault_section["matches"] = []
                unresolved.append("Vault branch unauthorized")
                await note({
                    "operation": "parallel_branch_failed",
                    "branch": "vault",
                    "status": "denied",
                    "vault_strategy": vault_strategy,
                    "failure_kind": "authorization_denied",
                    "source_count": 0,
                })

        # Exact-scope resume: if a seal names one pending child, only that child
        # may execute. Never widen to the sibling that already completed.
        run_web = web_args is not None and web_allowed and "web" not in reused_branches
        run_vault = vault_args is not None and vault_allowed and "vault" not in reused_branches
        if seal is not None and seal.pending_branch in {"web", "vault"}:
            if seal.pending_branch == "web":
                run_vault = False
            else:
                run_web = False
            if seal.pending_tool and seal.pending_input:
                from src.agent_runtime.authority import ExactOperation
                expected = seal.pending_input
                candidate = web_args if seal.pending_branch == "web" else vault_args
                try:
                    actual = ExactOperation.normalize(
                        seal.pending_tool, json.dumps(candidate or {}, ensure_ascii=False)
                    ).input
                except (TypeError, ValueError):
                    actual = None
                if actual != expected:
                    mark_seal_consumed(seal)
                    return {
                        "error": "Resumed parallel research no longer matches the sealed child operation.",
                        "exit_code": 1,
                        "blocked": True,
                        "executed": False,
                        "failure_kind": "exact_operation_mismatch",
                    }

        # Child may be authorized but outside discovery-selected turn surface.
        # Inherit only inventoried, authority-permitted children for nested dispatch.
        # If a child is not in this request's executable catalog at all, omit it —
        # the parent grant must not invent a missing capability.
        from src.agent_runtime.tool_catalog import active_catalog
        from src.turn_contract import active_turn_contract

        catalog = active_catalog()
        contract = active_turn_contract()

        def _child_turn_available(tool: str) -> bool:
            if contract is not None and contract.permits(tool):
                return True
            if catalog is not None and tool in catalog.inventory:
                return True
            if contract is not None and tool in contract.executable:
                return True
            # No turn contract / catalog (non-interactive tests): do not block.
            return catalog is None and contract is None

        if run_web and not _child_turn_available("web_search"):
            run_web = False
            web_section = {
                "status": "denied",
                "results": [],
                "error": "web_search is outside requested turn capabilities",
                "failure_kind": "turn_contract_denied",
            }
            unresolved.append("Web branch unavailable on this turn")
            await note({
                "operation": "parallel_branch_failed",
                "branch": "web",
                "status": "denied",
                "failure_kind": "turn_contract_denied",
                "source_count": 0,
            })
        if run_vault and not _child_turn_available(vault_tool):
            run_vault = False
            vault_section = {
                "status": "denied",
                "strategy": vault_strategy,
                "kind": "index_candidates" if vault_strategy == "fast" else "verified_findings",
                "findings": [],
                "error": f"{vault_tool} is outside requested turn capabilities",
                "failure_kind": "turn_contract_denied",
            }
            if vault_strategy == "fast":
                vault_section["matches"] = []
            unresolved.append("Vault branch unavailable on this turn")
            await note({
                "operation": "parallel_branch_failed",
                "branch": "vault",
                "status": "denied",
                "vault_strategy": vault_strategy,
                "failure_kind": "turn_contract_denied",
                "source_count": 0,
            })

        inherit_tools: list[str] = []
        if run_web:
            inherit_tools.append("web_search")
        if run_vault:
            inherit_tools.append(vault_tool)

        async def web_job() -> tuple[str, dict, float, float, float]:
            t0 = time.monotonic()
            result = await _run_child("web_search", web_args, ctx, timeout_s=_WEB_TIMEOUT_S)
            t1 = time.monotonic()
            return "web", result, (t1 - t0) * 1000.0, t0, t1

        async def vault_job() -> tuple[str, dict, float, float, float]:
            t0 = time.monotonic()
            timeout = _VAULT_FAST_TIMEOUT_S if vault_strategy == "fast" else _VAULT_TIMEOUT_S
            result = await _run_child(vault_tool, vault_args, ctx, timeout_s=timeout)
            t1 = time.monotonic()
            return "vault", result, (t1 - t0) * 1000.0, t0, t1

        jobs = []
        if run_web:
            jobs.append(web_job())
        if run_vault:
            jobs.append(vault_job())

        timings = {"web_ms": 0.0, "vault_ms": 0.0, "vault_index_ms": 0.0}
        launched_concurrent = plan_concurrent and len(jobs) == 2
        if jobs:
            with _authorized_child_turn_scope(inherit_tools):
                if launched_concurrent:
                    tasks = [asyncio.create_task(job) for job in jobs]
                    try:
                        gathered = await asyncio.gather(*tasks, return_exceptions=True)
                    except asyncio.CancelledError:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
                else:
                    # Conservative sequential fallback when deep research shares the
                    # foreground endpoint, or when only one branch is enabled.
                    gathered = []
                    for job in jobs:
                        try:
                            gathered.append(await job)
                        except Exception as error:  # noqa: BLE001 - branch isolation
                            gathered.append(error)

                for item in gathered:
                    if isinstance(item, Exception):
                        if isinstance(item, asyncio.CancelledError):
                            raise item
                        unresolved.append(f"Branch crashed: {type(item).__name__}")
                        continue
                    branch, result, elapsed_ms, t0, t1 = item
                    timings[f"{branch}_ms"] = round(elapsed_ms, 1)
                    branch_windows[branch] = (t0, t1)
                    if branch == "web":
                        status, error = _classify_web_result(result)
                        web_section = _web_branch_payload(result, status=status, error=error)
                        if status == "approval_required":
                            approval_bubble = result
                        if status in {"failed", "denied", "timeout", "cancelled", "approval_required"}:
                            unresolved.append(error or f"Web branch {status}")
                            await note({
                                "operation": "parallel_branch_failed",
                                "branch": "web",
                                "status": status,
                                "duration_ms": timings["web_ms"],
                                "source_count": len(web_section.get("results") or []),
                            })
                        else:
                            await note({
                                "operation": "parallel_branch_completed",
                                "branch": "web",
                                "status": status,
                                "duration_ms": timings["web_ms"],
                                "source_count": len(web_section.get("results") or []),
                            })
                    else:
                        status, error = _classify_vault_result(result, strategy=vault_strategy)
                        vault_section = _vault_branch_payload(
                            result, status=status, error=error, strategy=vault_strategy,
                        )
                        if vault_strategy == "fast":
                            # Fast path is index lookup; total branch time ≈ index time.
                            timings["vault_index_ms"] = timings["vault_ms"]
                        match_count = _vault_source_count(vault_section)
                        if status == "approval_required":
                            approval_bubble = result
                        diag = {
                            "branch": "vault",
                            "status": status,
                            "duration_ms": timings["vault_ms"],
                            "vault_strategy": vault_strategy,
                            "vault_index_ms": timings.get("vault_index_ms") or None,
                            "match_count": match_count,
                            "source_count": match_count,
                            "failure_kind": vault_section.get("failure_kind"),
                        }
                        if status in {"failed", "denied", "timeout", "cancelled", "approval_required"}:
                            unresolved.append(error or f"Vault branch {status}")
                            await note({"operation": "parallel_branch_failed", **diag})
                        else:
                            await note({"operation": "parallel_branch_completed", **diag})

        if mode in {"web", "both"} and web_section is None:
            web_section = {"status": "failed", "results": [], "error": "Web branch did not run", "failure_kind": "backend_failure"}
        if mode in {"vault", "both"} and vault_section is None:
            vault_section = {
                "status": "failed",
                "strategy": vault_strategy,
                "kind": "index_candidates" if vault_strategy == "fast" else "verified_findings",
                "findings": [],
                "error": "Vault branch did not run",
                "failure_kind": "backend_failure",
            }
            if vault_strategy == "fast":
                vault_section["matches"] = []

        branch_statuses = [
            section.get("status")
            for section in (web_section, vault_section)
            if isinstance(section, dict)
        ]
        successish = {"complete", "empty", "partial", "degraded"}
        failedish = {"failed", "denied", "timeout", "cancelled", "approval_required"}
        if approval_bubble is not None:
            overall = "approval_required"
        else:
            successes = [status for status in branch_statuses if status in successish]
            failures = [status for status in branch_statuses if status in failedish]
            if successes and failures:
                overall = "partial"
            elif failures and not successes:
                overall = "failed"
            elif any(status in {"partial", "degraded"} for status in branch_statuses):
                overall = "partial"
            elif branch_statuses:
                overall = "complete"
            else:
                overall = "failed"

        # Claim concurrency only when both children actually overlapped in time.
        actually_concurrent = False
        if "web" in branch_windows and "vault" in branch_windows:
            web_t0, web_t1 = branch_windows["web"]
            vault_t0, vault_t1 = branch_windows["vault"]
            actually_concurrent = web_t0 < vault_t1 and vault_t0 < web_t1

        # Seal completed siblings when waiting on approval so resumption cannot
        # re-execute them or widen beyond the pending child's exact scope.
        if approval_bubble is not None:
            completed = {}
            if isinstance(web_section, dict) and web_section.get("status") in successish:
                completed["web"] = web_section
            if isinstance(vault_section, dict) and vault_section.get("status") in successish:
                completed["vault"] = vault_section
            pending_branch = None
            pending_tool = None
            pending_arguments = None
            if isinstance(web_section, dict) and web_section.get("status") == "approval_required":
                pending_branch, pending_tool, pending_arguments = "web", "web_search", web_args
            elif isinstance(vault_section, dict) and vault_section.get("status") == "approval_required":
                pending_branch, pending_tool, pending_arguments = "vault", vault_tool, vault_args
            remember_completed_branches(
                owner=owner,
                session_id=session_id,
                request_id=request_id,
                args=args,
                completed=completed,
                pending_branch=pending_branch,
                pending_tool=pending_tool,
                pending_arguments=pending_arguments,
            )
        elif seal is not None and overall in {"complete", "partial", "failed"}:
            mark_seal_consumed(seal)

        total_ms = round((time.monotonic() - started) * 1000.0, 1)
        package = {
            "status": overall,
            "mode": mode,
            "vault_strategy": vault_strategy if mode in {"vault", "both"} else None,
            "unresolved": unresolved[:20],
            "metrics": {
                "web_ms": timings["web_ms"],
                "vault_ms": timings["vault_ms"],
                "vault_index_ms": timings.get("vault_index_ms") or 0.0,
                "vault_match_count": _vault_source_count(vault_section),
                "vault_strategy": vault_strategy if mode in {"vault", "both"} else None,
                "total_ms": total_ms,
                "concurrent": actually_concurrent,
                "planned_concurrent": launched_concurrent,
                "reused_branches": sorted(reused_branches),
                "shared_foreground_endpoint": shared,
                "route": {
                    key: route_detail.get(key)
                    for key in ("research_url", "default_url", "research_model", "default_model", "research_available")
                },
                "events": [
                    {k: v for k, v in event.items() if k != "parallel_research_event"}
                    for event in events
                ],
            },
        }
        if web_section is not None:
            package["web"] = web_section
        if vault_section is not None:
            package["vault"] = vault_section
        if shared and mode == "both" and vault_strategy == "deep":
            package["metrics"]["contention_fallback"] = "sequential"
            unresolved.append("Research role shares foreground endpoint; ran sequentially")
            package["unresolved"] = unresolved[:20]

        package = _apply_output_budget(package)
        await note({
            "operation": "parallel_research_completed",
            "status": package["status"],
            "duration_ms": total_ms,
            "concurrent": package["metrics"]["concurrent"],
            "planned_concurrent": package["metrics"]["planned_concurrent"],
            "vault_strategy": package.get("vault_strategy"),
            "vault_index_ms": package["metrics"].get("vault_index_ms"),
            "web_sources": len((package.get("web") or {}).get("results") or []),
            "vault_sources": _vault_source_count(package.get("vault")),
            "match_count": package["metrics"].get("vault_match_count"),
            "output_bytes": package["metrics"].get("output_bytes"),
        })

        result = {
            "output": json.dumps(package, ensure_ascii=False),
            "exit_code": 0 if package["status"] in {"complete", "partial", "approval_required"} else 1,
            "research_parallel": True,
            "parallel_status": package["status"],
        }
        if approval_bubble is not None:
            # Preserve the normal approval/resumption contract; never pretend success.
            result["approval_required"] = True
            result["executed"] = False
            result["exit_code"] = None
            if isinstance(approval_bubble.get("ask_user"), dict):
                result["ask_user"] = approval_bubble["ask_user"]
            result["error"] = "Parallel research is waiting for an exact user approval."
        return result
    finally:
        _ACTIVE.reset(token)


def schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": PARALLEL,
            "description": (
                "Concurrently search the public web and/or the authorized Obsidian vault. "
                "Default vault_strategy=fast uses indexed vault_knowledge_search (candidate "
                "matches, not verified claims). vault_strategy=deep runs bounded dispatch_dottie "
                "research. Use mode=both when personal notes and public information both matter. "
                "Returns one compact evidence package; you synthesize the final answer. "
                "Direct web_search, web_fetch, vault_knowledge_search, and dispatch_dottie remain "
                "available for specialized single-path work."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["web", "vault", "both"],
                        "description": "web=public search only; vault=authorized vault retrieval only; both=run independently and concurrently when safe.",
                    },
                    "vault_strategy": {
                        "type": "string",
                        "enum": ["fast", "deep"],
                        "description": "fast (default)=indexed lexical vault_knowledge_search without the Dottie agent; deep=existing dispatch_dottie verified research. Only for vault/both.",
                    },
                    "web_query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                        "description": "Required for web/both. Public search query; never invented when omitted.",
                    },
                    "vault_objective": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                        "description": "Required for vault/both. Vault search/research objective.",
                    },
                    "vault_constraints": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path_prefix": {"type": ["string", "null"]},
                            "required_sources": {"type": "boolean"},
                            "max_hops": {"type": "integer", "minimum": 0, "maximum": 3},
                        },
                    },
                },
                "required": ["mode"],
            },
        },
    }
