"""Device/grant registry durability, migration, and cascade rules."""

import json
import os
import threading

import pytest

from src.fs_devices import registry as reg


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    path = tmp_path / "fs_registry.json"
    monkeypatch.setattr(reg, "FS_REGISTRY_FILE", str(path))
    # registry_path() reads FS_REGISTRY_FILE constant via function
    monkeypatch.setattr(reg, "registry_path", lambda: str(path))
    monkeypatch.setattr(reg, "known_hosts_path", lambda: str(tmp_path / "ssh_known_hosts"))
    yield path


def test_ensure_local_device(isolated_registry):
    device = reg.ensure_local_device()
    assert device.id == reg.LOCAL_DEVICE_ID
    assert device.transport == "local"
    assert device.builtin
    devices = reg.list_devices()
    assert any(d.id == reg.LOCAL_DEVICE_ID for d in devices)


def test_upsert_and_list_grants(isolated_registry, tmp_path):
    reg.ensure_local_device()
    folder = tmp_path / "vault"
    folder.mkdir()
    grant = reg.AllowedDirectory(
        id="grant:test1",
        device_id=reg.LOCAL_DEVICE_ID,
        display_name="Vault",
        path=str(folder),
        permission="rw",
    )
    saved = reg.upsert_allowed_directory(grant)
    assert saved.id == "grant:test1"
    listed = reg.list_allowed_directories()
    assert len(listed) == 1
    assert listed[0].display_name == "Vault"


def test_cannot_remove_local_device(isolated_registry):
    reg.ensure_local_device()
    with pytest.raises(ValueError):
        reg.remove_device(reg.LOCAL_DEVICE_ID)


def test_remove_device_blocks_on_dependents(isolated_registry, tmp_path):
    reg.ensure_local_device()
    device = reg.Device(
        id="device:remote1",
        display_name="Laptop",
        transport="ssh",
        hostname="100.64.0.2",
        username="u",
    )
    reg.upsert_device(device)
    reg.upsert_allowed_directory(reg.AllowedDirectory(
        id="grant:r1",
        device_id="device:remote1",
        display_name="Projects",
        path="/home/u/dev",
        permission="rw",
    ))
    result = reg.remove_device("device:remote1", remove_grants=False)
    assert result["removed"] is False
    assert result["dependent_grants"]
    result = reg.remove_device("device:remote1", remove_grants=True)
    assert result["removed"] is True
    assert "grant:r1" in result["removed_grant_ids"]


def test_atomic_write_and_permissions(isolated_registry):
    reg.ensure_local_device()
    path = isolated_registry
    assert path.is_file()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600 or os.name == "nt"
    data = json.loads(path.read_text())
    assert "devices" in data
    assert reg.LOCAL_DEVICE_ID in data["devices"]


def test_concurrent_mutations(isolated_registry, tmp_path):
    reg.ensure_local_device()
    folder = tmp_path / "a"
    folder.mkdir()
    errors = []

    def worker(i):
        try:
            reg.upsert_allowed_directory(reg.AllowedDirectory(
                id=f"grant:c{i}",
                device_id=reg.LOCAL_DEVICE_ID,
                display_name=f"G{i}",
                path=str(folder),
                permission="ro",
            ))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(reg.list_allowed_directories()) == 8


def test_trusted_migration_only(isolated_registry, tmp_path, monkeypatch):
    folder = tmp_path / "trusted"
    folder.mkdir()
    monkeypatch.setattr(
        "src.tool_execution.vet_workspace",
        lambda raw: str(folder) if str(folder) in str(raw) else None,
    )
    created = reg.migrate_trusted_workspace_grants(
        default_path=str(folder),
        session_cwds=[str(folder)],
    )
    assert len(created) == 1
    # Second call is idempotent for same path.
    again = reg.migrate_trusted_workspace_grants(default_path=str(folder))
    assert again == []


def test_find_local_grant_does_not_create(isolated_registry, tmp_path, monkeypatch):
    folder = tmp_path / "nogrant"
    folder.mkdir()
    monkeypatch.setattr(
        "src.tool_execution.vet_workspace",
        lambda raw: str(folder) if str(folder) in str(raw) else None,
    )
    assert reg.find_local_grant_by_path(str(folder)) is None
    assert reg.list_allowed_directories() == []
