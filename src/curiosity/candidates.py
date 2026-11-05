"""Phase 3: unresolved-link audit and research candidates, read from the existing vault index.

The knowledge index already resolves every [[link]]; this module only reads it
(SQLite read-only), so there is no second index to keep in sync. Unresolved
links are research *opportunities*, filtered so that maintenance noise and
private material never become research subjects:

- attachments, Obsidian Bases (``*.base``), template placeholders and
  folder/path links are maintenance, not curiosity;
- links found in private notes (any path segment containing "private"), daily
  notes, and the user's own project notes are skipped entirely;
- Genre and Cast properties are import metadata (encyclopedia-stub bait).

Scoring follows what the user cares about: links from notes they rated highly
count more than links from unrated imports, and links written in prose count
more than metadata properties.
"""
from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ATTACHMENT = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|pdf|mp3|mp4|webm|epub|canvas|base)$", re.I)
SKIP_PROPERTIES = {"genre", "cast", "type", "previous_phase", "roadmap"}
PRIVATE_SEGMENT = re.compile(r"private", re.I)
SKIP_ROOTS = ("daily life of anthon/", "projects/")
RATING_KEY = "My Rating"
METADATA_LINE = re.compile(r"^\s*(?:[-*>]\s*)*(?:\*\*)?[\w ]{1,30}(?:\*\*)?\s*:\s*(?:\*\*)?\s*\[\[")


@dataclass
class Candidate:
    name: str
    kind: str                      # "graph" | "surprise" | "spawned" | "open_question"
    score: float = 0.0
    occurrences: int = 0
    sources: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    question: str | None = None    # for open questions
    ref: str | None = None         # backlog id for open questions / spawned links
    prose: bool = False            # linked in written text at least once (not only metadata)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "", False)}


def index_db(vault_root: Path, owner: str) -> Path:
    from src.knowledge_engine.index import digest, local_cache
    root = Path(vault_root).expanduser().resolve()
    return Path(local_cache()).expanduser().resolve() / digest(owner + "\0" + str(root)) / "vault_index.db"


def _connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"no vault index at {path}; open the vault in Mabel once to build it")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def normalize_target(target: str) -> str:
    return re.sub(r"\.md$", "", str(target).split("#", 1)[0].split("|", 1)[0].strip(), flags=re.I)


def is_private_path(path: str) -> bool:
    folded = path.casefold()
    return bool(PRIVATE_SEGMENT.search(path)) or folded.startswith(SKIP_ROOTS)


def _rating(value: str | None) -> float | None:
    try:
        number = float(str(value).split("/")[0])
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 10 else None


def audit(db_path: Path) -> dict:
    """Counts for the unresolved-link audit (occurrences vs distinct entities, and why skipped)."""
    db = _connect(db_path)
    rows = db.execute("""SELECT e.target, e.property_key, e.resolution, n.path FROM edges e
                         JOIN notes n ON n.id = e.source_id WHERE e.resolution = 'unresolved'""").fetchall()
    skipped = defaultdict(int)
    entities: dict[str, int] = defaultdict(int)
    for row in rows:
        reason = _skip_reason(row)
        if reason:
            skipped[reason] += 1
            continue
        entities[normalize_target(row["target"]).casefold()] += 1
    return {
        "notes": db.execute("SELECT count(*) FROM notes").fetchone()[0],
        "unresolved_occurrences": len(rows),
        "ambiguous_occurrences": db.execute("SELECT count(*) FROM edges WHERE resolution='ambiguous'").fetchone()[0],
        "skipped": dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
        "research_occurrences": sum(entities.values()),
        "research_entities": len(entities),
        "research_singletons": sum(1 for n in entities.values() if n == 1),
    }


def _skip_reason(row) -> str | None:
    target = normalize_target(row["target"])
    key = (row["property_key"] or "").casefold()
    if not target:
        return "empty"
    if ATTACHMENT.search(target):
        return "attachment_or_base"
    if "{{" in target:
        return "template_placeholder"
    if "/" in target:
        return "path_link"
    if is_private_path(row["path"]):
        return "private_or_own_project_source"
    if key in SKIP_PROPERTIES:
        return f"property_{key}"
    return None


