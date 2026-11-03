"""Conversation-scoped active directories intersecting persistent grants."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from src.fs_devices.registry import (
    LOCAL_DEVICE_ID,
    AllowedDirectory,
    get_allowed_directory,
    get_device,
    list_allowed_directories,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopedRoot:
    grant_id: str
    device_id: str
    device_name: str
    display_name: str
    path: str
    permission: str  # ro | rw
    backend: str  # local | sftp
    path_style: str
    available: bool
    unavailable_reason: str | None = None

    def public_dict(self) -> dict:
        return {
            "grant_id": self.grant_id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "display_name": self.display_name,
            "path": self.path,
            "permission": self.permission,
            "backend": self.backend,
            "path_style": self.path_style,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class ActiveDirectoryScope:
    roots: tuple[ScopedRoot, ...]
    primary_local_grant_id: str | None = None

    @property
    def available_roots(self) -> tuple[ScopedRoot, ...]:
        return tuple(r for r in self.roots if r.available)

    def get(self, grant_id: str) -> ScopedRoot | None:
        for root in self.roots:
            if root.grant_id == grant_id:
                return root
        return None

    def primary_local(self) -> ScopedRoot | None:
        if not self.primary_local_grant_id:
            return None
        root = self.get(self.primary_local_grant_id)
        if root is None or not root.available or root.backend != "local":
            return None
        if root.permission != "rw":
            return None
        # Must still be in the active set (already true via get on roots).
        return root

    def context_table(self) -> list[dict]:
        """Compact agent-facing table — no directory trees."""
        rows = []
        for root in self.roots:
            rows.append({
                "grant_id": root.grant_id,
                "label": root.display_name,
                "device": root.device_name,
                "permission": root.permission,
                "backend": root.backend,
                "available": root.available,
                "primary_shell": root.grant_id == self.primary_local_grant_id,
            })
        return rows

    def compat_workspace_path(self) -> str | None:
        """Legacy single-workspace path: explicit primary local only."""
        primary = self.primary_local()
        return primary.path if primary else None


def resolve_active_scope(
    active_ids: list[str] | None,
    *,
    primary_local_directory_id: str | None = None,
) -> ActiveDirectoryScope:
    """Intersect conversation selection with enabled persistent grants.

    Selection cannot escalate permissions. Missing/disabled/revoked grants are
    omitted. Offline remote devices are included as unavailable roots so the
    agent can see them without blocking local roots.
    """
    ordered_ids = []
    seen = set()
    for raw in active_ids or []:
        grant_id = str(raw or "").strip()
        if not grant_id or grant_id in seen:
            continue
        seen.add(grant_id)
        ordered_ids.append(grant_id)

    roots: list[ScopedRoot] = []
    for grant_id in ordered_ids:
        grant = get_allowed_directory(grant_id)
        if grant is None or not grant.enabled:
            continue
        device = get_device(grant.device_id)
        if device is None or not device.enabled:
            continue
        root = _materialize(grant, device)
        roots.append(root)

    primary = str(primary_local_directory_id or "").strip() or None
    if primary:
        match = next((r for r in roots if r.grant_id == primary), None)
        if (
            match is None
            or match.backend != "local"
            or match.permission != "rw"
            or not match.available
        ):
            # Do not silently fall back to another directory or a stale cwd.
            primary = None
    return ActiveDirectoryScope(roots=tuple(roots), primary_local_grant_id=primary)


def _materialize(grant: AllowedDirectory, device) -> ScopedRoot:
    backend = "local" if device.transport == "local" else "sftp"
    if backend == "local":
        path = os.path.realpath(os.path.expanduser(grant.path))
        available = os.path.isdir(path)
        reason = None if available else "local path missing or not a directory"
        # Re-vet sensitivity via existing policy when available.
        if available:
            try:
                from src.tool_execution import vet_workspace
                if vet_workspace(path) != path:
                    available = False
                    reason = "path is not a permitted workspace root"
            except Exception:
                available = False
                reason = "path failed validation"
        return ScopedRoot(
            grant_id=grant.id,
            device_id=device.id,
            device_name=device.display_name,
            display_name=grant.display_name,
            path=path if available else grant.path,
            permission=grant.permission,
            backend=backend,
            path_style=grant.path_style or ("windows" if os.name == "nt" else "posix"),
            available=available,
            unavailable_reason=reason,
        )

    # Remote: availability depends on connectivity; do not fail the whole scope.
    available = True
    reason = None
    try:
        from src.fs_devices.connections import open_sftp_adapter
        adapter = open_sftp_adapter(device)
        info = adapter.stat(grant.path)
        if info.is_symlink or not info.is_dir:
            available = False
            reason = "remote path is not a directory"
    except Exception as exc:
        available = False
        reason = str(exc)[:200]
        logger.info("Remote grant %s unavailable: %s", grant.id, reason)
    return ScopedRoot(
        grant_id=grant.id,
        device_id=device.id,
        device_name=device.display_name,
        display_name=grant.display_name,
        path=grant.path,
        permission=grant.permission,
        backend=backend,
        path_style=grant.path_style or "posix",
        available=available,
        unavailable_reason=reason,
    )


def grants_matching_local_path(path: str) -> list[AllowedDirectory]:
    """Return enabled local grants whose path equals the vetted path."""
    from src.tool_execution import vet_workspace
    vetted = vet_workspace(path)
    if not vetted:
        return []
    out = []
    for grant in list_allowed_directories(device_id=LOCAL_DEVICE_ID):
        if grant.enabled and os.path.realpath(grant.path) == vetted:
            out.append(grant)
    return out
