"""Observational, per-turn tool-routing trace.

Answers "why did (or didn't) the model see tool X on this turn?" without
reading the routing source.  Every routing stage reports the tool set it
produced; the trace attributes each exclusion to the first stage that removed
the tool, with a reason code from a fixed vocabulary.

Contract with the routing code (enforced by construction):

* Observational only.  Module-level functions return ``None`` and never raise.
  Routing code must not branch on anything from this module.
* Always collected when a trace is bound, independently of ``diagnostic_events``.
  Persistence follows the debug console's existing rules (skipped for
  incognito turns) and goes through its whitelist sanitizer.
* Records tool names, family names, reason codes, counts and token estimates.
  Never prompt text, tool arguments, tool output, URLs or credentials.

Per-tool lifecycle (each a superset of the next, except ``discovered``):

    registered   schema exists in the native + MCP inventory for this request
    authorized   survives user toggles, privileges, settings, request policy
    discoverable reachable through ``discover_tools`` (authorized minus authority denials)
    selected     offered by the turn contract (initial selection)
    advertised   schema actually sent to the provider in at least one round
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import functools
import json
import logging
import re

logger = logging.getLogger(__name__)

STATES = ("registered", "authorized", "discoverable", "selected", "advertised")
SCHEMA_VERSION = 1

# Fixed reason vocabulary. Codes are persisted; descriptions are for humans.
REASONS = {
    # registration
    "not_registered": "No schema with this name exists in this request's native/MCP inventory.",
    "mcp_not_loaded": "MCP schemas were not loaded for this turn (plan mode, MCP disabled, or owner blocked).",
    "host_shell_product_surface": "host_shell is environment-owned and never offered on the product surface.",
    # authorization (first denial source wins)
    "browser_page_commands_unavailable": "Browser page commands are not available in this installation.",
    "delegated_credential": "Blocked for delegated (API-token) credentials.",
    "ui_toggle_bash_off": "The Agent 'bash' toggle was off for this request.",
    "web_toggle_off": "The Agent web toggle was explicitly off.",
    "web_not_enabled_for_turn": "Web tools were not enabled: toggle unset and no web intent detected.",
    "legacy_web_turn_narrowing": "Legacy (non-contract) web turn removed personal/shell tools.",
    "incognito": "Disabled in incognito sessions.",
    "active_email_review": "Disabled while an email review card is active.",
    "user_privilege": "The user's privileges do not include this tool.",
    "global_disabled_setting": "Disabled by the global 'disabled_tools' setting.",
    "legacy_auto_escalation": "Chat auto-escalated to agent without a contract: shell/write tools withheld.",
    "compare_session": "Comparison session: document tools withheld.",
    "compare_mode": "Compare mode strips tools.",
    "plan_mode": "Plan mode is read-only.",
    "user_prohibited_in_message": "The user's message prohibited this tool.",
    "guide_only_turn": "The message asked for no tool use (guide-only turn).",
    "request_policy": "Disabled by the request tool policy.",
    "explicit_no_tool_use": "The message explicitly denies all tool use.",
    "owner_blocked": "Blocked for this owner by tool security.",
    "sft_workspace_guard": "Workspace tools are disabled for this owner (SFT fixture guard).",
    "authority_denied": "RequestAuthority denies this tool for the request.",
    # selection (turn contract)
    "not_in_turn_families": "Authorized, but not in any capability family the turn classifier selected.",
    "narrowed_by_request_seal": "The request classifier sealed the turn to an exact tool set that excludes this tool.",
    "narrowed_by_sealed_read": "A resolved read-only operation sealed the turn to its single reader.",
    "superseded_by_mcp_alias": "The MCP email schema replaces this legacy alias.",
    "contract_requirement_unavailable": "A required tool/capability was unavailable, so the contract offered no tools at all.",
    "scoped_out_post_contract": "Removed by post-contract scoping (clean-v3 preview / routing experiment).",
    # agent loop assembly
    "force_answer_no_tools": "The loop is forcing a final answer: no tools sent.",
    "tool_surface_none": "The model's configured tool surface is 'none'.",
    "legacy_router_textual": "Legacy 9B router transport: tools are named in the prompt, no native schemas.",
    "guide_only_no_schemas": "Guide-only turn: no native schemas sent.",
    "textual_transport": "The route uses textual (non-native) tool transport: no native schemas sent.",
    "disabled_snapshot_agent_loop": "Removed by the agent loop's disabled-tools snapshot.",
    "agent_loop_relevant_narrowing": "Removed by agent-loop request narrowing (e.g. single-note vault rewrite seal).",
    "agent_loop_pure_web_narrowing": "(Retired) The agent loop's own intent classifier narrowed a contract to web-only tools.",
    "agent_loop_vault_rewrite_seal": "Single-note vault rewrite seal: only read/write/edit file tools are offered.",
    "web_unavailable_partial": "Web tools are disabled; the turn continues with its authorized non-web tools.",
    "no_model_request": "The turn ended before any model request (deterministic reply, refusal or early exit).",
    "surface_filter": "Removed while formatting/filtering schemas for the model's tool surface.",
    "legacy_rag_not_retrieved": "Legacy (non-contract) path: not retrieved by tool RAG/keyword routing.",
    "candidate_specific_drop": "Dropped for this specific fallback candidate's request.",
    # additions
    "family": "Member of a selected capability family.",
    "request_seal": "Exact tool selected by the request classifier.",
    "sealed_read": "Reader for a resolved read-only operation.",
    "core_fallback": "No family recognized: small core recovery set.",
    "browser_web_recovery": "Browser kept as bounded recovery for a web turn.",
    "email_read_continuation": "Email readers kept after search narrowing.",
    "always_available": "Owner-checked visible surface (active document, saved task result, approval continuation).",
    "warm_tool": "Successfully used earlier in this conversation.",
    "controls": "Neutral controls (ask_user/update_plan) added with any selection.",
    "agent_loop_seal_readd": "Agent loop re-added an admitted writer/reader missing from the contract.",
    "discover_tools": "Added mid-turn through discover_tools.",
}

_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,120}$")
_ACTIVE: ContextVar["RoutingTrace | None"] = ContextVar("tool_routing_trace", default=None)


def _safe(fn):
    """Never let diagnostics affect the turn."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:  # pragma: no cover - defensive by design
            trace = _ACTIVE.get()
            if trace is not None:
                trace.errors += 1
            logger.debug("routing trace observation failed", exc_info=True)
        return None

    return wrapper


