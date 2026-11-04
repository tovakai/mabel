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



def _media_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "Anime").mkdir(parents=True)
    for name, rating, genre in (("Pluto", 10, "Mystery"), ("Monster", 9, "Thriller"), ("Unrated", None, "Comedy")):
        lines = ["---", "Type: Anime", f"Genre:\n  - \"[[{genre}]]\""]
        if rating is not None:
            lines.append(f"My Rating: {rating}")
        (vault / "Anime" / f"{name}.md").write_text("\n".join(lines + ["---", "body"]) + "\n")
    index = VaultIndex(vault, owner="alice", cache_dir=tmp_path / "cache")
    index.reconcile()
    return index


def test_compact_metadata_listing_sorted_by_rating(tmp_path):
    index = _media_vault(tmp_path)
    result = index.search(mode="metadata_only", property_filters={"Type": "Anime"},
                          fields=["My Rating", "Genre"], sort_by="My Rating", top_k=10)
    assert [m["title"] for m in result["matches"]] == ["Pluto", "Monster", "Unrated"]
    assert result["matches"][0] == {"title": "Pluto", "My Rating": 10, "Genre": ["Mystery"]}
    titles = index.search(mode="metadata_only", property_filters={"Type": "Anime"}, fields=[], top_k=200)
    assert all(set(m) == {"title"} for m in titles["matches"])


def test_compact_listing_bounds_are_enforced(tmp_path):
    import pytest
    index = _media_vault(tmp_path)
    with pytest.raises(ValueError):
        index.search(mode="metadata_only", fields=["My Rating"], top_k=101)  # the index stays strict; tools clamp
    with pytest.raises(ValueError):
        index.search("x", mode="lexical", fields=[])


def test_numeric_range_filters_and_top_k_clamp(tmp_path):
    index = _media_vault(tmp_path)
    high = index.search(mode="metadata_only", property_filters={"My Rating": {">=": 9}}, fields=[], top_k=200)
    assert sorted(m["title"] for m in high["matches"]) == ["Monster", "Pluto"]
    exact = index.search(mode="metadata_only", property_filters={"My Rating": {"gt": 9}}, fields=[], top_k=10)
    assert [m["title"] for m in exact["matches"]] == ["Pluto"]
