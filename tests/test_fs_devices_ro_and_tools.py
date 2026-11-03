"""Read-only grant enforcement and multi-root local path resolution."""

import os

import pytest

from src.fs_devices import registry as reg
from src.fs_devices.remote_ops import ReadOnlyGrantError, resolve_scoped_target, require_writable
from src.fs_devices.scope import ActiveDirectoryScope, ScopedRoot, resolve_active_scope
from src.tool_execution import (
    _active_directory_scope,
    _resolve_tool_path_in_scope,
)


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    path = tmp_path / "fs_registry.json"
    monkeypatch.setattr(reg, "registry_path", lambda: str(path))
    monkeypatch.setattr(reg, "known_hosts_path", lambda: str(tmp_path / "ssh_known_hosts"))
    yield path


def test_readonly_require_writable():
    target = {
        "legacy": False,
        "permission": "ro",
        "display_name": "Media",
    }
    with pytest.raises(ReadOnlyGrantError):
        require_writable(target)


def test_resolve_scoped_target_ro(isolated_registry, tmp_path, monkeypatch):
    folder = tmp_path / "media"
    folder.mkdir()
    (folder / "a.txt").write_text("hi")
    reg.ensure_local_device()
    grant = reg.upsert_allowed_directory(reg.AllowedDirectory(
        id="grant:media",
        device_id=reg.LOCAL_DEVICE_ID,
        display_name="Media",
        path=str(folder),
        permission="ro",
    ))
    monkeypatch.setattr(
        "src.tool_execution.vet_workspace",
        lambda raw: os.path.realpath(raw) if os.path.isdir(raw) else None,
    )
    scope = resolve_active_scope([grant.id])
    token = _active_directory_scope.set(scope)
    try:
        target = resolve_scoped_target(str(folder / "a.txt"), grant.id)
        assert target["permission"] == "ro"
        with pytest.raises(ReadOnlyGrantError):
            require_writable(target)
    finally:
        _active_directory_scope.reset(token)


def test_multi_root_path_resolution(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "only_a.txt").write_text("a")
    (b / "only_b.txt").write_text("b")
    scope = ActiveDirectoryScope(
        roots=(
            ScopedRoot(
                grant_id="grant:a",
                device_id="device:local",
                device_name="Desktop",
                display_name="A",
                path=str(a.resolve()),
                permission="rw",
                backend="local",
                path_style="posix",
                available=True,
            ),
            ScopedRoot(
                grant_id="grant:b",
                device_id="device:local",
                device_name="Desktop",
                display_name="B",
                path=str(b.resolve()),
                permission="rw",
                backend="local",
                path_style="posix",
                available=True,
            ),
        ),
        primary_local_grant_id="grant:a",
    )
    resolved = _resolve_tool_path_in_scope(scope, str(a / "only_a.txt"))
    assert os.path.realpath(resolved) == os.path.realpath(str(a / "only_a.txt"))
    resolved_b = _resolve_tool_path_in_scope(scope, str(b / "only_b.txt"))
    assert os.path.realpath(resolved_b) == os.path.realpath(str(b / "only_b.txt"))
    with pytest.raises(ValueError):
        _resolve_tool_path_in_scope(scope, str(tmp_path / "outside.txt"))


def test_empty_scope_errors():
    scope = ActiveDirectoryScope(roots=())
    with pytest.raises(ValueError, match="No active local directories"):
        _resolve_tool_path_in_scope(scope, "/tmp/x")
