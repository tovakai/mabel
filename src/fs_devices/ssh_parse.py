"""Parse convenient SSH connection strings into structured device settings.

Accepts familiar input such as ``ssh tovakai@100.x.x.x`` or ``ssh zenbook``.
Never executes shell commands. Unsupported shell syntax is rejected.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass


_UNSAFE = re.compile(r"[|;&$`<>(){}\n\r]|\$\(|`")
_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$")
_HOST = re.compile(
    r"^(?:"
    r"(?:\d{1,3}\.){3}\d{1,3}"  # IPv4 / Tailscale
    r"|(?:\[[0-9A-Fa-f:.]+\])"  # bracketed IPv6
    r"|(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*)"  # DNS / MagicDNS
    r")$"
)
_USER = re.compile(r"^[A-Za-z0-9._-]+$")


class SshParseError(ValueError):
    """Raised when the connection string is unsupported or unsafe."""


@dataclass(frozen=True)
class ParsedSshTarget:
    display_name: str
    hostname: str | None
    username: str | None
    port: int
    alias: str | None
    identity_file: str | None = None
    raw: str = ""

    def to_dict(self):
        return asdict(self)


def parse_ssh_connection(text: str) -> ParsedSshTarget:
    """Parse a user-supplied SSH connection string.

    Supported forms (optional leading ``ssh``):
    - ``user@host``
    - ``user@host -p 2222``
    - ``-p 2222 user@host``
    - ``host`` / OpenSSH config alias (``zenbook``)
    - ``-i ~/.ssh/id_ed25519 user@host``
    - ``-o IdentityFile=~/.ssh/id_ed25519 user@host`` (IdentityFile only)

    Rejects pipes, redirections, command substitution, and unknown ``-o`` keys.
    """
    if text is None or not str(text).strip():
        raise SshParseError("SSH connection string is required")
    raw = str(text).strip()
    if _UNSAFE.search(raw):
        raise SshParseError("Unsupported shell syntax in SSH connection string")
    if any(ord(c) < 32 for c in raw):
        raise SshParseError("Control characters are not allowed")

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError as exc:
        raise SshParseError(f"Malformed SSH connection string: {exc}") from exc
    if not tokens:
        raise SshParseError("SSH connection string is required")
    if tokens[0].casefold() == "ssh":
        tokens = tokens[1:]
    if not tokens:
        raise SshParseError("Missing SSH destination")

    port = 22
    identity_file = None
    positionals: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"-p", "-P"}:
            i += 1
            if i >= len(tokens):
                raise SshParseError("Missing port after -p")
            port = _port(tokens[i])
        elif tok.startswith("-p") and len(tok) > 2 and tok[2:].isdigit():
            port = _port(tok[2:])
        elif tok == "-i":
            i += 1
            if i >= len(tokens):
                raise SshParseError("Missing path after -i")
            identity_file = _identity(tokens[i])
        elif tok.startswith("-i") and len(tok) > 2:
            identity_file = _identity(tok[2:])
        elif tok == "-o":
            i += 1
            if i >= len(tokens):
                raise SshParseError("Missing value after -o")
            identity_file = _option_identity(tokens[i], identity_file)
        elif tok.startswith("-o") and len(tok) > 2:
            identity_file = _option_identity(tok[2:], identity_file)
        elif tok.startswith("-"):
            raise SshParseError(f"Unsupported SSH flag: {tok}")
        else:
            positionals.append(tok)
        i += 1

    if len(positionals) != 1:
        raise SshParseError("Exactly one SSH destination is required")
    dest = positionals[0]
    username = None
    hostname = None
    alias = None
    if "@" in dest:
        user, host = dest.split("@", 1)
        if not user or not host or not _USER.match(user):
            raise SshParseError("Invalid SSH username")
        username = user
        if not _HOST.match(host) and not _ALIAS.match(host):
            raise SshParseError("Invalid SSH hostname")
        # user@single-label is a real hostname/Tailscale MagicDNS short name,
        # not an OpenSSH config alias (aliases are bare tokens without user@).
        hostname = host.strip("[]")
        if "." not in hostname and not _looks_like_ipv4(hostname) and "[" not in host:
            # Keep resolvable as both hostname and config Host lookup key.
            alias = hostname
    elif _is_bare_alias(dest):
        # Undotted bare token → OpenSSH config Host alias (e.g. `ssh zenbook`).
        alias = dest
        hostname = dest
    elif _HOST.match(dest):
        hostname = dest.strip("[]")
    else:
        raise SshParseError("Invalid SSH destination")

    display = alias or (f"{username}@{hostname}" if username else hostname or "ssh")
    return ParsedSshTarget(
        display_name=display,
        hostname=hostname,
        username=username,
        port=port,
        alias=alias,
        identity_file=identity_file,
        raw=raw,
    )


def _looks_like_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _is_bare_alias(value: str) -> bool:
    """OpenSSH Host aliases are undotted labels (not IPs / FQDNs)."""
    if not _ALIAS.match(value):
        return False
    if "." in value or _looks_like_ipv4(value) or value.startswith("["):
        return False
    return True


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SshParseError("SSH port must be an integer") from exc
    if not (1 <= port <= 65535):
        raise SshParseError("SSH port out of range")
    return port


def _identity(value: str) -> str:
    path = str(value or "").strip()
    if not path or _UNSAFE.search(path) or any(ord(c) < 32 for c in path):
        raise SshParseError("Invalid identity file path")
    return path


def _option_identity(option: str, current: str | None) -> str | None:
    if "=" not in option:
        raise SshParseError("Unsupported -o option (expected Key=Value)")
    key, value = option.split("=", 1)
    if key.strip().casefold() != "identityfile":
        raise SshParseError(
            f"Unsupported OpenSSH -o option '{key.strip()}'; only IdentityFile is accepted"
        )
    return _identity(value.strip())
