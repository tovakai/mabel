"""Request-local convergence budget for simple vault document lookups.

Never grants tools, changes roots, or converts failures to successes. Deep
research, audits, and multi-step work are intentionally out of scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

# Workspace reads count too: on vault turns the model mixes indexed search
# with read_file/grep/glob/ls, and a budget that ignored them never fired.
_WORKSPACE_LOOKUP_TOOLS = frozenset({"grep", "glob", "ls", "get_workspace"})
READ_TOOLS = frozenset({
    "vault_knowledge_search", "vault_traverse_graph", "vault_read_range",
    "vault_index_status", "read_file",
}) | _WORKSPACE_LOOKUP_TOOLS
_SOURCE_TOOLS = frozenset({
    "vault_knowledge_search", "vault_traverse_graph", "vault_read_range", "read_file",
})
_LOOKUP = re.compile(
    r"^\s*(?:please\s+)?(?:find|locate|show\s+me|where\s+(?:is|are)|"
    r"look\s+up|search\s+for)\b", re.I,
)
# "What do my notes say about X?" is a lookup plus a short summary, not research.
_KNOWLEDGE_QUESTION = re.compile(
    r"^\s*(?:please\s+)?(?:what\s+(?:do|does|did)\s+(?:my|the)\s+(?:notes?|vault|obsidian)\s+"
    r"(?:say|have|contain|mention)|what(?:'s|\s+is)\s+in\s+my\s+(?:notes?|vault)|"
    r"what\s+have\s+i\s+(?:written|noted)|do\s+i\s+have\s+(?:any\s+)?notes?)\b", re.I,
)
_VAULT_TOPIC = re.compile(
    r"\b(?:vault|obsidian|note(?:s)?|document(?:ation|s)?|"
    r"markdown|files?|wiki(?:links?)?)\b", re.I,
)
_DEEP = re.compile(
    r"\b(?:audit|analy[sz]e|compare|research|investigate|comprehensive|"
    r"exhaustive|entire|every|all\s+(?:my|the)|summari[sz]e|"
    r"write|edit|change|create|update|fix|modify)\b", re.I,
)


_PUBLIC_WEB_ONLY = re.compile(
    r"\b(?:on\s+the\s+web|on\s+the\s+internet|online|"
    r"on\s+pypi|on\s+github|on\s+a\s+website)\b", re.I,
)


def simple_vault_lookup(text: str) -> bool:
    text = str(text or "").strip()
    return bool(
        (_LOOKUP.search(text) or _KNOWLEDGE_QUESTION.search(text))
        and _VAULT_TOPIC.search(text)
        and not _DEEP.search(text)
        and not _PUBLIC_WEB_ONLY.search(text)
    )


@dataclass
class VaultLookupBudget:
    """Stop simple document location after useful evidence or a bounded failure.

    Counts completed tool calls, never proposed or unexecuted operations.
    A request's security and consent boundary remains authoritative.
    """
    attempts: int = 0
    successful_reads: int = 0
    consecutive_failures: int = 0
    successful_queries: int = 0
    stopped: bool = False

    def observe(self, records) -> str | None:
        if self.stopped:
            return None
        for record in records or ():
            if not isinstance(record, dict):
                continue
            tool = record.get("tool_name")
            if tool not in READ_TOOLS:
                continue
            self.attempts += 1
            result = record.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            ok = (result.get("exit_code") == 0
                  and not result.get("error") and not result.get("blocked"))
            if ok:
                self.consecutive_failures = 0
                if tool in _SOURCE_TOOLS:
                    self.successful_reads += 1
                    if tool == "vault_knowledge_search":
                        self.successful_queries += 1
            else:
                self.consecutive_failures += 1
        reason = None
        if self.consecutive_failures >= 2:
            reason = "repeated_vault_failures"
        elif self.successful_reads >= 6:
            reason = "bounded_vault_evidence"
        elif self.attempts >= 9:
            reason = "vault_lookup_budget"
        if reason:
            self.stopped = True
        return reason
