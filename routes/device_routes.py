"""Device manager and allowed-directory APIs (admin / single-user)."""

from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.auth_helpers import get_current_user
from src.fs_devices.adapters import LocalAdapter
from src.fs_devices.connections import (
    accept_host_key,
    close_device_connection,
    open_sftp_adapter,
    test_device,
)
from src.fs_devices.registry import (
    LOCAL_DEVICE_ID,
    AllowedDirectory,
    Device,
    ensure_local_device,
    get_allowed_directory,
    get_device,
    list_allowed_directories,
    list_devices,
    migrate_trusted_workspace_grants,
    remove_allowed_directory,
    remove_device,
    upsert_allowed_directory,
    upsert_device,
)
from src.fs_devices.ssh_parse import SshParseError, parse_ssh_connection
from src.tool_execution import vet_workspace
from src.tool_security import owner_is_admin_or_single_user

_MAX_BROWSE = 500


def _require_admin(request: Request) -> str:
    owner = get_current_user(request)
    if not owner_is_admin_or_single_user(owner):
        raise HTTPException(status_code=403, detail="Device management is admin-only")
    return owner


class ParseSshBody(BaseModel):
    connection: str = Field(..., description="Familiar ssh user@host / alias string")


class DeviceBody(BaseModel):
    display_name: str | None = None
    transport: str = "ssh"
    hostname: str | None = None
    username: str | None = None
    port: int = 22
    alias: str | None = None
    identity_file: str | None = None
    connection: str | None = None  # optional ssh line to parse
    enabled: bool = True


class HostKeyBody(BaseModel):
    accept: bool = True
    hostname: str | None = None
    key_type: str | None = None
    base64_key: str | None = None


class GrantBody(BaseModel):
    device_id: str
    display_name: str
    path: str
    permission: str = "rw"
    enabled: bool = True
    path_style: str | None = None


class GrantPatchBody(BaseModel):
    display_name: str | None = None
    permission: str | None = None
    enabled: bool | None = None
    path: str | None = None


