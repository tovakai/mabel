"""Durable device and allowed-directory registry.

Persisted at ``data/fs_registry.json`` with atomic writes, ``0o600`` permissions,
and cross-thread/process locking via :func:`core.atomic_io.store_transaction`.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Iterator

from core.atomic_io import atomic_write_json, store_transaction
from src.constants import FS_REGISTRY_FILE, SSH_KNOWN_HOSTS_FILE

logger = logging.getLogger(__name__)

LOCAL_DEVICE_ID = "device:local"
REGISTRY_VERSION = 1
_KNOWN_HOSTS_FILE = SSH_KNOWN_HOSTS_FILE

_PERMISSIONS = frozenset({"ro", "rw"})
_TRANSPORTS = frozenset({"local", "ssh"})


@dataclass
class Device:
    id: str
    display_name: str
    transport: str
    hostname: str | None = None
    username: str | None = None
    port: int = 22
    alias: str | None = None
    identity_file: str | None = None
    builtin: bool = False
    enabled: bool = True
    last_status: str = "unknown"  # unknown | online | offline | error | auth_required
    last_error: str | None = None
    host_key_fingerprint: str | None = None

    def public_dict(self) -> dict:
        """Safe for API responses — never includes secrets (none are stored)."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "transport": self.transport,
            "hostname": self.hostname,
            "username": self.username,
            "port": self.port,
            "alias": self.alias,
            "identity_file": self.identity_file,
            "builtin": self.builtin,
            "enabled": self.enabled,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "host_key_fingerprint": self.host_key_fingerprint,
        }


@dataclass
class AllowedDirectory:
    id: str
    device_id: str
    display_name: str
    path: str
    permission: str = "rw"  # ro | rw
    enabled: bool = True
    path_style: str = "posix"  # posix | windows

    def public_dict(self) -> dict:
        return asdict(self)


@dataclass
class RegistryState:
    version: int = REGISTRY_VERSION
    devices: dict[str, Device] = field(default_factory=dict)
    grants: dict[str, AllowedDirectory] = field(default_factory=dict)
    # pending host keys: device_id -> {hostname, key_type, fingerprint, base64}
    pending_host_keys: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "devices": {k: asdict(v) for k, v in self.devices.items()},
            "grants": {k: asdict(v) for k, v in self.grants.items()},
            "pending_host_keys": deepcopy(self.pending_host_keys),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "RegistryState":
        raw = data if isinstance(data, dict) else {}
        devices = {}
        for key, value in (raw.get("devices") or {}).items():
            if not isinstance(value, dict):
                continue
            devices[str(key)] = Device(**{**_device_defaults(), **value, "id": str(key)})
        grants = {}
        for key, value in (raw.get("grants") or {}).items():
            if not isinstance(value, dict):
                continue
            grants[str(key)] = AllowedDirectory(
                **{**_grant_defaults(), **value, "id": str(key)}
            )
        pending = raw.get("pending_host_keys") if isinstance(raw.get("pending_host_keys"), dict) else {}
        return cls(
            version=int(raw.get("version") or REGISTRY_VERSION),
            devices=devices,
            grants=grants,
            pending_host_keys=dict(pending),
        )


def _device_defaults() -> dict:
    return {
        "display_name": "Device",
        "transport": "ssh",
        "hostname": None,
        "username": None,
        "port": 22,
        "alias": None,
        "identity_file": None,
        "builtin": False,
        "enabled": True,
        "last_status": "unknown",
        "last_error": None,
        "host_key_fingerprint": None,
    }


def _grant_defaults() -> dict:
    return {
        "device_id": LOCAL_DEVICE_ID,
        "display_name": "Directory",
        "path": "",
        "permission": "rw",
        "enabled": True,
        "path_style": "posix",
    }


def registry_path() -> str:
    return FS_REGISTRY_FILE


def known_hosts_path() -> str:
    return _KNOWN_HOSTS_FILE


def _empty_with_local() -> RegistryState:
    state = RegistryState()
    state.devices[LOCAL_DEVICE_ID] = Device(
        id=LOCAL_DEVICE_ID,
        display_name="This desktop (local)",
        transport="local",
        builtin=True,
        enabled=True,
        last_status="online",
    )
    return state


