"""SSH/SFTP connection manager with explicit host-key verification."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from src.fs_devices.adapters import SftpAdapter, infer_remote_path_style
from src.fs_devices.registry import (
    Device,
    known_hosts_path,
    mutate_registry,
    upsert_device,
)

logger = logging.getLogger(__name__)

_PASSWORD_GUIDANCE = (
    "This host requires password authentication, which Mabel does not store. "
    "Set up key-based login from the desktop that runs Mabel: "
    "1) ssh-keygen -t ed25519 if needed; "
    "2) ssh-copy-id user@host (or add the public key under Windows OpenSSH "
    "authorized_keys); "
    "3) optionally add an IdentityFile / Host alias in ~/.ssh/config; "
    "4) ensure ssh-agent has the key loaded (ssh-add). "
    "Then Test Connection again."
)


class HostKeyRequired(Exception):
    def __init__(self, *, hostname: str, key_type: str, fingerprint: str, base64_key: str):
        super().__init__("SSH host key is not trusted yet")
        self.hostname = hostname
        self.key_type = key_type
        self.fingerprint = fingerprint
        self.base64_key = base64_key

    def public_dict(self):
        return {
            "error": "host_key_required",
            "hostname": self.hostname,
            "key_type": self.key_type,
            "fingerprint": self.fingerprint,
            "guidance": (
                "The remote host key is unknown or changed. Review the fingerprint, "
                "then accept it explicitly before connecting."
            ),
        }


class AuthRequired(Exception):
    def __init__(self, message: str | None = None):
        super().__init__(message or _PASSWORD_GUIDANCE)
        self.guidance = _PASSWORD_GUIDANCE

    def public_dict(self):
        return {"error": "auth_required", "guidance": self.guidance}


@dataclass
class ConnectionResult:
    ok: bool
    status: str
    error: str | None = None
    guidance: str | None = None
    host_key: dict | None = None
    path_style: str | None = None
    home: str | None = None

    def public_dict(self):
        return {
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "guidance": self.guidance,
            "host_key": self.host_key,
            "path_style": self.path_style,
            "home": self.home,
        }


_pool_lock = threading.RLock()
# device_id -> (client, sftp, path_style, home)
_pool: dict[str, tuple[Any, Any, str, str]] = {}


def _ensure_paramiko():
    try:
        import paramiko  # noqa: F401
        return __import__("paramiko")
    except ImportError as exc:
        raise RuntimeError(
            "paramiko is required for SSH devices; install it in the Mabel environment"
        ) from exc


def _load_ssh_config(paramiko, hostname: str | None, alias: str | None) -> dict:
    """Resolve OpenSSH config for an alias/hostname subset Paramiko supports."""
    path = os.path.expanduser("~/.ssh/config")
    config = {
        "hostname": hostname,
        "user": None,
        "port": None,
        "identityfile": None,
    }
    if not os.path.isfile(path):
        return config
    try:
        parsed = paramiko.SSHConfig.from_path(path)
    except Exception as exc:
        logger.warning("Failed to parse OpenSSH config: %s", exc)
        return config
    lookup = alias or hostname or ""
    if not lookup:
        return config
    try:
        entry = parsed.lookup(lookup)
    except Exception:
        return config
    if entry.get("hostname"):
        config["hostname"] = entry["hostname"]
    if entry.get("user"):
        config["user"] = entry["user"]
    if entry.get("port"):
        try:
            config["port"] = int(entry["port"])
        except (TypeError, ValueError):
            pass
    identities = entry.get("identityfile") or []
    if isinstance(identities, str):
        identities = [identities]
    if identities:
        config["identityfile"] = os.path.expanduser(identities[0])
    return config


def _known_hosts():
    paramiko = _ensure_paramiko()
    path = known_hosts_path()
    host_keys = paramiko.HostKeys()
    if os.path.isfile(path):
        try:
            host_keys.load(path)
        except Exception as exc:
            logger.warning("Failed to load known_hosts: %s", exc)
    # Also consult the user's OpenSSH known_hosts when present.
    user_kh = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.isfile(user_kh):
        try:
            host_keys.load(user_kh)
        except Exception:
            pass
    return host_keys


def accept_host_key(*, device_id: str, hostname: str, key_type: str, base64_key: str) -> None:
    paramiko = _ensure_paramiko()
    path = known_hosts_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    host_keys = paramiko.HostKeys()
    if os.path.isfile(path):
        try:
            host_keys.load(path)
        except Exception:
            pass
    key = paramiko.RSAKey(data=__import__("base64").b64decode(base64_key)) if key_type == "ssh-rsa" else None
    # Prefer from_private_key-style constructors by type name.
    key_class = {
        "ssh-rsa": paramiko.RSAKey,
        "ssh-ed25519": getattr(paramiko, "Ed25519Key", None),
        "ecdsa-sha2-nistp256": getattr(paramiko, "ECDSAKey", None),
        "ecdsa-sha2-nistp384": getattr(paramiko, "ECDSAKey", None),
        "ecdsa-sha2-nistp521": getattr(paramiko, "ECDSAKey", None),
    }.get(key_type)
    if key_class is None:
        raise ValueError(f"Unsupported host key type: {key_type}")
    key = key_class(data=__import__("base64").b64decode(base64_key))
    host_keys.add(hostname, key_type, key)
    host_keys.save(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    with mutate_registry() as state:
        state.pending_host_keys.pop(device_id, None)
        device = state.devices.get(device_id)
        if device is not None:
            device.host_key_fingerprint = _fingerprint(key)
            device.last_error = None


def _fingerprint(key) -> str:
    # Prefer OpenSSH-style SHA256 fingerprint when available.
    try:
        return key.get_fingerprint_sha256()  # type: ignore[attr-defined]
    except Exception:
        digest = key.get_fingerprint()
        return "MD5:" + ":".join(f"{b:02x}" for b in digest)


def _reject_password_auth(transport) -> None:
    """Refuse to send passwords; surface guidance instead."""
    try:
        allowed = set(transport.auth_none(transport.get_username() or "") or [])
    except Exception as exc:
        # auth_none often fails; inspect exception text / preferred auths.
        text = str(exc).casefold()
        if "password" in text and "publickey" not in text:
            raise AuthRequired() from exc
        return
    if allowed and "publickey" not in allowed and "password" in allowed:
        raise AuthRequired()


def connect_device(device: Device, *, timeout: float = 15.0):
    """Open an SSH+SFTP session for ``device``. Returns (client, sftp, path_style, home)."""
    if device.transport != "ssh":
        raise ValueError("connect_device only supports ssh transport")
    paramiko = _ensure_paramiko()
    cfg = _load_ssh_config(paramiko, device.hostname, device.alias)
    hostname = cfg["hostname"] or device.hostname or device.alias
    if not hostname:
        raise ValueError("Device hostname or SSH alias is required")
    username = device.username or cfg["user"] or os.environ.get("USER") or os.environ.get("USERNAME")
    if not username:
        raise ValueError("SSH username is required")
    port = int(device.port or cfg["port"] or 22)
    identity = device.identity_file or cfg["identityfile"]
    if identity:
        identity = os.path.expanduser(identity)

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # Merge our durable known_hosts.
    for host, keys in _known_hosts().items():
        for name, key in keys.items():
            client.get_host_keys().add(host, name, key)

    class _Policy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):  # noqa: ANN001
            raise HostKeyRequired(
                hostname=hostname,
                key_type=key.get_name(),
                fingerprint=_fingerprint(key),
                base64_key=__import__("base64").b64encode(key.asbytes()).decode("ascii"),
            )

    client.set_missing_host_key_policy(_Policy())

    connect_kwargs = {
        "hostname": hostname,
        "port": port,
        "username": username,
        "timeout": timeout,
        "allow_agent": True,
        "look_for_keys": True,
        "password": None,  # never send a password
    }
    if identity and os.path.isfile(identity):
        connect_kwargs["key_filename"] = identity

    try:
        client.connect(**connect_kwargs)
    except HostKeyRequired:
        raise
    except paramiko.AuthenticationException as exc:
        raise AuthRequired() from exc
    except Exception as exc:
        text = str(exc).casefold()
        if "password" in text and "auth" in text:
            raise AuthRequired() from exc
        if "host key" in text or "unknown server" in text:
            # Changed host key surfaces as SSHException from paramiko.
            raise
        raise

    sftp = client.open_sftp()
    try:
        home = sftp.normalize(".")
    except Exception:
        home = "/"
    path_style = infer_remote_path_style(sftp, home)
    return client, sftp, path_style, home


def test_device(device: Device) -> ConnectionResult:
    try:
        client, sftp, path_style, home = connect_device(device)
        try:
            sftp.listdir(home or ".")
        finally:
            try:
                sftp.close()
            except Exception:
                pass
            try:
                client.close()
            except Exception:
                pass
        device.last_status = "online"
        device.last_error = None
        upsert_device(device)
        return ConnectionResult(ok=True, status="online", path_style=path_style, home=home)
    except HostKeyRequired as exc:
        with mutate_registry() as state:
            state.pending_host_keys[device.id] = {
                "hostname": exc.hostname,
                "key_type": exc.key_type,
                "fingerprint": exc.fingerprint,
                "base64_key": exc.base64_key,
            }
            if device.id in state.devices:
                state.devices[device.id].last_status = "error"
                state.devices[device.id].last_error = "host_key_required"
        return ConnectionResult(
            ok=False,
            status="error",
            error="host_key_required",
            guidance=exc.public_dict()["guidance"],
            host_key=exc.public_dict(),
        )
    except AuthRequired as exc:
        device.last_status = "auth_required"
        device.last_error = "auth_required"
        upsert_device(device)
        return ConnectionResult(
            ok=False,
            status="auth_required",
            error="auth_required",
            guidance=exc.guidance,
        )
    except Exception as exc:
        device.last_status = "error"
        device.last_error = str(exc)[:300]
        upsert_device(device)
        return ConnectionResult(ok=False, status="error", error=str(exc)[:300])


def open_sftp_adapter(device: Device) -> SftpAdapter:
    with _pool_lock:
        cached = _pool.get(device.id)
        if cached is not None:
            _client, sftp, path_style, home = cached
            return SftpAdapter(device.id, sftp, path_style=path_style, home=home)
        client, sftp, path_style, home = connect_device(device)
        _pool[device.id] = (client, sftp, path_style, home)
        return SftpAdapter(device.id, sftp, path_style=path_style, home=home)


def close_device_connection(device_id: str) -> None:
    with _pool_lock:
        entry = _pool.pop(str(device_id), None)
        if not entry:
            return
        client, sftp, _, _ = entry
        try:
            sftp.close()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass


def drop_all_connections() -> None:
    with _pool_lock:
        ids = list(_pool.keys())
    for device_id in ids:
        close_device_connection(device_id)
