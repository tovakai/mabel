"""Route filesystem tool I/O through local or SFTP adapters with RO enforcement."""

from __future__ import annotations

import os
from typing import Any

from src.fs_devices.adapters import LocalAdapter
from src.fs_devices.path_ops import confine_logical
from src.tool_execution import get_active_directory_scope


class ScopeSelectionError(ValueError):
    pass


class ReadOnlyGrantError(PermissionError):
    pass


def _scope_or_error():
    scope = get_active_directory_scope()
    if scope is None:
        return None
    if not scope.available_roots and not scope.roots:
        raise ScopeSelectionError(
            "No active directories are selected for this conversation"
        )
    return scope


def resolve_scoped_target(path: str | None = None, grant_id: str | None = None) -> dict[str, Any]:
    """Resolve a tool path to a concrete root + confined path + adapter backend."""
    scope = _scope_or_error()
    if scope is None:
        # Legacy single-workspace path (no multi-root scope bound).
        return {"legacy": True, "path": path, "grant_id": None, "root": None}

    root = None
    if grant_id:
        root = scope.get(str(grant_id))
        if root is None or not root.available:
            raise ScopeSelectionError(f"grant_id '{grant_id}' is not an available active directory")
    elif path:
        matches = []
        for candidate in scope.available_roots:
            try:
                if candidate.backend == "local":
                    from src.tool_execution import _resolve_tool_path_in_workspace
                    resolved = _resolve_tool_path_in_workspace(candidate.path, path)
                    matches.append((candidate, resolved))
                else:
                    resolved = confine_logical(candidate.path, path, style=candidate.path_style)
                    matches.append((candidate, resolved))
            except (ValueError, OSError):
                continue
        if len(matches) == 1:
            root, path = matches[0][0], matches[0][1]
        elif len(matches) > 1:
            uniq = {m[1] for m in matches}
            if len(uniq) == 1:
                root, path = matches[0][0], matches[0][1]
            else:
                raise ScopeSelectionError(
                    "path matches multiple active directories; pass grant_id to disambiguate"
                )
        else:
            raise ScopeSelectionError(f"path '{path}' is outside the active directories")
    else:
        primary = scope.primary_local()
        if primary is not None:
            root, path = primary, primary.path
        elif len(scope.available_roots) == 1:
            root = scope.available_roots[0]
            path = root.path
        else:
            raise ScopeSelectionError(
                "path or grant_id is required when multiple directories are active"
            )

    return {
        "legacy": False,
        "path": path,
        "grant_id": root.grant_id,
        "root": root,
        "device_id": root.device_id,
        "permission": root.permission,
        "backend": root.backend,
        "display_name": root.display_name,
    }


def require_writable(target: dict) -> None:
    if target.get("legacy"):
        return
    if target.get("permission") == "ro":
        raise ReadOnlyGrantError(
            f"Directory '{target.get('display_name')}' is read-only"
        )


def read_text_via_scope(path: str, *, grant_id: str | None = None, max_bytes: int) -> dict:
    target = resolve_scoped_target(path, grant_id)
    if target["legacy"]:
        return {"legacy": True}
    root = target["root"]
    if root.backend == "local":
        adapter = LocalAdapter(root.device_id)
        data = adapter.read_bytes(target["path"], max_bytes=max_bytes)
    else:
        from src.fs_devices.connections import open_sftp_adapter
        from src.fs_devices.registry import get_device
        device = get_device(root.device_id)
        if device is None:
            raise ScopeSelectionError("device unavailable")
        adapter = open_sftp_adapter(device)
        confined = adapter.confine(root.path, target["path"])
        info = adapter.stat(confined)
        if info.is_symlink:
            raise ValueError("refusing to read symlink")
        data = adapter.read_bytes(confined, max_bytes=max_bytes)
        target["path"] = confined
    text = data.decode("utf-8", errors="replace")
    return {
        "legacy": False,
        "text": text,
        "path": target["path"],
        "grant_id": target["grant_id"],
        "device_id": target["device_id"],
        "display_name": target["display_name"],
        "backend": target["backend"],
    }


def write_bytes_via_scope(path: str, data: bytes, *, grant_id: str | None = None,
                          exclusive: bool = False) -> dict:
    target = resolve_scoped_target(path, grant_id)
    if target["legacy"]:
        return {"legacy": True}
    require_writable(target)
    root = target["root"]
    if root.backend == "local":
        adapter = LocalAdapter(root.device_id)
        adapter.write_bytes(target["path"], data, exclusive=exclusive)
    else:
        from src.fs_devices.connections import open_sftp_adapter
        from src.fs_devices.registry import get_device
        device = get_device(root.device_id)
        if device is None:
            raise ScopeSelectionError("device unavailable")
        adapter = open_sftp_adapter(device)
        confined = adapter.confine(root.path, target["path"])
        adapter.write_bytes(confined, data, exclusive=exclusive)
        target["path"] = confined
    return {
        "legacy": False,
        "path": target["path"],
        "grant_id": target["grant_id"],
        "device_id": target["device_id"],
        "display_name": target["display_name"],
        "backend": target["backend"],
    }
