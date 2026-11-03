"""Resolve conversation directory scope for a chat turn.

Legacy ``workspace`` FormData may bind a turn only when it matches an existing
allowed-directory grant. It never creates persistent grants.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.fs_devices.registry import (
    find_local_grant_by_path,
    migrate_trusted_workspace_grants,
)
from src.fs_devices.scope import ActiveDirectoryScope, resolve_active_scope

logger = logging.getLogger(__name__)


def parse_active_directory_ids(raw: Any) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        values = raw
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = [part.strip() for part in text.split(",")]
            values = parsed if isinstance(parsed, list) else []
        else:
            values = [part.strip() for part in text.split(",") if part.strip()]
    out = []
    seen = set()
    for item in values:
        grant_id = str(item or "").strip()
        if not grant_id or grant_id in seen:
            continue
        seen.add(grant_id)
        out.append(grant_id)
    return out


def bind_turn_directory_scope(
    *,
    request_active_ids: Any = None,
    request_primary_local_id: Any = None,
    request_workspace_path: str = "",
    session: Any = None,
    allow_trusted_migration: bool = True,
) -> tuple[ActiveDirectoryScope, str, str]:
    """Return ``(scope, compat_workspace_path, rejected_workspace_path)``.

    ``compat_workspace_path`` is the explicit primary local path when set, else
    empty. Legacy single-root callers still receive a workspace string only when
    a primary local grant is active — never a silent first-of-list fallback.
    """
    if allow_trusted_migration:
        session_cwds = []
        if session is not None:
            cwd = str(getattr(session, "cwd", "") or "").strip()
            if cwd:
                session_cwds.append(cwd)
        try:
            migrate_trusted_workspace_grants(session_cwds=session_cwds)
        except Exception as exc:
            logger.warning("Trusted workspace grant migration skipped: %s", exc)

    active_ids = parse_active_directory_ids(request_active_ids)
    primary = str(request_primary_local_id or "").strip() or None

    if not active_ids and session is not None:
        active_ids = list(getattr(session, "active_directory_ids", None) or [])
    if not primary and session is not None:
        primary = str(getattr(session, "primary_local_directory_id", "") or "").strip() or None

    rejected = ""
    workspace_path = str(request_workspace_path or "").strip()
    if workspace_path:
        grant = find_local_grant_by_path(workspace_path)
        if grant is not None:
            if grant.id not in active_ids:
                active_ids.append(grant.id)
            # Legacy workspace posts do not invent a primary; only use as
            # primary when the session already designated it or nothing else is.
            if primary is None and grant.permission == "rw":
                # Still require explicit primary for shell; FS tools can use the
                # grant via active set. Compat workspace stays unset unless primary.
                pass
        else:
            # Unknown path: do not create a grant; surface rejection for admins.
            rejected = workspace_path

    scope = resolve_active_scope(active_ids, primary_local_directory_id=primary)
    compat = scope.compat_workspace_path() or ""
    return scope, compat, rejected
