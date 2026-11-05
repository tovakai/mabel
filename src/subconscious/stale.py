"""Phase 4 Archivist: stale framing in the notes about Mabel's own stack (Dottie's model).

"Historically correct but misleading as a statement about the current stack."
Scope is deliberately narrow: notes in the project folder about Mabel
(``Projects/gitprojects/Mabel`` in the source vault). Each section is compared
with live facts (``facts.collect``) and Dottie lists statements that are now
misleading: present-tense claims the facts contradict, or unchecked to-dos the
facts show are done.

A suggestion is kept only if its quote really occurs in the note and it cites
facts that exist. Notes are never edited; results are inbox items. Sections
already checked against the same facts are skipped on the next run.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from src.curiosity.verify import norm

from .facts import collect

PROJECT_FOLDER = "Projects/gitprojects/Mabel"
SECTION_CHARS = 5000
MERGE_CHARS = 4000
MIN_CONFIDENCE = 0.6
MAX_PER_SECTION = 5


def sections(text: str) -> list[tuple[int, str]]:
    """(first line number, text) chunks split on headings, each at most SECTION_CHARS."""
    lines = text.splitlines()
    chunks, start, current = [], 1, []
    for number, line in enumerate(lines, 1):
        heading = re.match(r"#{1,3}\s", line)
        if current and (heading or sum(len(x) + 1 for x in current) + len(line) > SECTION_CHARS):
            chunks.append((start, "\n".join(current)))
            start, current = number, []
        current.append(line)
    if current:
        chunks.append((start, "\n".join(current)))
    # Merge neighbouring small sections (fewer model calls, same coverage).
    merged: list[tuple[int, str]] = []
    for number, chunk in chunks:
        if merged and len(merged[-1][1]) + len(chunk) + 1 <= MERGE_CHARS:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n" + chunk)
        else:
            merged.append((number, chunk))
    return [(n, c) for n, c in merged if c.strip() and len(c.strip()) > 80]


_GENERIC = set("""
mabel mabels phase should would could their there these those which where while about after before
implemented implement feat perf fix fixes added adding first other every using being through within
without still never always later today model models chat chats turn turns user users""".split())


def distinctive(text: str) -> set[str]:
    from .archivist import fold
    return {w for w in fold(text).split() if len(w) >= 5 and w not in _GENERIC}


def relevant_facts(facts: list[dict], section: str) -> list[dict]:
    """Basic facts always; git facts only when they share two distinctive words with the section."""
    words = distinctive(section)
    return [f for f in facts if not f["text"].startswith("Implemented (git")
            or len(distinctive(f["text"]) & words) >= 2]


def supports(quote: str, fact_texts: list[str]) -> bool:
    """At least one cited fact must be about the same thing as the quote (two shared distinctive words).
    Checked per fact, so citing a long list of facts can't add up to a match."""
    words = distinctive(quote)
    return any(len(words & distinctive(text)) >= 2 for text in fact_texts)


_PRINCIPLE = re.compile(r"\b(?:must|should|shall)\b", re.I)   # modal rules; "always on" is a claim about state


def _line_of(section: str, first_line: int, quote: str) -> int:
    target = norm(quote)[:60]
    for offset, line in enumerate(section.splitlines()):
        if target and target[:30] in norm(line):
            return first_line + offset
    return first_line


