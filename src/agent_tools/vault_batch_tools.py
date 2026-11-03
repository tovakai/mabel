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


def _relative_scan_path(root: Path, error, index: int, used: set) -> str:
    """Relative audit path for a directory os.walk could not enter."""
    filename = getattr(error, "filename", None)
    relative = ""
    if isinstance(filename, str) and filename.strip():
        try:
            candidate = Path(filename).resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            candidate = ""
        if candidate and not Path(candidate).is_absolute() and ".." not in Path(candidate).parts:
            relative = candidate
    if not relative:
        relative = f".scan-error-{index}"
    base = relative
    suffix = 0
    while not relative or relative in used:
        suffix += 1
        relative = f"{base}-{suffix}"
    used.add(relative)
    return relative


def _fold_scan_errors(root: Path, groups: dict, scan_errors) -> None:
    """Keep a partial audit when Windows reparse points are inaccessible."""
    if not scan_errors:
        return
    used = set()
    for rows in groups.values():
        for row in rows:
            used.add(row if isinstance(row, str) else row.get("path"))
    for index, error in enumerate(scan_errors):
        groups["uninspected"].append({
            "path": _relative_scan_path(root, error, index, used),
            "reason": str(error)[:240],
            "kind": "unreadable",
        })


def _candidate_files(root: Path, pattern: str, *, include_control_dirs=False, inventory=None, scan_errors=None) -> list[Path]:
    from src.agent_runtime.workspace_discovery import is_control_directory, matches_glob
    files = []
    def traversal_error(error):
        # Inaccessible Windows reparse points abort os.walk. Record them and
        # keep scanning. Symlinks are still rejected below so a link cannot
        # widen the audit. Callers that do not pass scan_errors keep the
        # fail-closed batch behavior.
        if scan_errors is None:
            raise error
        scan_errors.append(error)
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=traversal_error):
        if inventory is not None:
            from src.agent_runtime.audit_receipts import stat_signature
            if len(inventory) >= 4096:
                raise ValueError("too many directories in audit inventory")
            inventory.append((Path(directory).relative_to(root).as_posix(), stat_signature(Path(directory))))
        # Prune before traversal and limits, so version history cannot consume
        # the active collection's bounded inventory or leak into broad globs.
        dirs[:] = [name for name in dirs if include_control_dirs or not is_control_directory(name)]
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root)
            if not matches_glob(relative.parts, pattern):
                continue
            if path.is_symlink():
                raise ValueError(f"symlinks are not allowed: {relative}")
            if not path.is_file():
                continue
            files.append(path)
            if len(files) > _MAX_FILES:
                raise ValueError(f"too many files (> {_MAX_FILES}); narrow glob")
    return sorted(files, key=lambda p: str(p).casefold())


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
            if result.get("status") == "preview":
                # A server-owned, short-lived continuation is recorded only
                # after an authorized preview actually succeeds.
                from src.agent_runtime.batch_preview_claims import remember_preview
                apply_args = remember_preview(
                    owner=ctx.get("owner"), session_id=ctx.get("session_id"),
                    workspace=workspace, payload=data, result=result,
                    authority=ctx.get("request_authority"),
                )
                if apply_args is not None:
                    result["apply_arguments"] = apply_args
            return {"output": json.dumps(result, ensure_ascii=False), "result": result, "exit_code": 0}
        except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
            return {"error": f"vault_batch_edit: {exc}", "exit_code": 1}


def validate_audit_arguments(payload):
    if not isinstance(payload, dict) or set(payload) - {"path", "glob", "property", "report_offset", "audit_id", "include_control_dirs", "classification", "require_current"}:
        raise ValueError("audit accepts only path, glob and property")
    offset = payload.get("report_offset", 0)
    if type(offset) is not int or not 0 <= offset <= _MAX_FILES:
        raise ValueError("report_offset must be an integer between 0 and 1000")
    pattern = payload.get("glob", "**/*.md")
    key = payload.get("property", "owned")
    if (not isinstance(pattern, str) or not pattern or pattern.startswith(("/", "\\"))
            or ":" in pattern or ".." in Path(pattern.replace("\\", "/")).parts or not pattern.endswith(".md")):
        raise ValueError("glob must be a relative Markdown pattern")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][\w -]*", key):
        raise ValueError("invalid property")
    if type(payload.get("include_control_dirs", False)) is not bool:
        raise ValueError("include_control_dirs must be boolean")
    if payload.get("classification") not in {None, "true", "false", "missing", "invalid", "uninspected", "not_true"}:
        raise ValueError("invalid audit classification")
    if type(payload.get("require_current", False)) is not bool:
        raise ValueError("require_current must be boolean")
    audit_id = payload.get("audit_id")
    if audit_id is not None and (not isinstance(audit_id, str) or not re.fullmatch(r"[a-f0-9]{32}", audit_id)):
        from src.agent_runtime.audit_receipts import AuditReceiptError
        raise AuditReceiptError("audit_receipt_invalid_id", "Use the native audit_id, not an invented identifier; omit audit_id for an initial audit")


