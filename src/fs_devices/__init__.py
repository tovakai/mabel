"""Multi-device filesystem registry: devices, directory grants, adapters, scope.

Devices describe where files live. Allowed directories describe what Mabel may
access persistently. Active directories describe what a conversation may use.
Tool authorization remains a separate gate on top of that intersection.
"""

from src.fs_devices.registry import (
    LOCAL_DEVICE_ID,
    AllowedDirectory,
    Device,
    ensure_local_device,
    get_allowed_directory,
    get_device,
    list_allowed_directories,
    list_devices,
    load_registry,
    migrate_trusted_workspace_grants,
    mutate_registry,
    remove_allowed_directory,
    remove_device,
    upsert_allowed_directory,
    upsert_device,
)
from src.fs_devices.scope import (
    ActiveDirectoryScope,
    ScopedRoot,
    resolve_active_scope,
)
from src.fs_devices.ssh_parse import ParsedSshTarget, parse_ssh_connection

__all__ = [
    "LOCAL_DEVICE_ID",
    "AllowedDirectory",
    "ActiveDirectoryScope",
    "Device",
    "ParsedSshTarget",
    "ScopedRoot",
    "ensure_local_device",
    "get_allowed_directory",
    "get_device",
    "list_allowed_directories",
    "list_devices",
    "load_registry",
    "migrate_trusted_workspace_grants",
    "mutate_registry",
    "parse_ssh_connection",
    "remove_allowed_directory",
    "remove_device",
    "resolve_active_scope",
    "upsert_allowed_directory",
    "upsert_device",
]