async def run(inbox, *, source_vault: Path, state_path: Path, owner: str = "tovakai", ask=None,
              folder: str = PROJECT_FOLDER, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    facts = collect(owner=owner)
    by_id = {f["id"]: f["text"] for f in facts}
    # Facts without the date line, so a new day alone doesn't re-check everything.
    facts_hash = hashlib.sha256("\n".join(f["text"] for f in facts[1:]).encode()).hexdigest()[:16]
    try:
        seen = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        seen = {}
    root = Path(source_vault) / folder
    checked = skipped = 0
    kept, dropped = [], 0
    for path in sorted(root.glob("*.md")):
        rel = str(path.relative_to(source_vault))
        text = path.read_text(encoding="utf-8")
        written = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        for first_line, section in sections(text):
            digest = hashlib.sha256(f"{rel}\n{section}".encode()).hexdigest()[:16]
            if seen.get(digest) == facts_hash:
                skipped += 1
                continue
            checked += 1
            shown = relevant_facts(facts, section)
            fact_ids = {f["id"] for f in shown}
            facts_text = "\n".join(f"{f['id']}: {f['text']}" for f in shown)
            prompt = (
                f"Facts about Mabel's current stack, gathered live today:\n{facts_text}\n\n"
                f"A section of the note {rel} (last edited {written}):\n<<<\n{section}\n>>>\n\n"
                "List statements in this section that are now MISLEADING about the current stack: present-tense "
                "claims that the facts contradict, or unchecked to-dos (\"- [ ]\") that the facts clearly show are "
                "done. Plans, history, reasoning, and rules or principles (\"must\", \"should\", \"never\") are "
                "fine: never flag them. Only flag something a specific fact clearly contradicts, and cite at most "
                "the one to three facts that do. If a to-do is only partly done, say \"partly done\" in why and put "
                "what is left in the suggestion. "
                "If nothing qualifies, return an empty list.\n"
                'Reply with only JSON: {"stale": [{"quote": "exact sentence copied from the section", '
                '"facts": ["F12"], "why": "one sentence", "suggestion": "how to reframe it, e.g. mark done or '
                'say as of when", "confidence": 0.0}]}')
            try:
                reply = await (ask or _ask_dottie)(prompt, owner)
            except Exception:
                continue                           # retried next run (not marked seen)
            for hit in _parse(reply)[:MAX_PER_SECTION]:
                quote = " ".join(str(hit.get("quote") or "").split())
                cited = [f for f in dict.fromkeys(str(x) for x in hit.get("facts") or []) if f in fact_ids]
                if len(cited) > 3:
                    dropped += 1          # a shotgun citation is not evidence
                    continue
                try:
                    confidence = max(0.0, min(1.0, float(hit.get("confidence") or 0)))
                except (TypeError, ValueError):
                    confidence = 0.0
                fact_lines = [by_id[fid] for fid in cited]
                is_todo = re.match(r"^[-*]\s*\[ \]", quote) is not None
                if (len(norm(quote)) < 15 or norm(quote) not in norm(section) or not cited
                        or (_PRINCIPLE.search(quote) and not is_todo)
                        or confidence < MIN_CONFIDENCE or not supports(quote, fact_lines)):
                    dropped += 1
                    continue
                line = _line_of(section, first_line, quote)
                item, outcome = inbox.add(
                    kind="stale_framing", loop="archivist", title=quote[:160],
                    summary=" ".join(str(hit.get("why") or "").split())[:400],
                    dedupe_key=f"stale:{rel}:{hashlib.sha256(norm(quote).encode()).hexdigest()[:16]}",
                    evidence=[{"ref": f"{rel}#L{line}", "kind": "note", "context": quote[:200]}]
                             + [{"ref": f"fact:{fid}", "kind": "fact", "context": text[:200]}
                                for fid, text in zip(cited, fact_lines)],
                    verification="source_checked", confidence=confidence, priority=int(35 + 30 * confidence),
                    days=30, payload={"suggestion": " ".join(str(hit.get("suggestion") or "").split())[:300],
                                      "note": rel, "line": line}, now=now)
                kept.append({"note": rel, "line": line, "quote": quote[:100], "outcome": outcome})
            seen[digest] = facts_hash
            Path(state_path).parent.mkdir(parents=True, exist_ok=True)
            Path(state_path).write_text(json.dumps(seen))   # per section: an interrupted run resumes
    return {"sections_checked": checked, "sections_skipped": skipped, "kept": kept, "dropped": dropped}


async def _ask_dottie(prompt: str, owner: str) -> str:
    from src import model_roles
    return await model_roles.call("research", [{"role": "user", "content": prompt}], owner=owner,
                                  max_tokens=1500, thinking_mode="off", temperature=0.1, timeout=120)


def _parse(reply: str) -> list[dict]:
    text = re.sub(r"(?s)<think>.*?</think>", "", str(reply or ""))
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    items = data.get("stale") if isinstance(data, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
