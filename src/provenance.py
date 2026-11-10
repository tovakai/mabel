"""Roadmap 2.0, Phase 2.0: where every thought came from.

One vocabulary for everything Mabel carries (memories, whispers, research
questions, inbox items, her recent lines):

- ``experienced``: something that happened to her: a conversation with Anthon,
  research she did, something she wrote.
- ``told``: someone said it: Anthon in chat (when it's about the world or himself)
  or Tova in a letter. Their word, attributed to them, not a fact.
- ``observed``: she saw it in the world: the life log, Home Assistant, messages,
  the news.
- ``inferred``: her little brain (Dottie's loops) guessed it from patterns. A
  hypothesis until something told or observed confirms it.
- ``reflected``: her own reflections and takes.
- ``dreamt``: from her dreams.

The hard rule: ``reflected`` and ``dreamt`` material never becomes a factual
memory. Existing stores keep their own fields; the ``of_*`` functions map
those fields onto this vocabulary, so nothing needs migrating.
"""
from __future__ import annotations

TYPES = ("experienced", "told", "observed", "inferred", "reflected", "dreamt")
NEVER_FACTUAL = frozenset({"reflected", "dreamt"})

# Memory candidates: their ``origin`` (who proposed it) decides.
_CANDIDATE_ORIGINS = {
    "completed_persisted_turn": "experienced",
    "daily_life_account": "observed",
    "letter_from_tova": "told",
    "self_reflection": "reflected",
    "dream": "dreamt",
}
_CANDIDATE_KINDS = {"conversation": "experienced", "tool_observation": "observed",
                    "ledger_event": "observed", "user_seeded": "told"}

# Whispers: their ``source`` tag (src/persona/whisper.py SOURCES).
_WHISPER_SOURCES = {"manual": "told", "research": "experienced", "calendar": "observed", "mail": "observed",
                    "conversation": "inferred", "life": "observed", "self": "reflected", "news": "observed"}

# Dottie's inbox: the loop that filed it.
_INBOX_LOOPS = {"archivist": "inferred", "dreamer": "inferred", "scout": "observed", "sentinel": "inferred"}


def of_candidate(*, origin: str = "", source_kind: str = "", source_ref: str = "") -> str:
    if origin in _CANDIDATE_ORIGINS:
        return _CANDIDATE_ORIGINS[origin]
    if source_ref.startswith("letter:"):
        return "told"
    if source_ref.startswith("lifelog:"):
        return "observed"
    return _CANDIDATE_KINDS.get(source_kind, "inferred")


def of_whisper(source: str) -> str:
    return _WHISPER_SOURCES.get(source, "reflected")


def of_inbox(loop: str) -> str:
    return _INBOX_LOOPS.get(loop, "inferred")


_BACKLOG_FOLDERS = (("journal/self/", "reflected"), ("journal/dreams/", "dreamt"), ("journal/anthon/", "observed"),
                    ("journal/news/", "observed"), ("journal/letters/", "told"), ("knowledge/research/", "experienced"))


def of_backlog(entry: dict) -> str:
    """Research questions: by the note they came from (``from_article``)."""
    if entry.get("origin") == "user_seeded":
        return "told"
    source = str(entry.get("from_article") or "")
    for folder, kind in _BACKLOG_FOLDERS:
        if source.startswith(folder):
            return kind
    return "reflected"


def can_be_fact(provenance: str) -> bool:
    return provenance not in NEVER_FACTUAL
