"""Phase 3: research backlog (open questions, spawned links) and article registry.

State lives in ``data/curiosity/backlog.json`` (local). Articles live in
Mabel's vault under ``knowledge/research/`` and are the source of truth for
"already researched"; the backlog only remembers what is still open.
New branches cool off before they can be picked, so research does not
immediately recurse into its own output.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

COOL_OFF = timedelta(hours=20)
MAX_OPEN_QUESTIONS = 40
MAX_SPAWNED = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Backlog:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                for key in ("questions", "spawned", "sessions"):
                    data.setdefault(key, [])
                return data
        except (OSError, ValueError):
            pass
        return {"questions": [], "spawned": [], "sessions": []}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".backlog-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    def eligible_questions(self, now: datetime | None = None) -> list[dict]:
        now = (now or _now()).isoformat()
        return [q for q in self.load()["questions"] if q["status"] == "open" and q["eligible_after"] <= now]

    def eligible_spawned(self, now: datetime | None = None) -> list[dict]:
        now = (now or _now()).isoformat()
        return [s for s in self.load()["spawned"] if s["status"] == "open" and s["eligible_after"] <= now]

    def add_from_article(self, *, article: str, subject: str, questions: list[str], links: list[str],
                         now: datetime | None = None) -> dict:
        now = now or _now()
        after = (now + COOL_OFF).isoformat()
        data = self.load()
        known_q = {q["question"].casefold() for q in data["questions"]}
        known_s = {s["name"].casefold() for s in data["spawned"]}
        added = {"questions": 0, "spawned": 0}
        for question in questions:
            if question.casefold() in known_q:
                continue
            data["questions"].append({"id": f"q{secrets.token_hex(4)}", "question": question, "subject": subject,
                                      "from_article": article, "origin": "self_generated",
                                      "created_at": now.isoformat(), "eligible_after": after, "status": "open"})
            added["questions"] += 1
        for link in links:
            if link.casefold() in known_s:
                continue
            data["spawned"].append({"id": f"l{secrets.token_hex(4)}", "name": link, "from_article": article,
                                    "created_at": now.isoformat(), "eligible_after": after, "status": "open"})
            added["spawned"] += 1
        data["questions"] = data["questions"][-MAX_OPEN_QUESTIONS:]
        data["spawned"] = data["spawned"][-MAX_SPAWNED:]
        self.save(data)
        return added

    def mark(self, ref: str, status: str) -> None:
        data = self.load()
        for item in data["questions"] + data["spawned"]:
            if item["id"] == ref:
                item["status"] = status
        self.save(data)

    def record_session(self, entry: dict) -> None:
        data = self.load()
        data["sessions"] = [s for s in data["sessions"] if s["id"] != entry["id"]][-49:] + [entry]
        self.save(data)


_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.S)


def articles(folder: Path) -> list[dict]:
    """Mabel's existing research articles (frontmatter only)."""
    out = []
    for path in sorted(Path(folder).glob("*.md")):
        try:
            match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
            meta = yaml.safe_load(match.group(1)) if match else None
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if isinstance(meta, dict) and meta.get("type") == "research-article":
            out.append({"path": path, **meta})
    return out


def researched_subjects(folder: Path) -> set[str]:
    subjects = set()
    for article in articles(folder):
        subjects.add(str(article.get("subject") or article["path"].stem).casefold())
        subjects.update(str(a).casefold() for a in article.get("aliases") or [])
    return subjects
