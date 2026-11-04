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
    if (_RATED_LIST.search(text) or _TITLE_LOOKUP.search(text)) and not _DEEP.search(text):
        return True  # "what did I rate X", "which manga have I rated 10"
    return bool(
        (_LOOKUP.search(text) or _KNOWLEDGE_QUESTION.search(text))
        and _VAULT_TOPIC.search(text)
        and not _DEEP.search(text)
        and not _PUBLIC_WEB_ONLY.search(text)
    )


_EMPTY_MARKERS = ('"matches": []', "No matches", "No files matching", "no matches found")


def _empty_result(result: dict) -> bool:
    output = str(result.get("output") or "").strip()
    return not output or any(marker in output[:400] for marker in _EMPTY_MARKERS)


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
    found_evidence: bool = False
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
                if not _empty_result(result):
                    self.found_evidence = True
                if tool in _SOURCE_TOOLS:
                    self.successful_reads += 1
                    if tool == "vault_knowledge_search":
                        self.successful_queries += 1
            else:
                self.consecutive_failures += 1
        reason = None
        if self.consecutive_failures >= 2:
            reason = "repeated_vault_failures"
        elif self.attempts >= 4 and not self.found_evidence:
            reason = "no_vault_evidence"  # several lookups, all empty: say so
        elif self.successful_reads >= 6:
            reason = "bounded_vault_evidence"
        elif self.attempts >= 9:
            reason = "vault_lookup_budget"
        if reason:
            self.stopped = True
        return reason


_TOPIC = re.compile(
    r"\b(?:about|on|regarding|concerning|for)\s+(?:the\s+)?(?P<topic>[^?!.\n]{2,120}?)\s*[?!.]*\s*$",
    re.I,
)


def knowledge_question_topic(text: str) -> str | None:
    """The subject of a simple "what do my notes say about X" question, or None.

    Used only to run the obvious first index search before the model's first
    round; anything unclear returns None and the model searches as before.
    """
    text = str(text or "").strip()
    if not (_KNOWLEDGE_QUESTION.search(text) and simple_vault_lookup(text)):
        return None
    match = _TOPIC.search(text)
    if not match:
        return None
    topic = re.sub(r"\s+in\s+my\s+(?:notes?|vault|obsidian)\s*$", "", match.group("topic").strip(), flags=re.I)
    if not re.search(r"\w", topic) or len(topic.split()) > 12:
        return None
    return topic


# --- library prefetch ------------------------------------------------------------
# The vault's media-note conventions (frontmatter Type values and the rating /
# playtime properties). Queries that match nothing inject nothing, so a vault
# with other conventions simply falls back to the model's own searches.
LIBRARY_TYPES = {
    "anime": "Anime", "manga": "Comic", "manhwa": "Comic", "comic": "Comic",
    "game": "Game", "movie": "Movie", "film": "Movie", "series": "Series", "show": "Series",
}
RATING_KEY = "My Rating"
PLAYTIME_KEY = "Playtime"

_KIND = re.compile(r"\b(anime|manga|manhwa|comics?|games?|movies?|films?|series|shows?)\b", re.I)
_RECOMMEND = re.compile(r"\b(?:recommend|suggest)\w*\b", re.I)
_RATED_LIST = re.compile(
    r"\b(?:which|what)\s+(?P<kind>anime|manga|manhwa|comics?|games?|movies?|films?|series|shows?)\s+"
    r"(?:have|did)\s+i\s+(?:rated?|given?|scored?)\s+(?:an?\s+)?(?P<n>10|[1-9])\b", re.I)
_TITLE_LOOKUP = re.compile(
    r"\b(?:what|how)\s+did\s+i\s+(?:rate|score)\s+(?P<a>[^?!.\n]{2,80})"
    r"|\bmy\s+(?:rating|score)\s+(?:for|of|on)\s+(?P<b>[^?!.\n]{2,80})"
    r"|\bhave\s+i\s+(?:ever\s+)?(?:watched|seen|read|played|finished|rated)\s+(?P<c>[^?!.\n]{2,80})", re.I)


def _library_type(text: str) -> str | None:
    match = _KIND.search(text or "")
    if not match:
        return None
    word = match.group(1).lower().rstrip("s") if match.group(1).lower() not in {"series"} else "series"
    return LIBRARY_TYPES.get(word)


