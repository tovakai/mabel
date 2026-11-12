"""What the user's own media library says about a subject: has he read/watched/played it, and his rating.

His vault keeps one note per work under ``Library/<Anime|Manga|Movies|Series|Games>`` with frontmatter
(Name, Type, Year, Progress, Complete, My Rating, Creator). Outreach used to ask "have you read it?"
about a manga he had finished and rated 10/10; these facts let Mabel know before she writes.

Read-only, local only. Matches are deliberately narrow: a work's full name in the subject text, or one of
its creators (e.g. the author) named there.
"""
from __future__ import annotations

import re
from pathlib import Path

FOLDERS = ("Anime", "Manga", "Movies", "Series", "Games")
MAX_FACTS = 6
MIN_NAME = 4          # shorter names ("Up", "Her") match too much ordinary text
MIN_CREATOR = 5
_LINK = re.compile(r"^\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]$")


def _frontmatter(path: Path) -> dict:
    import yaml
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return {}
        head = text.split("\n---", 2)[0].removeprefix("---")
        meta = yaml.safe_load(head)
        return meta if isinstance(meta, dict) else {}
    except Exception:
        return {}


def _names(value) -> list[str]:
    items = value if isinstance(value, list) else [value] if value else []
    out = []
    for item in items:
        text = str(item).strip()
        m = _LINK.match(text)
        out.append((m.group(1) if m else text).strip())
    return [n for n in out if n]


def entries(vault: Path | None) -> list[dict]:
    if not vault:
        return []
    out = []
    for folder in FOLDERS:
        for path in sorted((Path(vault) / "Library" / folder).glob("*.md")):
            meta = _frontmatter(path)
            out.append({"name": str(meta.get("Name") or path.stem), "folder": folder,
                        "type": str(meta.get("Type") or folder), "year": meta.get("Year"),
                        "rating": meta.get("My Rating"), "progress": meta.get("Progress"),
                        "complete": meta.get("Complete"), "creators": _names(meta.get("Creator"))})
    return out


def _has(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(phrase.casefold()) + r"(?!\w)", text))


def matches(text: str, library: list[dict]) -> list[dict]:
    folded = " ".join(str(text or "").split()).casefold()
    if not folded:
        return []
    hits = [e for e in library
            if (len(e["name"]) >= MIN_NAME and _has(folded, e["name"]))
            or any(len(c) >= MIN_CREATOR and _has(folded, c) for c in e["creators"])]
    hits.sort(key=lambda e: -_number(e["rating"]))
    return hits[:MAX_FACTS]


def _number(value) -> float:
    try:
        return float(str(value).split("/")[0])
    except (TypeError, ValueError):
        return -1.0


def _finished(e: dict) -> bool | None:
    if e.get("complete") is True:
        return True
    m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(e.get("progress") or ""))
    if m:
        return int(m.group(1)) >= int(m.group(2)) > 0
    return None


def fact(e: dict) -> str:
    what = e["type"].lower() if e["type"].lower() not in {"comic"} else "manga"
    year = f", {e['year']}" if e.get("year") else ""
    bits = []
    done = _finished(e)
    if done is True:
        bits.append("finished")
    elif e.get("progress"):
        bits.append(f"progress {e['progress']}")
    rating = _number(e.get("rating"))
    bits.append(f"he rated it {e['rating']}/10" if rating >= 0 else "not rated")
    return f"{e['name']} ({what}{year}): " + ", ".join(bits)


def facts_for(text: str, vault: Path | None, library: list[dict] | None = None) -> list[str]:
    return [fact(e) for e in matches(text, entries(vault) if library is None else library)]


def default_vault() -> Path | None:
    try:
        from src.agent_runtime.vault_target import persisted_vault_path
        path = persisted_vault_path()
        return Path(path) if path and Path(path, "Library").is_dir() else None
    except Exception:
        return None
