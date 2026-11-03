"""Resolve an Obsidian vault without borrowing an authorization root.

Authorization scope, process working directory, and the vault target are
separate. An empty tool path must not become the first allowed directory or a
broad parent such as a user profile. Explicit paths stay explicit and still
have to pass the existing resource binding checks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class VaultTargetError(ValueError):
    code = "vault_unconfigured"

    def __init__(self, message: str):
        super().__init__(message)


def persisted_vault_path() -> str | None:
    """Return a configured vault path, or None when nothing is configured.

    Reads ``MABEL_VAULT_ROOT`` and, when present, ``data/vault_target.json``.
    Does not create, rewrite, or delete that file, and does not touch the
    directory registry or an existing index.
    """
    env = os.environ.get("MABEL_VAULT_ROOT", "").strip()
    if env:
        return os.path.realpath(os.path.expanduser(env))
    try:
        from src.constants import DATA_DIR
        path = Path(DATA_DIR) / "vault_target.json"
    except Exception:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("path") if isinstance(data, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    return os.path.realpath(os.path.expanduser(raw.strip()))


def require_configured_vault() -> str:
    """Vault target for an omitted path. Never an authorization root."""
    configured = persisted_vault_path()
    if configured and os.path.isdir(configured):
        return configured
    raise VaultTargetError(
        "No Obsidian vault is configured. Pass an explicit vault path, or set "
        "MABEL_VAULT_ROOT / data/vault_target.json. The authorization root was "
        "not used as the vault target, and the index was not rebuilt."
    )