def _chmod_private(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_unlocked() -> RegistryState:
    path = registry_path()
    if not os.path.isfile(path):
        return _empty_with_local()
    try:
        import json
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to read fs registry; starting fresh local device: %s", exc)
        return _empty_with_local()
    state = RegistryState.from_dict(data)
    if LOCAL_DEVICE_ID not in state.devices:
        state.devices[LOCAL_DEVICE_ID] = _empty_with_local().devices[LOCAL_DEVICE_ID]
    return state


def _write_unlocked(state: RegistryState) -> None:
    path = registry_path()
    atomic_write_json(path, state.to_dict(), indent=2)
    _chmod_private(path)


@store_transaction(registry_path)
def load_registry() -> RegistryState:
    """Load registry under the store lock (snapshot copy for callers)."""
    return deepcopy(_read_unlocked())


@contextmanager
def mutate_registry() -> Iterator[RegistryState]:
    """Exclusive read/modify/write of the registry."""
    path = os.path.abspath(registry_path()) + ".lock"
    from core.atomic_io import _STORE_LOCKS, _STORE_LOCKS_GUARD
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.setdefault(path, threading.RLock())
    with lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                state = _read_unlocked()
                yield state
                _write_unlocked(state)
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def ensure_local_device() -> Device:
    with mutate_registry() as state:
        device = state.devices.get(LOCAL_DEVICE_ID)
        if device is None:
            device = _empty_with_local().devices[LOCAL_DEVICE_ID]
            state.devices[LOCAL_DEVICE_ID] = device
        return deepcopy(device)


def list_devices() -> list[Device]:
    state = load_registry()
    ensure_local_device()
    state = load_registry()
    return [deepcopy(d) for d in state.devices.values()]


def get_device(device_id: str) -> Device | None:
    state = load_registry()
    device = state.devices.get(str(device_id or ""))
    return deepcopy(device) if device else None


def upsert_device(device: Device) -> Device:
    if device.transport not in _TRANSPORTS:
        raise ValueError("Unsupported device transport")
    if device.transport == "local" and device.id != LOCAL_DEVICE_ID:
        raise ValueError("Only the builtin local device may use transport=local")
    if device.id == LOCAL_DEVICE_ID:
        device.builtin = True
        device.transport = "local"
    with mutate_registry() as state:
        if device.id == LOCAL_DEVICE_ID and LOCAL_DEVICE_ID in state.devices:
            existing = state.devices[LOCAL_DEVICE_ID]
            # Allow status updates on the builtin device; preserve identity.
            existing.display_name = device.display_name or existing.display_name
            existing.last_status = device.last_status or existing.last_status
            existing.last_error = device.last_error
            existing.enabled = True
            state.devices[LOCAL_DEVICE_ID] = existing
            return deepcopy(existing)
        state.devices[device.id] = device
        return deepcopy(device)


def remove_device(device_id: str, *, remove_grants: bool = False) -> dict:
    device_id = str(device_id or "")
    if device_id == LOCAL_DEVICE_ID:
        raise ValueError("Cannot remove the builtin local device")
    with mutate_registry() as state:
        if device_id not in state.devices:
            raise KeyError(device_id)
        dependents = [g for g in state.grants.values() if g.device_id == device_id]
        if dependents and not remove_grants:
            return {
                "removed": False,
                "dependent_grants": [g.public_dict() for g in dependents],
            }
        del state.devices[device_id]
        removed_grants = []
        if remove_grants:
            for grant in list(state.grants.values()):
                if grant.device_id == device_id:
                    removed_grants.append(grant.id)
                    del state.grants[grant.id]
        state.pending_host_keys.pop(device_id, None)
        return {"removed": True, "removed_grant_ids": removed_grants}


def list_allowed_directories(*, device_id: str | None = None) -> list[AllowedDirectory]:
    state = load_registry()
    grants = list(state.grants.values())
    if device_id:
        grants = [g for g in grants if g.device_id == device_id]
    return [deepcopy(g) for g in grants]


def get_allowed_directory(grant_id: str) -> AllowedDirectory | None:
    state = load_registry()
    grant = state.grants.get(str(grant_id or ""))
    return deepcopy(grant) if grant else None


def upsert_allowed_directory(grant: AllowedDirectory) -> AllowedDirectory:
    if grant.permission not in _PERMISSIONS:
        raise ValueError("permission must be 'ro' or 'rw'")
    if not grant.path or "\0" in grant.path:
        raise ValueError("Invalid directory path")
    with mutate_registry() as state:
        if grant.device_id not in state.devices:
            raise ValueError("Unknown device_id")
        if not grant.id:
            grant.id = f"grant:{uuid.uuid4().hex}"
        state.grants[grant.id] = grant
        return deepcopy(grant)


def remove_allowed_directory(grant_id: str) -> bool:
    with mutate_registry() as state:
        if grant_id not in state.grants:
            return False
        del state.grants[grant_id]
        return True


def migrate_trusted_workspace_grants(*, default_path: str | None = None,
                                      session_cwds: list[str] | None = None) -> list[AllowedDirectory]:
    """Create local grants only from previously trusted persisted configuration.

    Trusted sources: ``ODYSSEUS_WORKSPACE_DEFAULT`` and existing session ``cwd``
    values. Never call this with ordinary chat FormData workspace paths.
    """
    from src.tool_execution import vet_workspace

    candidates: list[str] = []
    env_default = (default_path if default_path is not None
                   else os.environ.get("ODYSSEUS_WORKSPACE_DEFAULT", "")).strip()
    if env_default:
        candidates.append(env_default)
    for cwd in session_cwds or []:
        value = str(cwd or "").strip()
        if value:
            candidates.append(value)

    created: list[AllowedDirectory] = []
    with mutate_registry() as state:
        if LOCAL_DEVICE_ID not in state.devices:
            state.devices[LOCAL_DEVICE_ID] = _empty_with_local().devices[LOCAL_DEVICE_ID]
        existing_paths = {
            os.path.realpath(g.path)
            for g in state.grants.values()
            if g.device_id == LOCAL_DEVICE_ID and g.path
        }
        for raw in candidates:
            vetted = vet_workspace(raw)
            if not vetted or vetted in existing_paths:
                continue
            grant = AllowedDirectory(
                id=f"grant:{uuid.uuid4().hex}",
                device_id=LOCAL_DEVICE_ID,
                display_name=os.path.basename(vetted.rstrip("/\\")) or vetted,
                path=vetted,
                permission="rw",
                enabled=True,
                path_style="windows" if os.name == "nt" else "posix",
            )
            state.grants[grant.id] = grant
            existing_paths.add(vetted)
            created.append(deepcopy(grant))
    return created


def find_local_grant_by_path(path: str) -> AllowedDirectory | None:
    """Match an already-authorized local grant; never creates one."""
    from src.tool_execution import vet_workspace
    vetted = vet_workspace(path)
    if not vetted:
        return None
    for grant in list_allowed_directories(device_id=LOCAL_DEVICE_ID):
        if grant.enabled and os.path.realpath(grant.path) == vetted:
            return grant
    return None