def _names(values) -> set[str]:
    out = set()
    for value in values or ():
        if isinstance(value, dict):
            value = (value.get("function") or {}).get("name") or value.get("name")
        if isinstance(value, str) and _NAME_RE.fullmatch(value):
            out.add(value)
    return out


def _schema_name(schema) -> str:
    if isinstance(schema, dict):
        return str((schema.get("function") or {}).get("name") or schema.get("name") or "")
    return ""


def _code(value: str) -> str:
    value = str(value or "")
    return value if _CODE_RE.fullmatch(value) and value in REASONS else "request_policy"


def schema_token_estimate(schemas) -> int:
    """The runtime's own schema-cost estimate (generation_budget)."""
    try:
        from src.generation_budget import estimate_tool_schema_tokens
        return int(estimate_tool_schema_tokens(list(schemas or [])))
    except Exception:
        return 0


class RoutingTrace:
    def __init__(self):
        self.profile: dict = {}
        self.registered: dict[str, str] = {}
        self.denied: dict[str, str] = {}
        self._denial_seen: set[str] = set()
        self.reenabled: dict[str, str] = {}
        self.authorized: set[str] | None = None
        self.discoverable: set[str] | None = None
        self.contract: dict = {}
        self.selected: set[str] | None = None
        self.selection_drop: dict[str, str] = {}
        self.selection_add: dict[str, str] = {}
        self.post_contract_drop: dict[str, str] = {}
        self.advisory: dict[str, list[str]] = {}
        self.rounds: list[dict] = []
        self._pending_assembly: dict | None = None
        self.discovered: dict[str, int] = {}
        self.discovery_refused: dict[str, str] = {}
        self.contract_resolutions = 0
        self.errors = 0

    # ---- registration / authorization -------------------------------------------------
    def register(self, schemas, source: str):
        for name in _names(schemas):
            self.registered.setdefault(name, source)

    def checkpoint_denials(self, label: str, current):
        """Attribute newly disabled names to ``label``; note re-enables."""
        current = _names(current)
        for name in current - self._denial_seen:
            self.denied.setdefault(name, _code(label))
        for name in self._denial_seen - current:
            if name in self.denied:
                self.reenabled[name] = _code(label) if label in REASONS else "request_policy"
                self.denied.pop(name, None)
        self._denial_seen = set(current)

    def deny(self, names, label: str):
        for name in _names(names):
            self.denied.setdefault(name, _code(label))
            self._denial_seen.add(name)

    def policy(self, policy):
        reasons = getattr(policy, "reasons", {}) or {}
        for name in _names(getattr(policy, "all_disabled_names", lambda: ())()):
            if name in self.denied:
                continue
            text = str(reasons.get(name, ""))
            if "prohibited by the user" in text:
                self.denied[name] = "user_prohibited_in_message"
            elif "guide-only" in text.lower() or getattr(policy, "mode", "") == "guide_only":
                self.denied[name] = "guide_only_turn"
            else:
                self.denied[name] = "request_policy"

    # ---- turn contract -----------------------------------------------------------------
    def contract_steps(self, *, families, executable, steps, offered, unavailable,
                       required, operation_tool, selected_tools, canonical=None):
        """Called once per resolve_turn_contract; the last call wins.

        ``steps`` are the contract's own canonical selection sets in order.
        Inventory names (e.g. ``mcp__email__read_email``) are mapped through the
        contract's ``canonical`` function so attribution matches selection.
        """
        canon = canonical or (lambda n: n)
        self.contract_resolutions += 1
        executable = _names(executable)
        offered = _names(offered)
        self.authorized = set(executable) if self.authorized is None else self.authorized | executable
        drop_c, add_c = {}, {}
        prev: set[str] = set()
        for add_label, drop_label, current in steps:
            current = set(current)
            for name in current - prev:
                add_c.setdefault(name, add_label)
            for name in prev - current:
                drop_c[name] = drop_label
            prev = current
        drop, add = {}, {}
        for name in executable:
            c = canon(name)
            if name in offered:
                if c in add_c:
                    add[name] = add_c[c]
                continue
            if unavailable and c in prev:
                drop[name] = "contract_requirement_unavailable"
            elif c in drop_c:
                drop[name] = drop_c[c]
            elif c in prev:
                drop[name] = "contract_requirement_unavailable" if unavailable else "superseded_by_mcp_alias"
            else:
                drop[name] = "not_in_turn_families"
        self.selection_drop = drop
        self.selection_add = add
        self.selected = set(offered)
        self.contract.update({
            "families": sorted(str(f) for f in families),
            "required": sorted(_names(required)),
            "unavailable": sorted(str(u)[:80] for u in unavailable),
            "sealed_read_tool": operation_tool if operation_tool and _NAME_RE.fullmatch(operation_tool) else None,
            "request_seal": sorted(_names(selected_tools)) if selected_tools is not None else None,
        })

    def final_contract(self, contract, label: str = "final"):
        offered = _names(getattr(contract, "offered", ()))
        before = set(self.selected or ())
        for name in before - offered:
            self.post_contract_drop.setdefault(name, "scoped_out_post_contract")
        for name in offered - before:
            self.selection_add.setdefault(name, "always_available")
        self.selected = offered
        self.contract["selection_mode"] = str(getattr(contract, "selection_mode", ""))[:40]
        self.contract["routing_experiment"] = str(getattr(contract, "routing_experiment", ""))[:40]
        self.contract["capabilities"] = sorted(str(c) for c in getattr(contract, "capabilities", ()))
        self.contract["active_capabilities"] = sorted(str(c) for c in getattr(contract, "active_capabilities", ()))
        self.contract["final_stage"] = label

    def catalog(self, inventory):
        self.discoverable = _names(inventory)

    # ---- agent loop --------------------------------------------------------------------
    def assembly(self, path: str, stages):
        """One `_tool_schemas_for_route` evaluation; committed by `advertise`."""
        expanded = []
        for label, value in stages:
            if value is not None and hasattr(value, "schemas"):
                value = value.schemas()  # a TurnContract: offered + discovered schemas
            expanded.append((label, _names(value or ())))
        self._pending_assembly = {"path": path, "stages": expanded}

    def advertise(self, *, round_num, candidate_index, model, endpoint_id, schemas, context_limit,
                  textual_reason=None):
        if isinstance(endpoint_id, (list, tuple)):  # raw candidate route descriptors
            descriptor = endpoint_id[candidate_index] if 0 <= int(candidate_index or 0) < len(endpoint_id) else {}
            endpoint_id = descriptor.get("endpoint_id") if isinstance(descriptor, dict) else ""
        names = _names(schemas)
        tokens = schema_token_estimate(schemas)
        per_tool = {}
        for schema in schemas or ():
            name = _schema_name(schema)
            if name in names:
                per_tool[name] = schema_token_estimate([schema])
        assembly = self._pending_assembly or {}
        self._pending_assembly = None
        drops = {}
        prev = None
        for label, current in assembly.get("stages", ()):
            if prev is not None:
                for name in prev - current:
                    drops.setdefault(name, label)
            prev = current
        entry = {
            "round": int(round_num or 0),
            "candidate": int(candidate_index or 0),
            "model": str(model or ""),
            "endpoint_id": str(endpoint_id or ""),
            "path": assembly.get("path", "unknown"),
            "tools": sorted(names),
            "drops": drops,
            "schema_tokens_estimate": tokens,
            "per_tool_tokens_estimate": per_tool,
            "context_limit": int(context_limit or 0),
        }
        if textual_reason:
            entry["textual_reason"] = _code(textual_reason)
        last = self.rounds[-1] if self.rounds else None
        if last and last["candidate"] == entry["candidate"] and last["tools"] == entry["tools"] \
                and last["model"] == entry["model"]:
            last["repeat_rounds"] = last.get("repeat_rounds", 0) + 1
            last["last_round"] = entry["round"]
            return
        if len(self.rounds) < 64:
            self.rounds.append(entry)

    def discover(self, added, refused=None, code=None):
        current_round = self.rounds[-1]["round"] if self.rounds else 0
        for name in _names(added):
            self.discovered.setdefault(name, current_round)
        if refused:
            for name in _names(refused):
                self.discovery_refused[name] = str(code or "unavailable_tool")[:40]

    # ---- summary -----------------------------------------------------------------------
    def tool_state(self, name: str) -> dict:
        advertised = any(name in r["tools"] for r in self.rounds) or name in self.discovered
        registered = name in self.registered
        authorized = (name in self.authorized) if self.authorized is not None else (registered and name not in self.denied)
        discoverable = (name in self.discoverable) if self.discoverable is not None else None
        selected = name in (self.selected or ())
        states = {"registered": registered, "authorized": authorized, "discoverable": discoverable,
                  "selected": selected, "advertised": advertised}
        reason = None
        if not registered:
            reason = "not_registered"
        elif not authorized:
            reason = self.denied.get(name, "request_policy")
        elif not selected and name not in self.discovered:
            reason = self.selection_drop.get(name) or self.post_contract_drop.get(name) or (
                "not_in_turn_families" if self.selected is not None else "legacy_rag_not_retrieved")
        elif not advertised:
            reason = next((r["drops"][name] for r in self.rounds if name in r["drops"]), None)
            if reason is None and self.rounds:
                reason = next((r.get("textual_reason") for r in self.rounds if r.get("textual_reason")), None)
            reason = reason or ("candidate_specific_drop" if self.rounds else "no_model_request")
        added = None
        if advertised or selected:
            added = "discover_tools" if (name in self.discovered and not selected) else self.selection_add.get(name)
        return {"states": states, "reason": reason, "added_by": added}

    def summary(self) -> dict:
        names = set(self.registered) | set(self.denied) | set(self.selected or ()) | set(self.discovered)
        names.discard("web_egress")  # policy marker, not a tool (see profile.web_explicitly_off)
        for r in self.rounds:
            names |= set(r["tools"])
        tools = {}
        for name in sorted(names):
            st = self.tool_state(name)
            code = "".join(
                "?" if st["states"][state] is None else (letter if st["states"][state] else "-")
                for state, letter in zip(STATES, "RADSV")
            )
            item = {"s": code}
            if st["reason"]:
                item["why"] = st["reason"]
            if st["added_by"]:
                item["via"] = st["added_by"]
            tools[name] = item
        first = self.rounds[0] if self.rounds else {}
        limit = first.get("context_limit") or 0
        peak = max((r["schema_tokens_estimate"] for r in self.rounds), default=0)
        return {
            "v": SCHEMA_VERSION,
            "profile": dict(self.profile),
            "contract": dict(self.contract, resolutions=self.contract_resolutions),
            "advisory": {k: sorted(v)[:40] for k, v in self.advisory.items()},
            "rounds": self.rounds,
            "discovered": dict(self.discovered),
            "discovery_refused": dict(self.discovery_refused),
            "reenabled": dict(self.reenabled),
            "tokens": {
                "first_round_schema_estimate": first.get("schema_tokens_estimate", 0),
                "peak_schema_estimate": peak,
                "context_limit": limit,
                "first_round_fraction": round(first.get("schema_tokens_estimate", 0) / limit, 4) if limit else None,
            },
            "counts": {state: sum(1 for t in tools.values() if t["s"][i] not in "-?")
                       for i, state in enumerate(STATES)},
            "tools": tools,
            "trace_errors": self.errors,
        }


