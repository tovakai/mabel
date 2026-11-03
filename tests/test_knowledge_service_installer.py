"""Opt-in user-systemd deployment never starts services or touches the vault."""
import json
from pathlib import Path
import sys

import pytest

from scripts.install_knowledge_index_services import install, render


def test_install_writes_isolated_index_watcher_and_embedding_timer(tmp_path):
    checkout = tmp_path / "Mabel"
    (checkout / "src/knowledge_engine").mkdir(parents=True)
    (checkout / "src/knowledge_engine/__main__.py").write_text("")
    vault = tmp_path / "Core of Truth"
    vault.mkdir()
    systemd = tmp_path / "systemd/user"
    config = tmp_path / "mabel/knowledge-index.env"
    paths = install(checkout=checkout, python=Path(sys.executable), vault=vault,
                    owner="alice", systemd_dir=systemd, env_file=config)
    assert len(paths) == 4
    watch = (systemd / "mabel-knowledge-index.service").read_text()
    embed = (systemd / "mabel-knowledge-embed.service").read_text()
    timer = (systemd / "mabel-knowledge-embed.timer").read_text()
    env = config.read_text()
    assert "watch --interval 2 --reconcile-seconds 120" in watch
    assert "Restart=on-failure" in watch
    assert "embed --embedding-limit 256 --batch-size 8" in embed
    assert "OnUnitInactiveSec=2min" in timer
    assert "MABEL_KNOWLEDGE_VAULT=" + json.dumps(str(vault)) in env
    assert 'MABEL_KNOWLEDGE_OWNER="alice"' in env
    assert "${MABEL_KNOWLEDGE_VAULT}" in watch
    assert "--embed" not in watch
    assert not list(vault.iterdir())
    assert config.stat().st_mode & 0o777 == 0o600


def test_install_refuses_to_overwrite_existing_units(tmp_path):
    checkout = tmp_path / "Mabel"
    (checkout / "src/knowledge_engine").mkdir(parents=True)
    (checkout / "src/knowledge_engine/__main__.py").touch()
    vault = tmp_path / "Vault"
    vault.mkdir()
    params = dict(checkout=checkout, python=Path(sys.executable), vault=vault,
                  owner="alice", systemd_dir=tmp_path / "user",
                  env_file=tmp_path / "knowledge.env")
    install(**params)
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        install(**params)
    install(**params, replace=True)
    assert list((tmp_path / "user").glob("*.bak-*"))


def test_render_never_embeds_path_or_account_in_execstart(tmp_path):
    units = render(checkout=tmp_path, python=Path(sys.executable),
                   env_file=tmp_path / "conf.env", every_minutes=5,
                   interval=3, reconcile_seconds=90,
                   embedding_limit=64, embedding_batch_size=4)
    assert set(units) == {
        "mabel-knowledge-index.service", "mabel-knowledge-embed.service",
        "mabel-knowledge-embed.timer",
    }
    assert "embed --embedding-limit 64 --batch-size 4" in units["mabel-knowledge-embed.service"]
    assert "OnUnitInactiveSec=5min" in units["mabel-knowledge-embed.timer"]
