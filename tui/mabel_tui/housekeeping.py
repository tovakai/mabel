"""Mabel's Gremlin housekeeping suggestions, read from (and ticked in) the Obsidian note she writes.

Lines look like:  - [ ] **Generic 1×4 Gridfinity bin** `00000217`: spec ID: GFST0104 _(check which one is right)_
A suggestion whose change is "spec ID: …", "name: …" or "category: …" can be applied straight to
tk-index (one edit per item, undoable there); anything else ("specs: measure the gauge") is a
to-do for him, which he ticks off himself.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

NOTE = "daily life of Anthon/Gremlin housekeeping.md"
LINE = re.compile(r"^(?P<lead>\s*[-*]\s+\[)(?P<mark>[ xX])(?P<rest>\]\s+\*\*(?P<name>.+?)\*\*\s+(?P<ids>(?:`\d{8}`(?:,\s*)?)+)"
                  r":\s*(?P<change>.+?)(?:\s+_\((?P<why>.*)\)_)?\s*)$")
SECTION = re.compile(r"^##\s+(?P<title>.+?)\s*$")
FIELDS = {"spec id": "spec_code", "spec": "spec_code", "name": "name", "category": "category"}


@dataclass
class Suggestion:
    line_no: int
    raw: str
    section: str
    name: str
    ids: list[str]
    change: str
    why: str = ""
    done: bool = False
    edit: dict = field(default_factory=dict)       # {"spec_code": "GFST0104"} when it can be applied

    @property
    def applicable(self) -> bool:
        return bool(self.edit)


def note_path(vault: str | Path) -> Path:
    return Path(vault).expanduser() / NOTE


def parse(text: str) -> list[Suggestion]:
    out, section = [], ""
    for n, line in enumerate(text.splitlines()):
        head = SECTION.match(line)
        if head:
            section = head["title"]
            continue
        m = LINE.match(line)
        if not m:
            continue
        change = m["change"].strip()
        field_name, _, value = change.partition(":")
        edit = {}
        key = FIELDS.get(field_name.strip().lower())
        if key and value.strip():
            edit = {key: value.strip()}
        out.append(Suggestion(n, line, section, m["name"], re.findall(r"\d{8}", m["ids"]), change,
                              (m["why"] or "").strip(), m["mark"] != " ", edit))
    return out


def load(vault: str | Path) -> list[Suggestion]:
    try:
        return parse(note_path(vault).read_text(encoding="utf-8"))
    except OSError:
        return []


def tick(vault: str | Path, suggestion: Suggestion, done: bool = True) -> bool:
    """Tick (or untick) that exact line; False if the note changed under us."""
    path = note_path(vault)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    n = suggestion.line_no
    if n >= len(lines) or lines[n].rstrip("\n") != suggestion.raw:
        n = next((i for i, l in enumerate(lines) if l.rstrip("\n") == suggestion.raw), -1)
        if n < 0:
            return False
    m = LINE.match(lines[n].rstrip("\n"))
    if not m:
        return False
    ending = "\n" if lines[n].endswith("\n") else ""
    lines[n] = f"{m['lead']}{'x' if done else ' '}{m['rest']}{ending}"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)
    return True