# ---- module API: all no-ops without a bound trace ------------------------------------------

def model_profile(model, configured_mode=None) -> dict:
    """Which routing profile a model name selects, and why. Never raises."""
    out: dict = {}
    try:
        from src.model_profiles import is_odysseus_tool_profile_model, tool_schema_profile
        mode = str(configured_mode or "").strip().lower()
        odysseus = bool(is_odysseus_tool_profile_model(model))
        out["schema_profile"] = str(tool_schema_profile(model))
        out["profile_reason"] = (
            f"configured_tool_mode:{mode}" if mode
            else "name_token_odysseus_or_ajax" if odysseus
            else "default_generic_name"
        )
    except Exception:
        out["profile_reason"] = "unresolved"
    try:
        from src.agent_loop import _is_qwen38_tool_router
        out["legacy_9b_router_name"] = bool(_is_qwen38_tool_router(model))
    except Exception:
        pass
    return out


def current() -> "RoutingTrace | None":
    return _ACTIVE.get()


def new_trace() -> RoutingTrace:
    return RoutingTrace()


def bind(trace: "RoutingTrace | None"):
    """Bind for the remainder of the current context (request handler/stream)."""
    try:
        return _ACTIVE.set(trace)
    except Exception:  # pragma: no cover
        return None


@contextmanager
def bound(trace: "RoutingTrace | None"):
    token = _ACTIVE.set(trace)
    try:
        yield trace
    finally:
        _ACTIVE.reset(token)