def setup_device_routes():
    router = APIRouter(tags=["devices"])

    @router.get("/api/devices")
    def api_list_devices(request: Request):
        _require_admin(request)
        ensure_local_device()
        # Opportunistic trusted migration (env default + nothing else here).
        migrate_trusted_workspace_grants()
        return {"devices": [d.public_dict() for d in list_devices()]}

    @router.post("/api/devices/parse-ssh")
    def api_parse_ssh(request: Request, body: ParseSshBody):
        _require_admin(request)
        try:
            parsed = parse_ssh_connection(body.connection)
        except SshParseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return parsed.to_dict()

    @router.post("/api/devices")
    def api_create_device(request: Request, body: DeviceBody):
        _require_admin(request)
        data = body.model_dump()
        if body.connection:
            try:
                parsed = parse_ssh_connection(body.connection)
            except SshParseError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            data.update({
                "display_name": body.display_name or parsed.display_name,
                "hostname": body.hostname or parsed.hostname,
                "username": body.username or parsed.username,
                "port": body.port if body.port != 22 else parsed.port,
                "alias": body.alias or parsed.alias,
                "identity_file": body.identity_file or parsed.identity_file,
                "transport": "ssh",
            })
        if data.get("transport") == "local":
            raise HTTPException(400, "Local device is builtin; cannot create another")
        device = Device(
            id=f"device:{uuid.uuid4().hex}",
            display_name=(data.get("display_name") or data.get("alias") or data.get("hostname") or "SSH device"),
            transport="ssh",
            hostname=data.get("hostname"),
            username=data.get("username"),
            port=int(data.get("port") or 22),
            alias=data.get("alias"),
            identity_file=data.get("identity_file"),
            enabled=bool(data.get("enabled", True)),
        )
        upsert_device(device)
        return device.public_dict()

    @router.patch("/api/devices/{device_id}")
    def api_patch_device(request: Request, device_id: str, body: DeviceBody):
        _require_admin(request)
        existing = get_device(device_id)
        if existing is None:
            raise HTTPException(404, "Device not found")
        if existing.builtin and existing.transport == "local":
            if body.display_name:
                existing.display_name = body.display_name
            upsert_device(existing)
            return existing.public_dict()
        if body.connection:
            try:
                parsed = parse_ssh_connection(body.connection)
            except SshParseError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            existing.hostname = parsed.hostname
            existing.username = parsed.username or existing.username
            existing.port = parsed.port
            existing.alias = parsed.alias
            existing.identity_file = parsed.identity_file or existing.identity_file
            if body.display_name is None:
                existing.display_name = parsed.display_name
        if body.display_name is not None:
            existing.display_name = body.display_name
        if body.hostname is not None:
            existing.hostname = body.hostname
        if body.username is not None:
            existing.username = body.username
        if body.port is not None:
            existing.port = int(body.port)
        if body.alias is not None:
            existing.alias = body.alias
        if body.identity_file is not None:
            existing.identity_file = body.identity_file
        existing.enabled = bool(body.enabled)
        close_device_connection(existing.id)
        upsert_device(existing)
        return existing.public_dict()

    @router.delete("/api/devices/{device_id}")
    def api_delete_device(request: Request, device_id: str, remove_grants: bool = Query(False)):
        _require_admin(request)
        try:
            result = remove_device(device_id, remove_grants=remove_grants)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError:
            raise HTTPException(404, "Device not found") from None
        close_device_connection(device_id)
        if not result.get("removed"):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Device has dependent directory grants",
                    "dependent_grants": result.get("dependent_grants") or [],
                },
            )
        return result

    @router.post("/api/devices/{device_id}/test")
    def api_test_device(request: Request, device_id: str):
        _require_admin(request)
        device = get_device(device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        if device.transport == "local":
            device.last_status = "online"
            device.last_error = None
            upsert_device(device)
            return {"ok": True, "status": "online", "path_style": "posix" if os.name != "nt" else "windows"}
        close_device_connection(device.id)
        return test_device(device).public_dict()

    @router.post("/api/devices/{device_id}/host-key")
    def api_host_key(request: Request, device_id: str, body: HostKeyBody):
        _require_admin(request)
        device = get_device(device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        if not body.accept:
            from src.fs_devices.registry import mutate_registry
            with mutate_registry() as state:
                state.pending_host_keys.pop(device_id, None)
            return {"ok": True, "accepted": False}
        from src.fs_devices.registry import load_registry
        pending = (load_registry().pending_host_keys or {}).get(device_id) or {}
        hostname = body.hostname or pending.get("hostname")
        key_type = body.key_type or pending.get("key_type")
        base64_key = body.base64_key or pending.get("base64_key")
        if not (hostname and key_type and base64_key):
            raise HTTPException(400, "Missing host key material to accept")
        try:
            accept_host_key(
                device_id=device_id,
                hostname=hostname,
                key_type=key_type,
                base64_key=base64_key,
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True, "accepted": True}

    @router.get("/api/devices/{device_id}/browse")
    def api_browse(request: Request, device_id: str, path: str = Query(default="")):
        _require_admin(request)
        device = get_device(device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        try:
            if device.transport == "local":
                adapter = LocalAdapter(device.id)
                target = os.path.realpath(os.path.expanduser(path.strip() or "~"))
                if not os.path.isdir(target):
                    target = os.path.realpath(os.path.expanduser("~"))
                dirs, truncated = adapter.listdir(target, max_entries=_MAX_BROWSE)
                selectable = vet_workspace(target) is not None
                parent = adapter.parent(target)
                return {
                    "device_id": device.id,
                    "path": target,
                    "parent": parent,
                    "dirs": [{"name": d.name, "path": d.path} for d in dirs],
                    "truncated": truncated,
                    "selectable": selectable,
                    "path_style": adapter.path_style,
                }
            adapter = open_sftp_adapter(device)
            home = adapter.home
            target = path.strip() or home
            try:
                info = adapter.stat(target)
                if not info.is_dir or info.is_symlink:
                    target = home
            except Exception:
                target = home
            dirs, truncated = adapter.listdir(target, max_entries=_MAX_BROWSE)
            parent = adapter.parent(target)
            # Remote selectable: must be a real directory; grant creation validates further.
            selectable = True
            try:
                info = adapter.stat(target)
                selectable = bool(info.is_dir and not info.is_symlink)
            except Exception:
                selectable = False
            return {
                "device_id": device.id,
                "path": target,
                "parent": parent,
                "dirs": [{"name": d.name, "path": d.path} for d in dirs],
                "truncated": truncated,
                "selectable": selectable,
                "path_style": adapter.path_style,
            }
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Browse failed: {exc}") from exc

    @router.get("/api/allowed-directories")
    def api_list_grants(request: Request, device_id: str | None = None):
        _require_admin(request)
        ensure_local_device()
        migrate_trusted_workspace_grants()
        grants = list_allowed_directories(device_id=device_id)
        devices = {d.id: d for d in list_devices()}
        out = []
        for grant in grants:
            payload = grant.public_dict()
            device = devices.get(grant.device_id)
            payload["device_name"] = device.display_name if device else grant.device_id
            payload["device_transport"] = device.transport if device else None
            out.append(payload)
        return {"directories": out}

    @router.post("/api/allowed-directories")
    def api_create_grant(request: Request, body: GrantBody):
        _require_admin(request)
        device = get_device(body.device_id)
        if device is None:
            raise HTTPException(404, "Device not found")
        if body.permission not in {"ro", "rw"}:
            raise HTTPException(400, "permission must be ro or rw")
        path = body.path.strip()
        path_style = body.path_style
        if device.transport == "local":
            vetted = vet_workspace(path)
            if not vetted:
                raise HTTPException(400, "Path is not a permitted local directory")
            path = vetted
            path_style = path_style or ("windows" if os.name == "nt" else "posix")
        else:
            try:
                adapter = open_sftp_adapter(device)
                info = adapter.stat(path)
                if info.is_symlink or not info.is_dir:
                    raise HTTPException(400, "Remote path must be a directory (not a symlink)")
                path = info.path
                path_style = path_style or adapter.path_style
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(502, f"Cannot verify remote path: {exc}") from exc
        grant = AllowedDirectory(
            id=f"grant:{uuid.uuid4().hex}",
            device_id=device.id,
            display_name=body.display_name.strip() or os.path.basename(path.rstrip("/\\")) or path,
            path=path,
            permission=body.permission,
            enabled=bool(body.enabled),
            path_style=path_style or "posix",
        )
        upsert_allowed_directory(grant)
        return grant.public_dict()

    @router.patch("/api/allowed-directories/{grant_id}")
    def api_patch_grant(request: Request, grant_id: str, body: GrantPatchBody):
        _require_admin(request)
        grant = get_allowed_directory(grant_id)
        if grant is None:
            raise HTTPException(404, "Directory grant not found")
        if body.display_name is not None:
            grant.display_name = body.display_name.strip() or grant.display_name
        if body.permission is not None:
            if body.permission not in {"ro", "rw"}:
                raise HTTPException(400, "permission must be ro or rw")
            grant.permission = body.permission
        if body.enabled is not None:
            grant.enabled = bool(body.enabled)
        if body.path is not None:
            raise HTTPException(400, "Change device path by removing and re-adding the grant")
        upsert_allowed_directory(grant)
        return grant.public_dict()

    @router.delete("/api/allowed-directories/{grant_id}")
    def api_delete_grant(request: Request, grant_id: str):
        _require_admin(request)
        if not remove_allowed_directory(grant_id):
            raise HTTPException(404, "Directory grant not found")
        return {"removed": True, "id": grant_id}

    return router