def run_audit(root: Path, payload: dict, check_path=None) -> dict:
    """Bounded, read-only inventory. YAML nodes are parsed, never constructed."""
    import yaml
    from yaml.nodes import MappingNode, ScalarNode

    validate_audit_arguments(payload)
    pattern = payload.get("glob", "**/*.md")
    key = payload.get("property", "owned")
    root = root.resolve(strict=True)
    versions = []
    scan_errors = []
    files = _candidate_files(root, pattern.replace("\\", "/"), include_control_dirs=payload.get("include_control_dirs", False), inventory=versions, scan_errors=scan_errors)
    from src.agent_runtime.audit_receipts import stat_signature
    for path in files:
        try:
            versions.append((path.relative_to(root).as_posix(), stat_signature(path)))
        except OSError:
            versions.append((path.relative_to(root).as_posix(), None))
    groups = {name: [] for name in ("true", "false", "missing", "invalid", "uninspected")}
    inspected = total_bytes = malformed = 0
    for path in files:
        invalid_value = False
        relative = path.relative_to(root).as_posix()
        try:
            if check_path is not None and Path(check_path(str(path))).resolve() != path.resolve():
                raise ValueError("resource binding changed")
            if path.is_symlink() or path.stat().st_nlink != 1:
                raise ValueError("linked file is not allowed")
            identity = path.stat()
            size = identity.st_size
            if size > _MAX_BYTES or total_bytes + size > 32 * 1024 * 1024:
                raise ValueError("audit byte limit exceeded")
            # Bounded read even if a file grows after stat.
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino) or opened.st_nlink != 1:
                    raise ValueError("file identity changed before read")
                if check_path is not None:
                    check_path(str(path))
                data = stream.read(min(_MAX_BYTES, 32 * 1024 * 1024 - total_bytes) + 1)
            total_bytes += len(data)
            if len(data) > _MAX_BYTES or total_bytes > 32 * 1024 * 1024:
                raise ValueError("audit byte limit exceeded")
            source = data.decode("utf-8-sig")
        except (OSError, ValueError) as exc:
            groups["uninspected"].append({"path": relative, "reason": str(exc), "kind": "unreadable"})
            continue
        inspected += 1
        try:
            lines = source.splitlines()
            if not lines or lines[0].strip() != "---":
                groups["missing"].append(relative)
                continue
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in {"---", "..."}), -1)
            if end < 0:
                raise ValueError("unclosed frontmatter")
            frontmatter = "\n".join(lines[1:end])
            if len(frontmatter) > 65536:
                raise ValueError("frontmatter exceeds 64 KiB parser limit")
            node = yaml.compose(frontmatter, Loader=yaml.SafeLoader)
            if node is None:
                groups["missing"].append(relative)
                continue
            if not isinstance(node, MappingNode):
                raise ValueError("frontmatter must be a mapping")
            seen = set()
            value = None
            for name, item in node.value:
                if not isinstance(name, ScalarNode) or name.value in seen:
                    raise ValueError("duplicate or non-scalar frontmatter key")
                if name.value == "<<":
                    raise ValueError("YAML merge keys require manual inspection")
                seen.add(name.value)
                if name.value == key:
                    value = item
            if key not in seen:
                groups["missing"].append(relative)
            elif (isinstance(value, ScalarNode) and value.tag == "tag:yaml.org,2002:bool"
                  and value.value.lower() in {"true", "false"}):
                groups[value.value.lower()].append(relative)
            else:
                invalid_value = True
                raise ValueError(f"{key} must be a literal YAML boolean true/false")
        except (ValueError, yaml.YAMLError, RecursionError) as exc:
            malformed += int(not invalid_value)
            reason = str(exc) if isinstance(exc, ValueError) else "invalid or excessively nested YAML frontmatter"
            groups["invalid"].append({"path": relative, "reason": reason[:240], "kind": "invalid" if invalid_value else "malformed"})
    _fold_scan_errors(root, groups, scan_errors)
    counts = {name: len(rows) for name, rows in groups.items()}
    discovered = sum(counts.values())
    complete = inspected == discovered
    return {"schema_version": 1, "complete": complete,
            "property_counts": {**{k: counts[k] for k in ("true", "false", "missing")},
                                "invalid": counts["invalid"] - malformed, "malformed": malformed,
                                "unreadable": counts["uninspected"]},
            "errors": groups["invalid"] + groups["uninspected"],
            "status": "complete" if complete else "partial",
            "discovery_policy": "include_control_dirs" if payload.get("include_control_dirs", False) else "active_content",
            "property": key, "discovered": discovered, "inspected": inspected,
            "counts": counts,
            "files": groups, "bytes_read": total_bytes, "modified": 0, "_versions": versions}


