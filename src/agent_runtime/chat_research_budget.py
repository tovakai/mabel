"""Configurable convergence budget for ordinary chat web research.

Deep research keeps its own multi-step planner. This helper only stops the
interactive agent from circling: progress is new unique queries/URLs, stall is
repeated work without new evidence, and absolute search/fetch/hard-round caps
force synthesis. Soft synthesis is advisory only — productive research may
continue past that threshold until a hard limit or stall is hit.

Read-only local retrieval (vault/workspace reads) participates in stall
detection and the advisory soft nudge, with progress meaning new evidence: a
file not yet read, result paths not yet seen, or a new URL. Empty results,
failures and repeats are not progress. Local rounds never count toward the
web search/fetch budgets or the hard round limit, so a long, productive vault
audit is never cut off by them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from src.agent_runtime.web_visit_ledger import normalize_fetch_url


def _setting_int(name: str, default: int, *, minimum: int = 1, maximum: int = 200) -> int:
    try:
        from src.settings import get_setting

        value = int(get_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True)
class ResearchBudgetDecision:
    """Outcome of one research-tool round observation."""

    hard_reason: Optional[str] = None
    soft_nudge: Optional[str] = None


@dataclass
class ChatResearchBudget:
    """Progress-aware web research budget for one interactive turn."""

    max_searches: int = 8
    max_fetches: int = 12
    stall_rounds: int = 3
    soft_synthesis_rounds: int = 10
    hard_synthesis_rounds: int = 24

    search_count: int = 0
    fetch_count: int = 0
    research_rounds: int = 0
    stall_streak: int = 0
    unique_queries: set[str] = field(default_factory=set)
    unique_urls: set[str] = field(default_factory=set)
    exhausted_reason: Optional[str] = None
    soft_nudge_emitted: bool = False
    # Web + local retrieval rounds (advisory soft nudge only).
    retrieval_rounds: int = 0
    evidence_keys: set[str] = field(default_factory=set)

    @classmethod
    def from_settings(cls) -> "ChatResearchBudget":
        soft = _setting_int("agent_web_research_soft_synthesis_rounds", 10, minimum=4)
        hard_default = max(soft * 2, 20)
        hard = _setting_int(
            "agent_web_research_hard_rounds",
            hard_default,
            minimum=soft + 1,
            maximum=200,
        )
        return cls(
            max_searches=_setting_int("agent_web_research_max_searches", 8),
            max_fetches=_setting_int("agent_web_research_max_fetches", 12),
            stall_rounds=_setting_int("agent_web_research_stall_rounds", 3, minimum=2),
            soft_synthesis_rounds=soft,
            hard_synthesis_rounds=hard,
        )

    def synthesis_instruction(self, reason: str) -> str:
        if reason == "stall":
            detail = (
                "Recent retrieval tools repeated earlier queries, URLs or results, "
                "or returned nothing new."
            )
        elif reason == "search_budget":
            detail = f"The configured search budget ({self.max_searches}) is exhausted."
        elif reason == "fetch_budget":
            detail = f"The configured fetch budget ({self.max_fetches}) is exhausted."
        elif reason == "hard_rounds":
            detail = (
                f"The hard research round limit ({self.hard_synthesis_rounds}) "
                "was reached."
            )
        else:
            detail = (
                f"This research turn reached the soft synthesis threshold "
                f"({self.soft_synthesis_rounds} tool rounds)."
            )
        if reason == "soft_synthesis":
            return (
                "You have gathered substantial evidence. Prefer synthesizing "
                "an answer from sources already collected. You may still read a "
                "specific source or run a materially different search if a concrete "
                f"gap remains. {detail}"
            )
        return (
            "Stop calling retrieval tools and synthesize the best answer from evidence "
            "already gathered. Cite sources you have. State remaining gaps briefly "
            f"instead of searching again. {detail}"
        )

    def observe_round(self, tool_records: list[Mapping[str, Any]] | None) -> ResearchBudgetDecision:
        """Update counters from completed tool results.

        ``hard_reason`` forces tool-free synthesis. ``soft_nudge`` is advisory
        and is emitted at most once while productive research may continue.
        """
        if self.exhausted_reason:
            return ResearchBudgetDecision(hard_reason=self.exhausted_reason)
        records = [record for record in (tool_records or ()) if isinstance(record, dict)]
        web_records = [
            record for record in records
            if record.get("tool_name") in {
                "web_search", "web_fetch", "private_browser", "research_parallel",
            }
        ]
        local_records = [record for record in records if record.get("tool_name") in LOCAL_RETRIEVAL_TOOLS]
        if not web_records and not local_records:
            return ResearchBudgetDecision()

        self.retrieval_rounds += 1
        if web_records:
            self.research_rounds += 1
        made_progress = False
        for record in local_records:
            keys = _local_evidence_keys(record)
            if keys - self.evidence_keys:
                made_progress = True
            self.evidence_keys |= keys
        for record in web_records:
            name = record.get("tool_name")
            content = str(record.get("content") or "")
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            try:
                exit_code = int(
                    0 if result.get("exit_code") == 0 else (result.get("exit_code") or 1)
                )
            except (TypeError, ValueError):
                exit_code = 1
            ok = (
                exit_code == 0
                and not result.get("error")
                and not result.get("blocked")
                and not result.get("approval_required")
            )
            if name == "web_search":
                self.search_count += 1
                query = _search_query(content).casefold()
                if ok and query and query not in self.unique_queries:
                    self.unique_queries.add(query)
                    made_progress = True
            elif name == "research_parallel":
                # Compound coordinator: count the web branch only when enabled.
                mode = _parallel_mode(content)
                if mode in {"web", "both"}:
                    self.search_count += 1
                    query = _parallel_web_query(content).casefold()
                    if ok and query and query not in self.unique_queries:
                        self.unique_queries.add(query)
                        made_progress = True
                    elif ok and not query:
                        made_progress = True
            elif name == "web_fetch":
                self.fetch_count += 1
                for url in _fetch_urls(content, result):
                    if ok and url and url not in self.unique_urls:
                        self.unique_urls.add(url)
                        made_progress = True
            elif name == "private_browser" and ok:
                made_progress = True

        if made_progress:
            self.stall_streak = 0
        else:
            self.stall_streak += 1

        hard_reason = None
        if self.search_count >= self.max_searches:
            hard_reason = "search_budget"
        elif self.fetch_count >= self.max_fetches:
            hard_reason = "fetch_budget"
        elif self.stall_streak >= self.stall_rounds:
            hard_reason = "stall"
        elif self.research_rounds >= self.hard_synthesis_rounds:
            hard_reason = "hard_rounds"
        if hard_reason:
            self.exhausted_reason = hard_reason
            return ResearchBudgetDecision(hard_reason=hard_reason)

        soft_nudge = None
        if (
            self.retrieval_rounds >= self.soft_synthesis_rounds
            and not self.soft_nudge_emitted
        ):
            self.soft_nudge_emitted = True
            soft_nudge = "soft_synthesis"
        return ResearchBudgetDecision(soft_nudge=soft_nudge)


# Read-only local retrieval that participates in stall detection.
LOCAL_RETRIEVAL_TOOLS = frozenset({
    "read_file", "grep", "glob", "ls", "get_workspace",
    "vault_knowledge_search", "vault_read_range", "vault_retrieve_batch",
    "vault_traverse_graph", "vault_index_status", "vault_audit", "dispatch_dottie",
})
_READ_TOOLS = frozenset({"read_file", "vault_read_range"})
_EMPTY_RESULT_PREFIXES = ("no files matching", "no matches", "no results", "0 matches", "(empty", "empty directory")


def _local_evidence_keys(record: Mapping[str, Any]) -> set[str]:
    """Deterministic evidence identities for one local retrieval result.

    A file read is keyed by its path (so reading a file a search only listed is
    new evidence); search/listing results by the paths or lines they surface.
    Failures and empty results yield no evidence.
    """
    import json

    name = str(record.get("tool_name") or "")
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    if result.get("exit_code") not in (0, None) or result.get("error") or result.get("blocked"):
        return set()
    output = str(result.get("output") or "").strip()
    if not output or output.casefold().startswith(_EMPTY_RESULT_PREFIXES):
        return set()
    try:
        args = json.loads(str(record.get("content") or "{}"))
    except (TypeError, ValueError):
        args = {}
    args = args if isinstance(args, dict) else {}
    if name in _READ_TOOLS:
        target = str(args.get("path") or args.get("note_id") or args.get("file") or "").strip()
        span = f":{args.get('start_line') or args.get('offset') or ''}-{args.get('end_line') or args.get('limit') or ''}"
        return {f"read:{target}{span}" if target else f"read-output:{hash(output)}"}
    keys = set()
    if output.startswith("{"):
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            for bucket in ("matches", "results", "findings", "notes"):
                items = parsed.get(bucket)
                # Status payloads use the same names for counts ({"notes": 1267}).
                if not isinstance(items, list):
                    continue
                for item in items:
                    if isinstance(item, dict):
                        path = item.get("rel_path") or item.get("path") or item.get("note_id")
                        if path:
                            keys.add(f"path:{path}:{item.get('start_line') or ''}")
            if keys:
                return keys
    for line in output.splitlines():
        line = line.strip()
        if not line or line.endswith(":") and "/" in line:
            continue  # listing header
        head = line.split(":", 1)[0] if "/" in line.split(":", 1)[0] else ""
        keys.add(f"path:{head}" if head else f"line:{name}:{line[:200]}")
    return keys


def _search_query(content: str) -> str:
    import json
    import re

    raw = str(content or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            return str(parsed.get("query") or "").strip()
    match = re.search(r"query\s*[:=]\s*(.+)$", raw, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip().strip('"')
    return raw.splitlines()[0].strip() if raw else ""


def _parallel_payload(content: str) -> dict:
    import json

    raw = str(content or "").strip()
    if not raw.startswith("{"):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parallel_mode(content: str) -> str:
    mode = str(_parallel_payload(content).get("mode") or "both").strip().casefold()
    return mode if mode in {"web", "vault", "both"} else "both"


def _parallel_web_query(content: str) -> str:
    return str(_parallel_payload(content).get("web_query") or "").strip()


def _fetch_urls(content: str, result: Mapping[str, Any]) -> list[str]:
    from src.agent_runtime.web_visit_ledger import fetch_request_from_tool_content

    request = fetch_request_from_tool_content(content)
    urls = [str(item).strip() for item in (request.get("urls") or []) if str(item).strip()]
    if request.get("url"):
        urls = [str(request.get("url") or "").strip(), *urls]
    if not urls:
        for entry in result.get("batch_results") or ():
            if isinstance(entry, dict) and entry.get("url"):
                urls.append(str(entry.get("url") or "").strip())
    if not urls:
        single = str(result.get("url") or "").strip()
        if single:
            urls.append(single)
    if not urls:
        output = str(result.get("output") or "")
        for line in output.splitlines():
            if line.startswith("Source: "):
                urls.append(line[8:].strip())
                break
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        key = normalize_fetch_url(url)
        if key and key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered
