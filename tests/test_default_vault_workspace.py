"""Core of Truth / vault target is the default active workspace (opt-out)."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


def test_resolve_default_vault_workspace_binds_configured_vault(tmp_path, monkeypatch):
    import routes.chat_routes as cr
    from src.agent_runtime import vault_target as vt

    vault = tmp_path / "core of truth"
    vault.mkdir()
    monkeypatch.setattr(vt, "persisted_vault_path", lambda: str(vault))
    monkeypatch.setattr(cr, "get_current_user", lambda req: "alice")
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user", lambda owner: True,
    )

    bound, rejected = cr._resolve_default_vault_workspace(object())
    assert rejected == ""
    assert os.path.samefile(bound, vault)

    # Explicit opt-out must not re-bind.
    skipped, rejected = cr._resolve_default_vault_workspace(
        object(), opted_out=True,
    )
    assert skipped == "" and rejected == ""

    # An already-chosen workspace wins.
    other = tmp_path / "other"
    other.mkdir()
    kept, rejected = cr._resolve_default_vault_workspace(
        object(), current_workspace=str(other),
    )
    assert os.path.samefile(kept, other)


def test_interactive_review_seals_web_and_workspace_reads(tmp_path):
    from src.agent_runtime.authority import (
        ExactOperation, create_request_authority,
    )

    auth = create_request_authority(
        "What is Fruit of Grisaia about?",
        owner="alice",
        session_id="s",
        workspace=str(tmp_path),
        interactive_review=True,
        capabilities={"workspace_read", "search_browser"},
    )
    assert auth.permits(ExactOperation.normalize(
        "web_fetch",
        '{"url":"https://en.wikipedia.org/wiki/The_Fruit_of_Grisaia"}',
    ))
    assert auth.permits(ExactOperation.normalize(
        "web_search", "Fruit of Grisaia plot",
    ))
    assert auth.permits(ExactOperation.normalize(
        "read_file", '{"path":"Library/Games/x.md"}',
    ))
    assert not auth.permits(ExactOperation.normalize("bash", "pwd"))
