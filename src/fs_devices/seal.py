"""Seal local active directory grants into RequestAuthority resource roots."""

from __future__ import annotations

from src.agent_runtime.resources import FilesystemRoot
from src.fs_devices.scope import ActiveDirectoryScope


def seal_local_scope_roots(scope: ActiveDirectoryScope | None, *, owner: str = "") -> tuple[FilesystemRoot, ...]:
    """Seal available local roots from the conversation scope.

    Remote (SFTP) roots are enforced through adapters + grant ids, not inode
    seals, because FilesystemRoot identity is local-filesystem based.
    """
    if scope is None:
        return ()
    roots = []
    for item in scope.available_roots:
        if item.backend != "local":
            continue
        try:
            roots.append(FilesystemRoot.seal(item.path, owner=owner))
        except (OSError, ValueError, RuntimeError):
            continue
    return tuple(roots)


def attach_scope_roots(authority, scope: ActiveDirectoryScope | None):
    """Return a copy of ``authority`` with local scope roots sealed in."""
    from dataclasses import replace
    sealed = seal_local_scope_roots(scope, owner=getattr(authority, "owner", "") or "")
    if not sealed:
        # Keep existing roots (legacy single workspace) when scope has no local.
        return authority
    return replace(authority, resource_roots=sealed)
