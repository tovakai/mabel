"""Platform-aware path helpers for local and remote filesystem adapters."""

from __future__ import annotations

import os
import re
from pathlib import PurePosixPath, PureWindowsPath


_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def detect_path_style(path: str) -> str:
    value = str(path or "")
    if _WIN_ABS.match(value) or "\\" in value:
        return "windows"
    return "posix"


def pure_path(path: str, *, style: str):
    return PureWindowsPath(path) if style == "windows" else PurePosixPath(path)


def is_absolute(path: str, *, style: str) -> bool:
    return pure_path(path, style=style).is_absolute()


def join_path(root: str, *parts: str, style: str) -> str:
    current = pure_path(root, style=style)
    for part in parts:
        piece = str(part or "")
        if not piece or piece in {".", ""}:
            continue
        if style == "windows":
            current = current.joinpath(*PureWindowsPath(piece).parts)
        else:
            current = current.joinpath(*PurePosixPath(piece).parts)
    text = str(current)
    if style == "windows":
        return text
    # PurePosixPath collapses duplicate slashes but keeps a single leading /.
    return text if text.startswith("/") or not root.startswith("/") else "/" + text


def parent_path(path: str, *, style: str) -> str | None:
    p = pure_path(path, style=style)
    parent = p.parent
    if str(parent) == str(p):
        return None
    return str(parent)


def normalize_remote_path(path: str, *, style: str) -> str:
    """Normalize without touching the local OS filesystem."""
    p = pure_path(path, style=style)
    parts = []
    for part in p.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts or parts[-1] == ".." or (style == "posix" and parts == ["/"]):
                # Keep leading root; refuse escaping above root by dropping .. at root.
                if style == "posix" and parts == ["/"]:
                    continue
                if style == "windows" and len(parts) == 1 and parts[0].endswith(":"):
                    continue
                if style == "windows" and parts and parts[0].startswith("\\\\"):
                    # UNC share root — do not climb above share
                    if len(parts) <= 2:
                        continue
            if parts and parts[-1] != "..":
                parts.pop()
                continue
            parts.append("..")
            continue
        parts.append(part)
    if style == "posix":
        if path.startswith("/"):
            return "/" + "/".join(p for p in parts if p != "/")
        return "/".join(parts) or "."
    return str(PureWindowsPath(*parts)) if parts else str(p.anchor or ".")


def confine_logical(root: str, candidate: str, *, style: str) -> str:
    """Contain ``candidate`` under ``root`` using logical path rules (no OS realpath).

    Relative candidates join the root. Absolute candidates must already lie under
    the root after normalization. ``..`` escape attempts raise ``ValueError``.
    """
    root_n = normalize_remote_path(root, style=style)
    cand = str(candidate or "").strip()
    if not cand:
        raise ValueError("path is required")
    if is_absolute(cand, style=style):
        resolved = normalize_remote_path(cand, style=style)
    else:
        resolved = normalize_remote_path(join_path(root_n, cand, style=style), style=style)
    root_p = pure_path(root_n, style=style)
    resolved_p = pure_path(resolved, style=style)
    try:
        resolved_p.relative_to(root_p)
    except ValueError as exc:
        raise ValueError(f"path escapes authorized root ({root_n})") from exc
    return str(resolved_p)


def local_style() -> str:
    return "windows" if os.name == "nt" else "posix"
