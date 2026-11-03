"""The local vault index picks up notes added or edited after its first scan."""
import os
import time

from src.knowledge_engine.index import VaultIndex


def _index(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Old.md").write_text("# Old\nNothing about games.\n")
    cache = tmp_path / "cache"
    return vault, VaultIndex(vault, owner="alice", cache_dir=cache)


def test_unchanged_vault_needs_no_rescan(tmp_path):
    vault, index = _index(tmp_path)
    assert index.needs_reconcile()  # never scanned
    index.reconcile()
    assert not index.needs_reconcile()


def test_new_note_triggers_rescan_and_becomes_searchable(tmp_path):
    vault, index = _index(tmp_path)
    index.reconcile()
    (vault / "Lessons In Love.md").write_text("---\ntype: Game\n---\n# Lessons In Love\nA visual novel.\n")
    assert index.needs_reconcile()
    index.reconcile()
    hits = index.search("Lessons in Love")["matches"]
    assert hits and hits[0]["rel_path"] == "Lessons In Love.md"
    assert not index.needs_reconcile()


def test_edited_and_deleted_notes_trigger_rescan(tmp_path):
    vault, index = _index(tmp_path)
    index.reconcile()
    later = time.time() + 5
    note = vault / "Old.md"
    note.write_text("# Old\nNow about Selebus.\n")
    os.utime(note, (later, later))
    assert index.needs_reconcile()
    index.reconcile()
    assert index.search("Selebus")["matches"]
    note.unlink()
    assert index.needs_reconcile()

