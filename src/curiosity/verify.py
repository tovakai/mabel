"""Phase 3: parse a drafted article and verify its citations against fetched sources.

The model cites sources as [S1]; after the article it lists short verbatim
quotes per source. A cited source counts as verified only when at least one
of its quotes really occurs in the fetched text (whitespace, case, quote
styles, dashes and other punctuation ignored). Unverified citations are marked ``[S2?]`` and
the article is flagged ``needs_review`` rather than silently trusted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SECTION_FOUND = "What I found"
SECTION_TAKE = "What I make of it"
SECTION_QUESTIONS = "Open questions"
SECTION_LINKS = "Links"
MAX_LINKS = 4
MAX_QUESTIONS = 3
MIN_QUOTE_CHARS = 12
_CITE = re.compile(r"\[(S\d{1,2})\]")


def norm(text: str) -> str:
    text = str(text or "").lower()
    text = text.translate(str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                         "–": "-", "—": "-", " ": " "}))
    text = re.sub(r"\[\d+\]", "", text)          # wiki footnote markers
    text = re.sub(r"(?<=\w)'(?=\w)", "", text)    # don't -> dont, Kondo's -> Kondos
    return " ".join(re.sub(r"[\W_]+", " ", text).split())   # same words, in order, punctuation ignored


@dataclass
class Draft:
    title: str
    body: str
    found: str
    questions: list[str]
    links: list[str]
    quotes: dict[str, list[str]]
    whisper: str
    problems: list[str] = field(default_factory=list)


def _section(body: str, name: str) -> str:
    match = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S | re.I)
    return match.group(1).strip() if match else ""


def salvage_quotes(fragment: str) -> dict[str, list[str]]:
    """Complete ``"S1": ["..", ".."]`` entries from a possibly cut-off JSON quote block."""
    out: dict[str, list[str]] = {}
    for sid, body in re.findall(r'"(S\d{1,2})"\s*:\s*\[(.*?)(?:\]|$)', fragment, re.S):
        quotes = [json.loads(f'"{q}"') for q in re.findall(r'"((?:[^"\\]|\\.)*)"', body)]
        if quotes:
            out[sid.upper()] = quotes[:4]
    return out


def parse_draft(text: str) -> Draft:
    text = re.sub(r"(?s)<think>.*?</think>", "", text or "").strip()
    problems = []
    meta = {}
    block = None
    for candidate in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        block = candidate
    if block:
        try:
            meta = json.loads(block.group(1))
        except ValueError:
            problems.append("quote_block_unparseable")
        text = (text[:block.start()] + text[block.end():]).strip()
    else:
        cut = re.search(r"```(?:json)?\s*\{", text)
        if cut:   # the reply ran out mid-block: salvage complete quotes, drop the fragment
            meta = {"quotes": salvage_quotes(text[cut.start():])}
            text = text[:cut.start()].strip()
            problems.append("quote_block_truncated")
        else:
            problems.append("no_quote_block")
    title_match = re.search(r"^#\s+(.+)$", text, re.M)
    title = title_match.group(1).strip() if title_match else ""
    body = text[title_match.start():].strip() if title_match else text
    if not title:
        problems.append("no_title")
    found = _section(body, SECTION_FOUND)
    if not found:
        problems.append("no_found_section")
    questions = [re.sub(r"^[-*\d.)\s]+", "", line).strip()
                 for line in _section(body, SECTION_QUESTIONS).splitlines() if line.strip()][:MAX_QUESTIONS]
    links = []
    for name in re.findall(r"\[\[([^\]|#]+)", _section(body, SECTION_LINKS)):
        if name.strip() and name.strip() not in links:
            links.append(name.strip())
    quotes = meta.get("quotes") if isinstance(meta.get("quotes"), dict) else {}
    quotes = {str(k).upper(): [str(q) for q in (v if isinstance(v, list) else [v])][:4] for k, v in quotes.items()}
    return Draft(title, body, found, questions, links[:MAX_LINKS], quotes,
                 str(meta.get("whisper") or "").strip()[:400], problems)


def verify(draft: Draft, sources: dict[str, str]) -> dict:
    """Mutates ``draft.body`` to mark unverified citations; returns the verification record."""
    normalized = {sid: norm(text) for sid, text in sources.items()}
    cited = sorted(set(_CITE.findall(draft.found)), key=lambda s: int(s[1:]))
    verified, quotes_ok = [], {}
    for sid in cited:
        good = [q for q in draft.quotes.get(sid, [])
                if len(norm(q)) >= MIN_QUOTE_CHARS and sid in normalized and norm(q) in normalized[sid]]
        if good:
            verified.append(sid)
            quotes_ok[sid] = good
    unknown = [sid for sid in cited if sid not in sources]
    unverified = [sid for sid in cited if sid not in verified]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", draft.found)) if len(s.split()) >= 6]
    uncited = [s for s in sentences if not _CITE.search(s)]
    for sid in unverified:
        draft.body = draft.body.replace(f"[{sid}]", f"[{sid}?]")
    problems = list(draft.problems)
    if unverified:
        problems.append("unverified_citations")
    if unknown:
        problems.append("unknown_sources")
    if len(uncited) > 2:
        problems.append("uncited_claims")
    if not verified:
        problems.append("nothing_verified")
    return {"cited": cited, "verified": verified, "unverified": unverified, "unknown": unknown,
            "uncited_sentences": len(uncited), "quotes": quotes_ok, "problems": problems,
            "status": "verified" if not problems else "needs_review"}
