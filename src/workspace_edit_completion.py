"""Evidence-based completion for a bounded Markdown frontmatter migration.

This intentionally recognises a narrow, explicitly requested workflow. It must
never turn an arbitrary successful write into a claim that a multi-step task is done.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import PurePath
from typing import Any, Mapping, Sequence


_META = re.compile(r"(?m)^\s*([A-Za-z_][\w-]*):[ \t]*(\S[^\r\n]*)\s*$")
_MD_ENTRY = re.compile(r"(?:^|[/\s\"'])((?:[\w\-.(),'!]+[ ]?)*[\w\-.(),'!]+\.md)(?![\w.])", re.I)


def _args(event: Mapping[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(event.get("command") or ""))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _file(event: Mapping[str, Any]) -> str:
    args = _args(event)
    raw = str(args.get("path") or event.get("command") or "").strip()
    return PurePath(raw).name


def _ok(event: Mapping[str, Any]) -> bool:
    code = event.get("exit_code")
    return code in (None, 0, "0") and not event.get("error")


def _clean_edit(old: str, new: str, expected: set[str], marker: str) -> bool:
    """Only the named metadata operations may change between edit fragments."""
    lines_old = old.splitlines()
    lines_new = new.splitlines()
    deletions: set[str] = set()
    additions: set[str] = set()
    for tag, i, j, k, m in difflib.SequenceMatcher(None, lines_old, lines_new).get_opcodes():
        if tag in ("replace", "delete"):
            deletions.update(line.strip() for line in lines_old[i:j])
        if tag in ("replace", "insert"):
            additions.update(line.strip() for line in lines_new[k:m])
    if any(line != marker for line in deletions):
        return False
    if not additions or not additions.issubset(expected):
        return False
    return True


def verified_markdown_migration_summary(
    request: str, events: Sequence[Mapping[str, Any]]
) -> str | None:
    """Return a grounded terminal report, but only after every discovered note is checked.

    No task-type inference or model prose is trusted as execution evidence.
    This is deliberately conservative: if discovery, diff, or readback is
    ambiguous, the regular agent loop remains responsible for completing it.
    """
    text = str(request or "")
    if not all(re.search(pattern, text, re.I | re.S) for pattern in (
        r"\bmarkdown\s+files?\b|\b\.md\b",
        r"\b(?:yaml|frontmatter|properties)\b",
        r"\b(?:remove|delete)\b",
        r"\b(?:add|insert|set)\b",
        r"\b(?:verify|read\s+the\s+files?\s+again|read\s+back|check)\b",
    )):
        return None
    match = re.search(r"[‘’\x60\x22]([^‘’\x60\x22\n]+:\s*\[\[[^‘’\x60\x22\n]+\]\])[‘’\x60\x22]", text)
    if not match:
        return None
    marker = match.group(1).strip()
    expected_props = {f"{k}: {v.strip()}" for k, v in _META.findall(text)}
    if not expected_props or any("[[" in line for line in expected_props):
        return None
    md_paths: set[str] = set()
    # Only trust native discovery; a generic shell command might be scoped
    # somewhere unrelated to the selected workspace.
    for event in events:
        tool = str(event.get("tool") or "")
        if tool not in {"ls", "glob"} or not _ok(event):
            continue
        if tool == "glob" and not re.search(r"\*\.md", str(event.get("command") or "")):
            continue
        found = _MD_ENTRY.findall(str(event.get("output") or ""))
        if found:
            md_paths.update(found)
    if not md_paths:
        return None

    read_indices: dict[str, list[tuple[int, str]]] = {}
    edits: dict[str, int] = {}
    for i, event in enumerate(events):
        tool = str(event.get("tool") or "")
        if tool == "read_file" and _ok(event):
            path = _file(event)
            if path in md_paths:
                read_indices.setdefault(path, []).append((i, str(event.get("output") or "")))
        elif tool in {"edit_file", "write_file", "apply_patch"} and _ok(event):
            # Any unrelated mutation means the request is more complex than
            # this narrow verifier; do not claim the migration is complete.
            if tool != "edit_file":
                return None
            args = _args(event)
            path = _file(event)
            old = str(args.get("old_string") or "")
            new = str(args.get("new_string") or "")
            if path not in md_paths or not _clean_edit(old, new, expected_props, marker):
                return None
            edits[path] = i

    if not edits or not md_paths.issubset(read_indices):
        return None
    for path, edit_index in edits.items():
        if not any(
            read_index > edit_index
            and expected_props.issubset({line.strip() for line in contents.splitlines()})
            and marker not in {line.strip() for line in contents.splitlines()}
            and "[Tool result truncated" not in contents
            for read_index, contents in read_indices[path]
        ):
            return None
    untouched = sorted(md_paths - edits.keys())
    for path in untouched:
        if not any(
            marker not in {line.strip() for line in contents.splitlines()}
            for _, contents in read_indices[path]
        ):
            return None
    edited = sorted(edits)
    lines = ["Markdown metadata update verified from file readbacks.", "", "Changed:"]
    lines.extend(f"- {path}" for path in edited)
    if untouched:
        lines.extend(["", "Inspected and left unchanged:"])
        lines.extend(f"- {path}" for path in untouched)
    lines.extend(["", f"Verified the requested properties and absence of {marker} in the changed files."])
    return "\n".join(lines)