def _call(method: str, *args, **kwargs):
    trace = _ACTIVE.get()
    if trace is None:
        return
    try:
        getattr(trace, method)(*args, **kwargs)
    except Exception:
        trace.errors += 1
        logger.debug("routing trace %s failed", method, exc_info=True)


@_safe
def profile(**fields):
    trace = _ACTIVE.get()
    if trace is not None:
        for key, value in fields.items():
            if isinstance(value, (bool, int, float)) or value is None:
                trace.profile[key] = value
            else:
                trace.profile[key] = str(value)[:120]


@_safe
def register(schemas, source="native"):
    _call("register", schemas, source)


@_safe
def checkpoint_denials(label, current_names):
    _call("checkpoint_denials", label, current_names)


@_safe
def deny(names, label):
    _call("deny", names, label)


@_safe
def policy(tool_policy):
    _call("policy", tool_policy)


@_safe
def contract_steps(**kwargs):
    _call("contract_steps", **kwargs)


@_safe
def final_contract(contract, label="final"):
    _call("final_contract", contract, label)


@_safe
def catalog(inventory):
    _call("catalog", inventory)


@_safe
def advisory(kind, names):
    trace = _ACTIVE.get()
    if trace is not None:
        trace.advisory[str(kind)[:40]] = sorted(_names(names))