def audit_report_page(report, offset=0, *, detail_budget=6000, classification=None):
    """Keep model evidence within the dispatcher cap; never truncate silently.

    Every page retains full audit counts. Detail pages are independent of scan
    coverage, and list positive ownership and invalid metadata first.
    """
    page = {k: v for k, v in report.items() if k not in {"files", "errors", "_versions"}}
    page["errors"] = []
    error_budget = 1000
    for error in report.get("errors", [])[:3]:
        size = len(json.dumps(error, ensure_ascii=False))
        if size > error_budget:
            break
        page["errors"].append(error)
        error_budget -= size
    page["errors_truncated"] = len(report.get("errors", [])) > len(page["errors"])
    page["error_count"] = len(report.get("errors", []))
    entries = []
    categories = ("false", "missing") if classification == "not_true" else (classification,) if classification else ("true", "invalid", "uninspected", "false", "missing")
    for category in categories:
        for item in report["files"][category]:
            entries.append({"classification": item.get("kind", category) if isinstance(item, dict) else category, **(item if isinstance(item, dict) else {"path": item})})
    detail = []
    used = 0
    for entry in entries[offset:]:
        size = len(json.dumps(entry, ensure_ascii=False))
        if detail and used + size > detail_budget:
            break
        detail.append(entry)
        used += size
    next_offset = offset + len(detail)
    page.update(entries=detail, classification=classification, matching=len(entries), report_offset=offset,
                next_offset=next_offset if next_offset < len(entries) else None)
    return page


class VaultAuditTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import _resolve_tool_path
        from src.agent_runtime.structured_audits import AuditScopeError
        from src.agent_runtime.audit_receipts import AuditReceiptError
        from src.agent_runtime.resources import ResourceIdentityError
        try:
            args = json.loads(content)
            if not isinstance(args, dict):
                raise ValueError("expected JSON object")
            # The dispatcher binds this directory to the request's sealed roots.
            root = Path(_resolve_tool_path(args.get("path", "")))
            validate_audit_arguments(args)
            from src.agent_runtime.structured_audits import active_audit, AuditScopeError
            audit = active_audit()
            if audit is not None:
                audit.check_operation(root, args)
            elif args.get("include_control_dirs", False):
                raise AuditScopeError("Including control/history directories requires explicit trusted audit intent")
            if audit is None:
                from src.agent_runtime.workspace_discovery import is_control_directory
                workspace = (ctx.get("request_authority").workspace if ctx.get("request_authority") else str(root))
                if any(is_control_directory(p) for p in root.relative_to(Path(workspace).resolve()).parts):
                    raise AuditScopeError("Auditing a control/history directory requires explicit trusted audit intent")
            from src.agent_runtime.audit_receipts import lookup, remember
            result = lookup(ctx, root, args)
            if result is None:
                if args.get("report_offset", 0) > 0:
                    raise ValueError("Pagination requires an existing audit receipt; start with an aggregate audit")
                result = await asyncio.to_thread(run_audit, root, args, _resolve_tool_path)
                result = remember(ctx, root, args, result)
            from src.agent_runtime.structured_audits import record_native_audit
            record_native_audit(result, root, args)
            if "report_offset" in args:
                observation = audit_report_page(result, args["report_offset"], detail_budget=1500, classification=args.get("classification"))
            else:
                observation = {k: v for k, v in result.items() if k not in {"files", "errors"}}
                observation.update(error_count=len(result["errors"]), details_available=True,
                                   detail_count=result["discovered"], sample_only=True)
                observation["sample_entries"] = [row for row in audit_report_page(result, detail_budget=350)["entries"][:3]
                                                 if len(json.dumps(row, ensure_ascii=False)) <= 350]
            return {"output": json.dumps(observation, ensure_ascii=False), "result": result, "exit_code": 0}
        except ResourceIdentityError as exc:
            return {"error": str(exc), "exit_code": 1, "failure_kind": "resource_identity_denied", "blocked": True, "executed": False}
        except (AuditScopeError, AuditReceiptError) as exc:
            return {"error": f"vault_audit: {exc}", "exit_code": 1,
                    "failure_kind": exc.code, "executed": False, "recoverable": True}
        except ImportError:
            return {"error": "YAML parser unavailable", "exit_code": 1,
                    "failure_kind": "missing_optional_dependency", "executed": False}
        except (ValueError, OSError, TypeError) as exc:
            return {"error": f"vault_audit: {exc}", "exit_code": 1,
                    "failure_kind": "invalid_arguments", "executed": False}
