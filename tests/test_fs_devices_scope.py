"""Conversation active-directory scope and turn binding rules."""

import os

import pytest

from src.fs_devices import registry as reg
from src.fs_devices.scope import resolve_active_scope
from src.fs_devices.turn_bind import bind_turn_directory_scope, parse_active_directory_ids


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    path = tmp_path / "fs_registry.json"
    monkeypatch.setattr(reg, "registry_path", lambda: str(path))
    monkeypatch.setattr(reg, "known_hosts_path", lambda: str(tmp_path / "ssh_known_hosts"))
    yield path


def _local_grant(tmp_path, name, permission="rw"):
    folder = tmp_path / name
    folder.mkdir()
    reg.ensure_local_device()
    grant = reg.AllowedDirectory(
        id=f"grant:{name}",
        device_id=reg.LOCAL_DEVICE_ID,
        display_name=name,
        path=str(folder),
        permission=permission,
    )
    return reg.upsert_allowed_directory(grant), folder


def test_parse_active_directory_ids_json_and_csv():
    assert parse_active_directory_ids('["a","b"]') == ["a", "b"]
    assert parse_active_directory_ids("a, b, a") == ["a", "b"]
    assert parse_active_directory_ids(None) == []


def test_multi_select_and_primary(isolated_registry, tmp_path, monkeypatch):
    g1, p1 = _local_grant(tmp_path, "vault")
    g2, p2 = _local_grant(tmp_path, "repo")
    monkeypatch.setattr("src.tool_execution.vet_workspace", lambda raw: os.path.realpath(raw) if os.path.isdir(raw) else None)
    scope = resolve_active_scope([g1.id, g2.id], primary_local_directory_id=g1.id)
    assert len(scope.available_roots) == 2
    assert scope.primary_local().grant_id == g1.id
    assert scope.compat_workspace_path() == os.path.realpath(str(p1))


def test_primary_must_be_active_local_rw(isolated_registry, tmp_path, monkeypatch):
    g_ro, _ = _local_grant(tmp_path, "media", permission="ro")
    g_rw, _ = _local_grant(tmp_path, "code", permission="rw")
    monkeypatch.setattr("src.tool_execution.vet_workspace", lambda raw: os.path.realpath(raw) if os.path.isdir(raw) else None)
    # Primary pointing at RO grant is cleared.
    scope = resolve_active_scope([g_ro.id, g_rw.id], primary_local_directory_id=g_ro.id)
    assert scope.primary_local_grant_id is None
    assert scope.compat_workspace_path() is None
    # Primary not in active set is cleared.
    scope2 = resolve_active_scope([g_ro.id], primary_local_directory_id=g_rw.id)
    assert scope2.primary_local_grant_id is None


def test_revoked_grant_dropped(isolated_registry, tmp_path, monkeypatch):
    g1, _ = _local_grant(tmp_path, "alive")
    monkeypatch.setattr("src.tool_execution.vet_workspace", lambda raw: os.path.realpath(raw) if os.path.isdir(raw) else None)
    scope = resolve_active_scope([g1.id, "grant:missing"])
    assert [r.grant_id for r in scope.roots] == [g1.id]


def test_turn_bind_unknown_workspace_rejects_without_creating_grant(
    isolated_registry, tmp_path, monkeypatch
):
    unknown = tmp_path / "unknown"
    unknown.mkdir()
    monkeypatch.setattr(
        "src.tool_execution.vet_workspace",
        lambda raw: os.path.realpath(raw) if os.path.isdir(os.path.expanduser(raw)) else None,
    )
    monkeypatch.setattr(reg, "find_local_grant_by_path", lambda path: None)
    scope, compat, rejected = bind_turn_directory_scope(
        request_workspace_path=str(unknown),
        allow_trusted_migration=False,
    )
    assert rejected == str(unknown)
    assert compat == ""
    assert scope.roots == ()
    assert reg.list_allowed_directories() == []


def test_turn_bind_matching_grant_activates(isolated_registry, tmp_path, monkeypatch):
    g1, folder = _local_grant(tmp_path, "matched")
    monkeypatch.setattr(
        "src.tool_execution.vet_workspace",
        lambda raw: os.path.realpath(str(folder)) if str(folder) in os.path.realpath(os.path.expanduser(raw or "")) else None,
    )

    def _find(path):
        if os.path.realpath(path) == os.path.realpath(str(folder)):
            return g1
        return None

    monkeypatch.setattr(reg, "find_local_grant_by_path", _find)
    scope, compat, rejected = bind_turn_directory_scope(
        request_workspace_path=str(folder),
        allow_trusted_migration=False,
    )
    assert rejected == ""
    assert any(r.grant_id == g1.id for r in scope.roots)