def candidates(db_path: Path, *, exclude: set[str] = frozenset(), top: int = 5, prose: int = 3,
               surprises: int = 2, seed: int | None = None) -> list[Candidate]:
    """Top graph-relevant candidates, the best links written in prose, then a couple of surprises."""
    db = _connect(db_path)
    titles = {r[0].casefold() for r in db.execute("SELECT title FROM notes")}
    for (aliases,) in db.execute("SELECT aliases FROM notes"):
        try:
            titles.update(str(a).casefold() for a in json.loads(aliases or "[]"))
        except ValueError:
            pass
    ratings = {r["note_id"]: _rating(r["value"]) for r in
               db.execute("SELECT note_id, value FROM properties WHERE key = ?", (RATING_KEY,))}
    rows = db.execute("""SELECT e.target, e.property_key, e.context, e.source_id, n.path, n.title FROM edges e
                         JOIN notes n ON n.id = e.source_id WHERE e.resolution = 'unresolved'""").fetchall()
    pool: dict[str, Candidate] = {}
    for row in rows:
        if _skip_reason(row):
            continue
        name = normalize_target(row["target"])
        key = name.casefold()
        if key in titles or key in exclude:
            continue
        cand = pool.setdefault(key, Candidate(name, "graph"))
        cand.occurrences += 1
        cand.prose = cand.prose or (not row["property_key"] and not METADATA_LINE.match(str(row["context"] or ""))
                                    and len(str(row["context"] or "").split()) >= 8)
        if row["path"] not in cand.sources:
            cand.sources.append(row["path"])
            rating = ratings.get(row["source_id"])
            weight = (rating / 10 if rating is not None else 0.4) * (1.5 if not row["property_key"] else 1.0)
            cand.score += weight
            if rating is not None and rating >= 8:
                cand.reasons.append(f"linked from {row['title']} (you rated it {rating:g}/10)")
        context = " ".join(str(row["context"] or "").split())[:220]
        if context and len(cand.contexts) < 3 and context not in cand.contexts:
            cand.contexts.append(context)
    for cand in pool.values():
        cand.score = round(cand.score + 0.3 * math.log1p(cand.occurrences), 3)
        if len(cand.sources) > 1:
            cand.reasons.insert(0, f"linked from {len(cand.sources)} notes")
        cand.reasons = cand.reasons[:3]
    ranked = sorted(pool.values(), key=lambda c: (-c.score, c.name.casefold()))
    chosen = ranked[:top]
    chosen += [c for c in ranked if c.prose and c not in chosen][:prose]
    rest = [c for c in ranked[20:] if c not in chosen]
    rng = random.Random(seed)
    for cand in rng.sample(rest, min(surprises, len(rest))):
        cand.kind = "surprise"
        cand.reasons = ["a lower-ranked link picked at random"] + cand.reasons[:1]
        chosen.append(cand)
    return chosen


def vault_context(db_path: Path, subject: str, *, limit: int = 5, chars: int = 700,
                  allow: tuple[str, ...] = ()) -> list[dict]:
    """Lexical vault context for ``subject`` (non-private notes only, plus explicitly ``allow``-ed folders)."""
    db = _connect(db_path)
    words = [w for w in re.findall(r"\w+", subject) if len(w) > 1]
    if not words:
        return []
    phrase = '"' + " ".join(words).replace('"', "") + '"'
    out = []
    try:
        rows = db.execute("""SELECT n.path, c.heading, c.text FROM chunks_fts f
                             JOIN chunks c ON c.id = f.chunk_id JOIN notes n ON n.id = c.note_id
                             WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?""",
                          (phrase, limit * 3)).fetchall()
    except sqlite3.OperationalError:
        return []
    for row in rows:
        allowed = any(row["path"].casefold().startswith(prefix.casefold()) for prefix in allow)
        if (is_private_path(row["path"]) and not allowed) or any(o["path"] == row["path"] for o in out):
            continue
        out.append({"path": row["path"], "heading": row["heading"], "text": " ".join(row["text"].split())[:chars]})
        if len(out) >= limit:
            break
    return out


def existing_titles(db_path: Path) -> set[str]:
    """Casefolded note titles and aliases in the source vault."""
    db = _connect(db_path)
    titles = {r[0].casefold() for r in db.execute("SELECT title FROM notes")}
    for (aliases,) in db.execute("SELECT aliases FROM notes"):
        try:
            titles.update(str(a).casefold() for a in json.loads(aliases or "[]"))
        except ValueError:
            pass
    return titles
