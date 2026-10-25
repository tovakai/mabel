"""Hermetic tests for the Mabel-vault → Chroma incremental sync contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import mabel_vault_rag as vault


class FakeCollection:
    def __init__(self, fail_upsert=False):
        self.rows = {}
        self.fail_upsert = fail_upsert

    def upsert(self, *, ids, documents, embeddings, metadatas):
        if self.fail_upsert:
            raise RuntimeError("embedding lane down")
        for doc_id, text, metadata in zip(ids, documents, metadatas):
            self.rows[doc_id] = {"document": text, "metadata": metadata}

    def get(self, *, where, include):
        filters = {k: v["$eq"] for p in where["$and"] for k, v in p.items()}
        return {"ids": [i for i, row in self.rows.items()
                        if all(row["metadata"].get(k) == v for k, v in filters.items())]}

    def delete(self, *, ids):
        for doc_id in ids:
            self.rows.pop(doc_id, None)


class FakeLane:
    def __init__(self, collection):
        self.collection = collection

    def encode(self, texts):
        return [[0.3, 0.4] for _ in texts]


class FakeRag:
    healthy = True

    def __init__(self, *collections):
        self._lanes = [FakeLane(c) for c in (collections or (FakeCollection(),))]

    def _split_into_chunks(self, text):
        return [s for s in text.split("|") if s]

    def _active_collections(self):
        return [(str(n), lane.collection) for n, lane in enumerate(self._lanes)]


@pytest.fixture
def root(tmp_path):
    for name in vault.NOTE_DIRS:
        (tmp_path / name).mkdir()
    return tmp_path


def add(root: Path, path: str, content: str):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    return file


def sync(rag, root):
    return vault.sync_vault(
        rag, root=root, owners=("admin",), manifest_path=root / "local-state.json"
    )


def test_scans_only_six_private_markdown_roots(root):
    add(root, "memory/first.md", "---\ntags: [memory]\n---\nhello [[Second]]")
    add(root, "projects/nested/deeper.md", "project log")
    add(root, "src/python.md", "must not be indexed")
    add(root, "docs/notes.md", "not my personal vault")
    add(root, "identity/secrets.txt", "text attachment not indexed")
    add(root, "memory/.hidden.md", "not indexed")
    add(root, "memory/.obsidian/plugin.md", "not indexed")
    add(root, "memory/node_modules/package.md", "not indexed")
    recorded = vault._read_snapshot(root)
    assert set(recorded) == {"memory/first.md", "projects/nested/deeper.md"}
    assert "[[Second]]" in recorded["memory/first.md"][1]


def test_incremental_updates_delete_old_chunks_without_touching_other_owners(root):
    note = add(root, "memory/hello.md", "old one|old two")
    rag = FakeRag()
    collection = rag._lanes[0].collection
    first = sync(rag, root)
    assert first["indexed"] == 2
    assert len(collection.rows) == 2
    other = "unrelated-doc"
    collection.rows[other] = {
        "document": "private foreign content",
        "metadata": {
            "owner": "another-user",
            "mabel_namespace": vault.NAMESPACE,
            "source": str(note),
        },
    }
    same = sync(rag, root)
    assert same["unchanged"] == 1
    assert same["indexed"] == 0

    note.write_text("new one", encoding="utf-8")
    changed = sync(rag, root)
    assert changed["indexed"] == 1
    assert len(collection.rows) == 2
    assert other in collection.rows
    assert all("old" not in row["document"] for i, row in collection.rows.items() if i != other)

    note.unlink()
    result = sync(rag, root)
    assert result["removed"] == 1
    assert list(collection.rows) == [other]


def test_missing_folder_never_triggers_deletion(root):
    note = add(root, "memory/hello.md", "content")
    rag = FakeRag()
    sync(rag, root)
    (root / "knowledge").rmdir()
    with pytest.raises(ValueError, match="Missing or symlinked"):
        sync(rag, root)
    assert len(rag._lanes[0].collection.rows) == 1
    assert note.is_file()


def test_symlink_to_outside_not_indexed(root, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.md")
    outside.write_text("should stay private", encoding="utf-8")
    (root / "knowledge" / "escape.md").symlink_to(outside)
    assert "knowledge/escape.md" not in vault._read_snapshot(root)


def test_failed_embedding_lane_keeps_manifest_and_retries(root):
    note = add(root, "memory/hello.md", "updated")
    local = FakeCollection()
    remote = FakeCollection(fail_upsert=True)
    rag = FakeRag(local, remote)
    first = sync(rag, root)
    assert first["failed"] == 1
    manifest = json.loads((root / "local-state.json").read_text())
    assert manifest["owners"]["admin"] == {}
    remote.fail_upsert = False
    second = sync(rag, root)
    assert second["failed"] == 0
    assert second["indexed"] == 1
    assert len(local.rows) == len(remote.rows) == 1
    assert all(row["metadata"]["source"] == str(note) for row in local.rows.values())


def test_owner_explicit_and_not_ownerless(root):
    rag = FakeRag()
    with pytest.raises(ValueError, match="owner"):
        vault.sync_vault(rag, root=root, owners=(), manifest_path=root / "manifest.json")


def test_manifest_from_other_root_fails_closed(root, tmp_path):
    add(root, "memory/hello.md", "x")
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps({
        "version": vault.MANIFEST_VERSION,
        "root": "/not-this-vault",
        "owners": {"admin": {"memory/hello.md": "old"}},
    }))
    with pytest.raises(ValueError, match="does not match"):
        vault.sync_vault(FakeRag(), root=root, owners=("admin",), manifest_path=manifest)


def test_fully_empty_snapshot_does_not_purge_existing_vectors(root):
    note = add(root, "memory/hello.md", "x")
    rag = FakeRag()
    sync(rag, root)
    note.unlink()
    with pytest.raises(RuntimeError, match="suddenly has no notes"):
        sync(rag, root)
    assert len(rag._lanes[0].collection.rows) == 1


def test_no_reembedding_when_content_unchanged(root):
    add(root, "memory/hello.md", "hello")
    rag = FakeRag()
    sync(rag, root)
    collection = rag._lanes[0].collection
    collection.fail_upsert = True
    result = sync(rag, root)
    assert result["failed"] == 0
    assert result["unchanged"] == 1
