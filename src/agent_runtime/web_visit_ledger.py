"""Turn-local ledger of web_fetch visits that survives message compaction.

Tracks URL evidence independently of the prompt so compaction cannot make the
model "forget" that a page was already read. Legitimate upgrades remain open:
partial downloads may be re-fetched with full=true, and a new extraction_goal
may re-read a page for additional focused evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


class FetchKind(str, Enum):
    PARTIAL = "partial"
    FOCUSED = "focused"
    COMPLETE = "complete"


_KIND_RANK = {
    FetchKind.PARTIAL: 1,
    FetchKind.FOCUSED: 2,
    FetchKind.COMPLETE: 3,
}


def normalize_fetch_url(url: str) -> str:
    """Canonicalize a fetch URL for visit comparison without rewriting queries."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.rstrip("/")
    scheme = (parts.scheme or "https").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Drop fragments only; query strings distinguish paginated/API reads.
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def classify_fetch_kind(
    *,
    extraction_goal: str = "",
    full: bool = False,
    truncated: bool = False,
    output: str = "",
) -> FetchKind:
    text = str(output or "")
    if truncated or "[partial content:" in text:
        return FetchKind.PARTIAL
    if str(extraction_goal or "").strip() and not full:
        return FetchKind.FOCUSED
    return FetchKind.COMPLETE


def fetch_request_from_tool_content(content: str) -> dict[str, Any]:
    """Best-effort parse of a web_fetch tool argument payload."""
    import json

    raw = str(content or "").strip()
    if not raw:
        return {"url": "", "extraction_goal": "", "full": False, "urls": []}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            url_field = parsed.get("url")
            urls_field = parsed.get("urls")
            urls: list[str] = []
            if isinstance(urls_field, list):
                for item in urls_field:
                    if isinstance(item, dict):
                        value = str(item.get("url") or "").strip()
                    else:
                        value = str(item or "").strip()
                    if value:
                        urls.append(value)
            if isinstance(url_field, list):
                for item in url_field:
                    value = str(item or "").strip()
                    if value:
                        urls.append(value)
            elif url_field:
                urls.append(str(url_field).strip())
            # Preserve order while deduplicating.
            seen: set[str] = set()
            ordered: list[str] = []
            for item in urls:
                key = normalize_fetch_url(item)
                if key and key not in seen:
                    seen.add(key)
                    ordered.append(item.strip())
            return {
                "url": ordered[0] if ordered else "",
                "extraction_goal": str(
                    parsed.get("extraction_goal") or parsed.get("query") or ""
                ).strip(),
                "full": parsed.get("full") is True,
                "urls": ordered,
            }
    return {
        "url": raw.splitlines()[0].strip(),
        "extraction_goal": "",
        "full": False,
        "urls": [],
    }


def request_urls(request: Mapping[str, Any] | None) -> list[str]:
    """Return ordered unique URLs from a parsed web_fetch request."""
    if not isinstance(request, dict):
        return []
    urls = [str(item).strip() for item in (request.get("urls") or []) if str(item).strip()]
    if request.get("url"):
        urls = [str(request.get("url") or "").strip(), *urls]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in urls:
        key = normalize_fetch_url(item)
        if key and key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


@dataclass
class VisitRecord:
    url: str
    kind: FetchKind
    goals: set[str] = field(default_factory=set)
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "kind": self.kind.value,
            "goals": sorted(self.goals),
            "count": self.count,
        }


@dataclass
class WebVisitLedger:
    """In-memory visit state for one agent turn."""

    visits: dict[str, VisitRecord] = field(default_factory=dict)

    def record(
        self,
        url: str,
        kind: FetchKind | str,
        *,
        extraction_goal: str = "",
    ) -> VisitRecord:
        key = normalize_fetch_url(url)
        if not key:
            raise ValueError("url required")
        kind_value = kind if isinstance(kind, FetchKind) else FetchKind(str(kind))
        goal = str(extraction_goal or "").strip().casefold()
        current = self.visits.get(key)
        if current is None:
            current = VisitRecord(url=key, kind=kind_value, goals=set(), count=0)
            self.visits[key] = current
        if _KIND_RANK[kind_value] >= _KIND_RANK[current.kind]:
            current.kind = kind_value
        if goal:
            current.goals.add(goal)
        current.count += 1
        return current

    def lookup(self, url: str) -> Optional[VisitRecord]:
        return self.visits.get(normalize_fetch_url(url))

    def should_suppress(
        self,
        url: str,
        kind: FetchKind | str,
        *,
        extraction_goal: str = "",
    ) -> bool:
        """Return True when the request adds no new evidence class."""
        current = self.lookup(url)
        if current is None:
            return False
        kind_value = kind if isinstance(kind, FetchKind) else FetchKind(str(kind))
        goal = str(extraction_goal or "").strip().casefold()
        # Upgrades always remain available.
        if _KIND_RANK[kind_value] > _KIND_RANK[current.kind]:
            return False
        # A new focused goal is additional evidence even on a completed page.
        if kind_value is FetchKind.FOCUSED and goal and goal not in current.goals:
            return False
        # Identical or weaker re-reads of the same evidence class are duplicates.
        if kind_value is FetchKind.FOCUSED:
            return (not goal) or goal in current.goals
        return True

    def summary_lines(self, *, limit: int = 20) -> list[str]:
        rows = sorted(self.visits.values(), key=lambda item: item.url)
        lines = []
        for record in rows[: max(0, int(limit))]:
            goals = ",".join(sorted(record.goals)[:3])
            suffix = f" goals={goals}" if goals else ""
            lines.append(f"- {record.url} [{record.kind.value}]{suffix}")
        return lines
