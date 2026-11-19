"""The Obsidian vault for the VAULT channel: notes, links, tags, headings, backlinks, search, rename.

Read straight from disk (his PC's vault, or the deck's Syncthing copy). Our own code, inspired by
basalt's feature set (GPL app, so no code is borrowed); adds backlinks, tags and daily notes.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

WIKILINK = re.compile(r"(!?)\[\[([^\]\|#]*)(#[^\]\|]*)?(?:\|([^\]]*))?\]\]")
INLINE_TAG = re.compile(r"(?<![\w/#&])#([A-Za-zÅÄÖåäöÆæØøÜü][\w/\-ÅÄÖåäö]*)")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^(```|~~~)")


@dataclass
class Note:
    path: Path                      # absolute
    rel: str                        # vault-relative, with .md
    name: str                       # file stem (what [[links]] use)
    mtime: float = 0.0
    frontmatter: dict = field(default_factory=dict)
    links: list[str] = field(default_factory=list)       # raw link targets (names or paths), no .md
    tags: set[str] = field(default_factory=set)
    headings: list[tuple[int, str, int]] = field(default_factory=list)   # (level, text, line)


def split_frontmatter(text: str) -> tuple[dict, str, int]:
    """(frontmatter, body, number of lines the frontmatter took)."""
    if not text.startswith("---\n"):
        return {}, text, 0
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text, 0
    head = text[4:end]
    rest = text[end + 4:]
    rest = rest[1:] if rest.startswith("\n") else rest
    try:
        data = yaml.safe_load(head)
    except yaml.YAMLError:
        data = None
    # The opening ---, the head's lines and the closing ---: the body starts after them.
    return (data if isinstance(data, dict) else {}), rest, head.count("\n") + 3


def scan_text(text: str) -> tuple[dict, list[str], set[str], list[tuple[int, str, int]]]:
    fm, body, offset = split_frontmatter(text)
    links, tags, heads, fenced = [], set(), [], False
    for n, line in enumerate(body.splitlines()):
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if fenced:
            continue
        for m in WIKILINK.finditer(line):
            if m.group(2).strip():
                links.append(m.group(2).strip())
        for t in INLINE_TAG.findall(line):
            tags.add(t)
        h = HEADING.match(line)
        if h:
            heads.append((len(h.group(1)), h.group(2), n + offset))
    raw = fm.get("tags") or fm.get("tag") or []
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    for t in raw if isinstance(raw, list) else []:
        if t:
            tags.add(str(t).lstrip("#"))
    return fm, links, tags, heads


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.notes: dict[str, Note] = {}         # rel → Note
        self.by_name: dict[str, list[Note]] = {}

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    # ── scanning ────────────────────────────────────────────────────────

    def scan(self) -> "Vault":
        notes = {}
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for f in files:
                if f.endswith(".md") and not f.startswith("."):
                    p = Path(dirpath) / f
                    notes[str(p.relative_to(self.root))] = self._read(p)
        self.notes = notes
        self._index()
        return self

    def _read(self, p: Path) -> Note:
        rel = str(p.relative_to(self.root))
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            mtime = p.stat().st_mtime
        except OSError:
            text, mtime = "", 0.0
        fm, links, tags, heads = scan_text(text)
        return Note(p, rel, p.stem, mtime, fm, links, tags, heads)

    def _index(self) -> None:
        self.by_name = {}
        for note in self.notes.values():
            self.by_name.setdefault(note.name.lower(), []).append(note)

    def refresh_note(self, rel: str) -> Note | None:
        p = self.root / rel
        if not p.exists():
            self.notes.pop(rel, None)
            self._index()
            return None
        self.notes[rel] = self._read(p)
        self._index()
        return self.notes[rel]

    # ── links ───────────────────────────────────────────────────────────

    def resolve(self, target: str, source: Note | None = None) -> Note | None:
        """A [[target]] → note: by path if it has a slash, else by name (closest to the source first)."""
        t = target.strip().removesuffix(".md")
        if not t:
            return source
        if "/" in t:
            direct = self.notes.get(t + ".md")
            if direct:
                return direct
            t = t.rsplit("/", 1)[-1]
        found = self.by_name.get(t.lower(), [])
        if len(found) > 1 and source:
            here = str(Path(source.rel).parent)
            found = sorted(found, key=lambda n: (str(Path(n.rel).parent) != here, len(n.rel)))
        return found[0] if found else None

    def backlinks(self, note: Note) -> list[Note]:
        out = []
        for other in self.notes.values():
            if other is note:
                continue
            if any(self.resolve(t, other) is note for t in other.links if t.lower().removesuffix(".md").rsplit("/", 1)[-1] == note.name.lower()):
                out.append(other)
        return sorted(out, key=lambda n: n.rel.lower())

    def tags(self) -> dict[str, list[Note]]:
        out: dict[str, list[Note]] = {}
        for note in self.notes.values():
            for t in note.tags:
                out.setdefault(t, []).append(note)
        return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())))

    # ── search ──────────────────────────────────────────────────────────

    def find_notes(self, query: str, limit: int = 50) -> list[Note]:
        """Fuzzy match on note names and paths (the [[ picker and quick open)."""
        q = query.lower().strip()
        if not q:
            return sorted(self.notes.values(), key=lambda n: -n.mtime)[:limit]

        def score(n: Note):
            name, rel = n.name.lower(), n.rel.lower()
            if name == q:
                return 0
            if name.startswith(q):
                return 1
            if q in name:
                return 2
            if q in rel:
                return 3
            it = iter(name)
            return 4 if all(c in it for c in q) else None
        scored = [(s, n) for n in self.notes.values() if (s := score(n)) is not None]
        return [n for s, n in sorted(scored, key=lambda x: (x[0], len(x[1].name)))][:limit]

    def search(self, query: str, limit: int = 100) -> list[tuple[Note, int, str]]:
        """Lines containing every word of the query: (note, line number, the line)."""
        words = [w.lower() for w in query.split() if w]
        if not words:
            return []
        hits = []
        for note in sorted(self.notes.values(), key=lambda n: -n.mtime):
            try:
                lines = note.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for n, line in enumerate(lines):
                low = line.lower()
                if all(w in low for w in words):
                    hits.append((note, n, line.strip()))
                    if len(hits) >= limit:
                        return hits
        return hits

    # ── files ───────────────────────────────────────────────────────────

    def read(self, note: Note) -> str:
        return note.path.read_text(encoding="utf-8", errors="replace")

    def write(self, note: Note, text: str) -> Note:
        tmp = note.path.with_name("." + note.path.name + ".mabel-tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, note.path)
        return self.refresh_note(note.rel)

    def create(self, rel: str, text: str = "") -> Note:
        rel = rel if rel.endswith(".md") else rel + ".md"
        p = self.root / rel
        if self.root.resolve() not in p.resolve().parents:
            raise ValueError("outside the vault")
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_text(text, encoding="utf-8")
        return self.refresh_note(rel)

    def rename(self, note: Note, new_name: str) -> tuple[Note, int]:
        """Rename in place and rewrite [[links]] to it across the vault (like Obsidian's 'always update links')."""
        new_name = new_name.strip().removesuffix(".md")
        if not new_name or "/" in new_name:
            raise ValueError("a plain name, no folders")
        new_path = note.path.with_name(new_name + ".md")
        if new_path.exists():
            raise ValueError(f"{new_name}.md already exists here")
        old_name, old_rel = note.name, note.rel
        changed = 0
        for other in list(self.notes.values()):
            if not any(t.lower().rsplit("/", 1)[-1] == old_name.lower() for t in other.links):
                continue
            text = self.read(other)

            def swap(m):
                bang, target, head, alias = m.group(1), m.group(2), m.group(3) or "", m.group(4)
                if self.resolve(target, other) is not note:
                    return m.group(0)
                folder = target.rsplit("/", 1)[0] + "/" if "/" in target else ""
                return f"{bang}[[{folder}{new_name}{head}{'|' + alias if alias is not None else ''}]]"
            new_text = WIKILINK.sub(swap, text)
            if new_text != text:
                self.write(other, new_text)
                changed += 1
        note.path.rename(new_path)
        self.notes.pop(old_rel, None)
        new_rel = str(new_path.relative_to(self.root))
        self.notes[new_rel] = self._read(new_path)
        self._index()
        return self.notes[new_rel], changed

    # ── daily note ──────────────────────────────────────────────────────

    def daily_settings(self) -> dict:
        try:
            return json.loads((self.root / ".obsidian" / "daily-notes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def daily(self, day: date | None = None) -> Note:
        """Today's (or ``day``'s) daily note, created from the vault's template if missing."""
        day = day or date.today()
        s = self.daily_settings()
        fmt = s.get("format") or "YYYY-MM-DD"
        rel = f"{(s.get('folder') or '').strip('/')}/{moment(day, fmt)}.md".lstrip("/")
        if rel in self.notes:
            return self.notes[rel]
        template = ""
        if s.get("template"):
            t = self.root / (s["template"] if s["template"].endswith(".md") else s["template"] + ".md")
            try:
                template = fill_template(t.read_text(encoding="utf-8"), day, Path(rel).stem, fmt)
            except OSError:
                template = ""
        return self.create(rel, template)


# ── Obsidian/moment.js date formats ─────────────────────────────────────

_MOMENT = [("YYYY", "%Y"), ("YY", "%y"), ("MMMM", "%B"), ("MMM", "%b"), ("MM", "%m"), ("M", "{M}"),
           ("DDDD", "%j"), ("Do", "{Do}"), ("DD", "%d"), ("D", "{D}"), ("dddd", "%A"), ("ddd", "%a"),
           ("HH", "%H"), ("H", "{H}"), ("hh", "%I"), ("mm", "%M"), ("ss", "%S"), ("A", "%p"), ("ww", "{ww}"),
           ("w", "{w}")]


def moment(when: date | datetime, fmt: str) -> str:
    """Format a date with moment.js tokens (what Obsidian uses), e.g. 'dddd, MMMM Do, YYYY'."""
    out, i = "", 0
    while i < len(fmt):
        if fmt[i] == "[":                                   # [literal]
            j = fmt.find("]", i)
            out += fmt[i + 1:j] if j > 0 else fmt[i:]
            i = j + 1 if j > 0 else len(fmt)
            continue
        for token, repl in _MOMENT:
            if fmt.startswith(token, i):
                d = when.day
                special = {"{M}": str(when.month), "{D}": str(d), "{H}": str(getattr(when, "hour", 0)),
                           "{Do}": f"{d}{'th' if 11 <= d % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(d % 10, 'th')}",
                           "{ww}": f"{when.isocalendar()[1]:02d}", "{w}": str(when.isocalendar()[1])}
                out += special.get(repl) or when.strftime(repl)
                i += len(token)
                break
        else:
            out += fmt[i]
            i += 1
    return out


def fill_template(text: str, day: date, title: str, fmt: str = "YYYY-MM-DD") -> str:
    now = datetime.combine(day, datetime.now().time())
    text = re.sub(r"\{\{date:([^}]+)\}\}", lambda m: moment(now, m.group(1)), text)
    text = re.sub(r"\{\{time:([^}]+)\}\}", lambda m: moment(now, m.group(1)), text)
    return (text.replace("{{date}}", moment(day, fmt)).replace("{{time}}", now.strftime("%H:%M"))
            .replace("{{title}}", title).replace("{{yesterday}}", moment(day - timedelta(days=1), fmt))
            .replace("{{tomorrow}}", moment(day + timedelta(days=1), fmt)))
