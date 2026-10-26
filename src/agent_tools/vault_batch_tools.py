"""Workspace-confined, reviewable bulk text and Obsidian frontmatter edits.

The model describes one set operation; deterministic code enumerates, previews,
validates, applies and verifies it. Preview fingerprints the complete file set.
No shell, subprocess, extra package, or write outside the active workspace.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any

_MAX_FILES = 1000
_MAX_BYTES = 2 * 1024 * 1024
_KEY = re.compile(r"^([A-Za-z_][\w -]*):(?:\s|$)")
_PLAIN = re.compile(r"^[A-Za-z][\w ./-]*$")


def _linebreak(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        if _PLAIN.fullmatch(value) and value.lower() not in {"true", "false", "null", "yes", "no", "on", "off"}:
            return value
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) and len(value) <= 100 and all(type(x) in {bool, int, str} for x in value):
        return "[" + ", ".join(_yaml_scalar(item) for item in value) + "]"
    raise ValueError("frontmatter values must be strings, integers, booleans or short scalar lists")


def _frontmatter_edit(source: str, args: dict[str, Any]) -> str:
    newline = _linebreak(source)
    lines = source.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), -1)
        if end == -1:
            raise ValueError("unclosed YAML frontmatter")
        existing = lines[1:end]
        body = lines[end + 1:]
    else:
        existing, body = [], lines

    removed_lines = set(args.get("remove_lines") or [])
    existing = [line for line in existing if line.rstrip("\r\n") not in removed_lines]
    body = [line for line in body if line.rstrip("\r\n") not in removed_lines]

    set_props = args.get("set_properties") or {}
    remove_keys = set(args.get("remove_properties") or []) | set(set_props)
    result = []
    i = 0
    seen: set[str] = set()
    while i < len(existing):
        line = existing[i]
        match = _KEY.match(line.rstrip("\r\n"))
        if match:
            key = match.group(1).strip()
            if key in seen:
                raise ValueError(f"duplicate frontmatter key: {key}")
            seen.add(key)
            if key in remove_keys:
                # Never silently discard multi-line YAML structures.
                remainder = line[match.end():].strip()
                if remainder in {"|", ">", "|-", ">-", "|+", ">+"}:
                    raise ValueError(f"complex YAML value for key: {key}")
                if i + 1 < len(existing) and (existing[i + 1].startswith((" ", "\t")) or existing[i + 1].lstrip().startswith("- ")):
                    raise ValueError(f"nested YAML value for key: {key}")
                i += 1
                continue
        result.append(line)
        i += 1
    for key, value in set_props.items():
        result.append(f"{key}: {_yaml_scalar(value)}{newline}")
    if not (lines and lines[0].strip() == "---") and not (result or set_props):
        return "".join(body)
    if result and not result[-1].endswith(("\n", "\r")):
        result[-1] += newline
    final = "---" + newline + "".join(result) + "---" + newline + "".join(body)
    fm = final.splitlines()
    if not fm or fm[0] != "---":
        raise ValueError("frontmatter verification failed")
    close = next((idx for idx in range(1, len(fm)) if fm[idx] == "---"), -1)
    if close < 0:
        raise ValueError("frontmatter close delimiter missing")
    fm_lines = fm[1:close]
    for key, value in set_props.items():
        expected = f"{key}: {_yaml_scalar(value)}"
        if fm_lines.count(expected) != 1:
            raise ValueError(f"frontmatter property failed verification: {key}")
    for key in args.get("remove_properties") or []:
        if any(_KEY.match(x) and _KEY.match(x).group(1).strip() == key for x in fm_lines):
            raise ValueError(f"frontmatter deletion failed verification: {key}")
    if any(line.rstrip("\r\n") in removed_lines for line in final.splitlines(keepends=True)):
        raise ValueError("removed line remains after transformation")
    return final


def _transform(source: str, args: dict[str, Any]) -> str:
    if args["operation"] == "frontmatter":
        return _frontmatter_edit(source, args)
    old, new = args["old_string"], args["new_string"]
    count = source.count(old)
    if not count:
        return source
    if count > 1 and not args.get("replace_all", False):
        raise ValueError(f"replacement not unique ({count} occurrences)")
    return source.replace(old, new) if args.get("replace_all") else source.replace(old, new, 1)


def _validated_args(args: dict[str, Any]) -> dict[str, Any]:
    mode = args.get("mode", "preview")
    operation = args.get("operation", "frontmatter")
    glob = args.get("glob", "**/*.md")
    if mode not in {"preview", "apply"} or operation not in {"frontmatter", "replace"}:
        raise ValueError("mode must be preview/apply and operation frontmatter/replace")
    if not isinstance(glob, str) or not glob or glob.startswith(("/", "\\")) or ":" in glob or ".." in Path(glob.replace("\\", "/")).parts or not glob.endswith(".md"):
        raise ValueError("glob must be a relative Markdown pattern within the workspace")
    contains = args.get("contains_line")
    if contains is not None and (not isinstance(contains, str) or not contains or "\n" in contains or "\r" in contains):
        raise ValueError("contains_line must be one exact nonempty line")
    sets = args.get("set_properties") or {}
    deletes = args.get("remove_properties") or []
    removed_lines = args.get("remove_lines") or []
    if operation == "frontmatter":
        if not isinstance(sets, dict) or not isinstance(deletes, list) or not isinstance(removed_lines, list) or not (sets or deletes or removed_lines):
            raise ValueError("frontmatter edit needs valid set_properties, remove_properties or remove_lines")
        keys = list(sets) + list(deletes)
        if any(not isinstance(x, str) or not re.fullmatch(r"[A-Za-z_][\w -]*", x) for x in keys):
            raise ValueError("invalid YAML key")
        if len(keys) != len(set(keys)) and set(sets).intersection(deletes):
            raise ValueError("cannot set and remove the same YAML key")
        if not all(isinstance(x, str) and x and "\n" not in x and "\r" not in x for x in removed_lines):
            raise ValueError("remove_lines must contain exact single-line strings")
        for value in sets.values():
            _yaml_scalar(value)
    else:
        if not isinstance(args.get("old_string"), str) or not args["old_string"] or not isinstance(args.get("new_string"), str) or args["old_string"] == args["new_string"]:
            raise ValueError("replace requires different nonempty old_string and string new_string")
        if not isinstance(args.get("replace_all", False), bool):
            raise ValueError("replace_all must be boolean")
    result = {"mode": mode, "operation": operation, "glob": glob.replace("\\", "/"), "contains_line": contains}
    if operation == "frontmatter":
        result.update(set_properties=sets, remove_properties=deletes, remove_lines=removed_lines)
    else:
        result.update(old_string=args["old_string"], new_string=args["new_string"], replace_all=args.get("replace_all", False))
    if mode == "apply":
        plan = args.get("plan_id")
        if not isinstance(plan, str) or not re.fullmatch(r"[a-f0-9]{64}", plan):
            raise ValueError("apply requires plan_id returned by preview")
        result["plan_id"] = plan
    return result


def _candidate_files(root: Path, pattern: str) -> list[Path]:
    candidates = sorted(root.glob(pattern), key=lambda p: str(p).casefold())
    if len(candidates) > _MAX_FILES:
        raise ValueError(f"too many files ({len(candidates)} > {_MAX_FILES}); narrow glob")
    files = []
    for path in candidates:
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        current = root
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise ValueError(f"symlinks are not allowed: {relative}")
        files.append(path)
    return files


def _prepare(root: Path, args: dict[str, Any], check_path=None) -> tuple[str, list[tuple[Path, bytes, bytes]], dict[str, Any]]:
    candidates = _candidate_files(root, args["glob"])
    changed = []
    inventory = []
    matched = 0
    preview = []
    for path in candidates:
        if check_path is not None:
            confined = Path(check_path(str(path)))
            if confined.resolve() != path.resolve():
                raise ValueError("file admission changed during planning")
        if path.stat().st_nlink != 1:
            raise ValueError(f"hardlinked file is not allowed: {path.name}")
        if path.stat().st_size > _MAX_BYTES:
            raise ValueError(f"file is too large: {path.name}")
        original = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        inventory.append([relative, hashlib.sha256(original).hexdigest()])
        try:
            source = original.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"not UTF-8: {relative}") from exc
        if args["contains_line"] is not None and args["contains_line"] not in source.splitlines():
            continue
        matched += 1
        updated = _transform(source, args)
        target = (b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b"") + updated.encode("utf-8")
        if target != original:
            changed.append((path, original, target))
            if len(preview) < 3:
                diff = "".join(list(difflib.unified_diff(source.splitlines(keepends=True), updated.splitlines(keepends=True), fromfile=relative, tofile=relative))[:50])
                preview.append({"path": relative, "diff": diff[:6000]})
    spec = {k: v for k, v in args.items() if k not in {"mode", "plan_id"}}
    fingerprint = hashlib.sha256(json.dumps({"root": str(root), "spec": spec, "inventory": inventory}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    report = {"scanned": len(candidates), "matched": matched, "to_change": len(changed), "unchanged_matches": matched - len(changed), "sample_diffs": preview}
    return fingerprint, changed, report


def _atomic_write(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=".mabel-batch-", dir=path.parent)
    try:
        os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def run_batch(root: Path, payload: dict[str, Any], check_path=None) -> dict[str, Any]:
    """Pure planning + guarded commit. Caller must pass the trusted workspace root."""
    args = _validated_args(payload)
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("active workspace is not a directory")
    fingerprint, changes, report = _prepare(root, args, check_path)
    if args["mode"] == "preview":
        return {"status": "preview", "plan_id": fingerprint, **report}
    if args["plan_id"] != fingerprint:
        raise ValueError("workspace changed since preview; request a new preview")
    completed = []
    try:
        for path, old, new in changes:
            if check_path is not None and Path(check_path(str(path))).resolve() != path.resolve():
                raise ValueError(f"path admission changed: {path.name}")
            if path.is_symlink() or path.read_bytes() != old:
                raise ValueError(f"file changed during batch: {path.name}")
            _atomic_write(path, new)
            completed.append((path, old))
            if path.read_bytes() != new:
                raise ValueError(f"post-write verification failed: {path.name}")
    except Exception as exc:
        rollback_errors = []
        for path, old in reversed(completed):
            try:
                _atomic_write(path, old)
            except OSError as rollback_exc:
                rollback_errors.append(f"{path.name}: {rollback_exc}")
        raise ValueError(f"batch failed: {exc}; rollback failures: {rollback_errors}") from exc
    return {"status": "verified", "scanned": report["scanned"], "matched": report["matched"], "updated": len(changes), "unchanged_matches": report["unchanged_matches"], "failed": 0, "updated_paths": [str(path.relative_to(root)).replace(os.sep, "/") for path, _, _ in changes][:50], "updated_paths_truncated": len(changes) > 50}


class VaultBatchEditTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path, get_active_workspace
        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("expected JSON object")
            workspace = get_active_workspace()
            if not workspace:
                raise ValueError("a selected workspace is required for batch edits")
            result = await asyncio.to_thread(run_batch, Path(workspace), data, _resolve_tool_path)
            return {"output": json.dumps(result, ensure_ascii=False), "result": result, "exit_code": 0}
        except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
            return {"error": f"vault_batch_edit: {exc}", "exit_code": 1}