@_safe
def assembly(path, stages):
    _call("assembly", path, stages)


@_safe
def advertise(**kwargs):
    _call("advertise", **kwargs)


@_safe
def discover(added, refused=None, code=None):
    _call("discover", added, refused, code)


@_safe
def persist(trace, store, owner, session, turn):
    if trace is None or store is None:
        return
    store.emit(str(owner), str(session), str(turn), "RUNTIME", "tool_routing", "resolved",
               metadata={"routing": trace.summary()})


# ---- explanation (CLI / console) ----------------------------------------------------------

def explain(summary: dict, tool: str) -> list[str]:
    """Human-readable answer to 'why did/didn't the model see <tool>?'."""
    lines = []
    tools = summary.get("tools") or {}
    profile_ = summary.get("profile") or {}
    contract = summary.get("contract") or {}
    lines.append(f"model={profile_.get('model')} endpoint={profile_.get('endpoint_id')} "
                 f"runtime={profile_.get('runtime')} contract={profile_.get('use_turn_contract')} "
                 f"surface={profile_.get('tool_surface') or 'default'} schema_profile={profile_.get('schema_profile')}")
    lines.append(f"families={contract.get('families')} seal={contract.get('request_seal')} "
                 f"sealed_read={contract.get('sealed_read_tool')} unavailable={contract.get('unavailable')}")
    item = tools.get(tool)
    if item is None:
        lines.append(f"{tool}: never seen on this turn -> {REASONS['not_registered']}")
        return lines
    state = item.get("s", "-----")
    labels = [f"{s}={'yes' if c not in '-?' else ('n/a' if c == '?' else 'no')}"
              for s, c in zip(STATES, state)]
    lines.append(f"{tool}: " + " ".join(labels))
    if item.get("why"):
        code = item["why"]
        lines.append(f"  excluded: {code} -> {REASONS.get(code, code)}")
    if item.get("via"):
        code = item["via"]
        lines.append(f"  added by: {code} -> {REASONS.get(code, code)}")
    for r in summary.get("rounds") or ():
        mark = "advertised" if tool in r.get("tools", ()) else ("dropped:" + r["drops"][tool] if tool in r.get("drops", {}) else "absent")
        span = f"{r['round']}" + (f"-{r['last_round']}" if r.get("last_round") else "")
        lines.append(f"  round {span} cand={r['candidate']} {r['model']} path={r['path']} "
                     f"tools={len(r['tools'])} ~{r['schema_tokens_estimate']}tok/{r['context_limit']}: {mark}")
    if tool in (summary.get("discovered") or {}):
        lines.append(f"  discovered via discover_tools in round {summary['discovered'][tool]}")
    if tool in (summary.get("discovery_refused") or {}):
        lines.append(f"  discover_tools refused: {summary['discovery_refused'][tool]}")
    for kind, names in (summary.get("advisory") or {}).items():
        lines.append(f"  advisory[{kind}] {'includes' if tool in names else 'omits'} {tool}")
    return lines