_COMPARE = re.compile(
    r"\bcompare\s+(?:what\s+)?my\s+(?:obsidian\s+)?(?:vault\s+)?(?:notes?|vault)\s+(?:say\s+)?(?:on|about|for|regarding)\s+"
    r"(?P<topic>[^?!.\n]{2,80}?)\s+(?:with|to|against|and)\s+(?:the\s+)?"
    r"(?:public|online|official|what\s+(?:the\s+)?(?:internet|web))", re.I)


def compare_topic(text: str) -> str | None:
    """The subject of "compare my notes on X with public information", or None."""
    match = _COMPARE.search(str(text or ""))
    if not match:
        return None
    topic = match.group("topic").strip()
    return topic if topic and len(topic.split()) <= 10 else None


_BROAD = re.compile(
    r"\b(?:summari[sz]e|sum\s+up|overview|recap|everything|all\s+(?:that\s+)?(?:i|my)|what\s+all)\b"
    r".{0,60}?\b(?:my\s+(?:obsidian\s+)?(?:vault|notes?)|in\s+(?:my\s+)?(?:notes?|vault))\b"
    r".{0,40}?\b(?:about|on|regarding|concerning|for)\s+(?P<topic>[^?!.\n]{2,120}?)\s*[?!.]*\s*$", re.I)
_BROAD_MUTATION = re.compile(r"\b(?:add|create|write|edit|update|fix|change|delete|remove|rename|move|tag)\b", re.I)
BROAD_NOTE_READS = 8


def broad_topic(text: str) -> str | None:
    """Search terms for "summarise what my notes say about X"-style questions, or None."""
    text = str(text or "").strip()
    match = _BROAD.search(text)
    if not match or _BROAD_MUTATION.search(text) or _PUBLIC_WEB_ONLY.search(text):
        return None
    from src.knowledge_engine.research import _evidence_query
    query = _evidence_query(match.group("topic"))
    return query if query and re.search(r"\w", query) else None


def vault_prefetch_requests(text: str) -> list[dict]:
    """Obvious first vault queries for a library question, run before round 1.

    Returns vault_knowledge_search argument objects, or [] when the request is
    not a simple library question. Never used for edits or web comparisons.
    """
    text = str(text or "").strip()
    broad = broad_topic(text)
    if broad:
        # "_broad" is internal: the prefetch reads the distinct matching notes next.
        return [{"query": broad, "top_k": 30, "_broad": True}]
    topic = compare_topic(text)
    if topic:
        return [{"query": topic, "top_k": 10}]  # the notes side of a notes-vs-public comparison
    if not text or _DEEP.search(text.replace("recommend", "")) or _PUBLIC_WEB_ONLY.search(text):
        return []
    from src.turn_contract import _PERSONAL_LIBRARY_RE
    if _RECOMMEND.search(text) and _PERSONAL_LIBRARY_RE.search(text):
        kind = _library_type(text)
        if not kind:
            return []
        if kind == "Game":
            # Few games are rated; playtime is the main taste signal, ratings a strong one.
            return [{"mode": "metadata_only", "property_filters": {"Type": kind, PLAYTIME_KEY: {"exists": True}},
                     "fields": [PLAYTIME_KEY, "Genre"], "sort_by": PLAYTIME_KEY, "top_k": 25},
                    {"mode": "metadata_only", "property_filters": {"Type": kind, RATING_KEY: {">=": 1}},
                     "fields": [RATING_KEY, "Genre"], "sort_by": RATING_KEY, "top_k": 25}]
        return [
            {"mode": "metadata_only", "property_filters": {"Type": kind}, "fields": [RATING_KEY, "Genre", "Year"],
             "sort_by": RATING_KEY, "top_k": 40},
            {"mode": "metadata_only", "property_filters": {"Type": kind}, "fields": [], "top_k": 200},
        ]
    rated = _RATED_LIST.search(text)
    if rated:
        kind = _library_type(rated.group("kind"))
        if kind:
            return [{"mode": "metadata_only", "property_filters": {"Type": kind, RATING_KEY: int(rated.group("n"))},
                     "fields": [], "top_k": 200}]
    title = _TITLE_LOOKUP.search(text)
    if title:
        topic = (title.group("a") or title.group("b") or title.group("c") or "").strip()
        topic = re.sub(r"\s+(?:yet|already|before|ever)\s*$", "", topic, flags=re.I)
        if topic and len(topic.split()) <= 10:
            return [{"query": topic, "top_k": 10}]
    topic = knowledge_question_topic(text)
    if topic:
        return [{"query": topic, "top_k": 10}]
    return []
