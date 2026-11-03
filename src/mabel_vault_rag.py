"""Synchronize *only* Mabel's private Obsidian note folders into Odysseus RAG.

The Git checkout is the Obsidian vault root. This module never indexes source
code and never modifies vault files. Changes are detected by content hashes;
a local manifest lives under DATA_DIR (which must not be Syncthing-synced).
All Chroma writes/deletes are tagged by owner AND a Mabel-vault namespace.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from src.index_walk import is_indexable_file, prune_index_dirs

log = logging.getLogger(__name__)

NOTE_DIRS = ("memory", "identity", "knowledge", "projects", "journal", "attachments")
MAX_NOTE_BYTES = 1024 * 1024
MANIFEST_VERSION = 1
NAMESPACE = "mabel-vault-v1"


def enabled() -> bool:
    return os.getenv("MABEL_VAULT_RAG_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def configured_owners() -> tuple[str, ...]:
    owners = [o.strip().lower() for o in os.getenv("MABEL_VAULT_RAG_OWNERS", "").split(",")]
    return tuple(dict.fromkeys(o for o in owners if o and "/" not in o and "\\" not in o))


def resolve_vault_root() -> Path:
    from src.runtime_paths import get_app_root

    raw = os.getenv("MABEL_VAULT_ROOT") or get_app_root()
    root = Path(raw).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("MABEL_VAULT_ROOT is not a directory")
    # Missing directories usually indicate a missing Docker mount or incomplete
    # Syncthing state. Fail *before* deleting anything from the index.
    for name in NOTE_DIRS:
        folder = root / name
        if not folder.is_dir() or folder.is_symlink() or folder.resolve() != folder:
            raise ValueError(f"Missing or symlinked Mabel vault folder: {name}")
    return root


def _read_snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Return {relative_path: (sha256, content)}. Refuse symlink escapes."""
    notes: dict[str, tuple[str, str]] = {}
    for section in NOTE_DIRS:
        base = root / section
        for directory, dirs, filenames in os.walk(base, followlinks=False):
            prune_index_dirs(dirs)
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
            for name in sorted(filenames):
                if not is_indexable_file(name) or not name.lower().endswith(".md"):
                    continue
                file = Path(directory) / name
                if file.is_symlink() or not file.is_file():
                    continue
                try:
                    if file.stat().st_size > MAX_NOTE_BYTES:
                        raise RuntimeError(f"Mabel vault note exceeds {MAX_NOTE_BYTES} bytes: {file}")
                    raw = file.read_bytes()
                    if len(raw) > MAX_NOTE_BYTES:
                        raise RuntimeError(f"Mabel vault note grew while scanning: {file}")
                    text = raw.decode("utf-8-sig")
                    relative = file.relative_to(root).as_posix()
                    notes[relative] = (hashlib.sha256(raw).hexdigest(), text)
                except (OSError, UnicodeError) as exc:
                    # Do not interpret unreadable files as deletions.
                    raise RuntimeError(f"Cannot scan Mabel vault file: {file}") from exc
    return notes


def _manifest_file() -> Path:
    from src.constants import DATA_DIR

    return Path(DATA_DIR) / "mabel_vault_rag_manifest.json"


def _load_manifest(path: Path, root: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != MANIFEST_VERSION or data.get("root") != str(root):
        raise ValueError("Mabel RAG manifest does not match this vault root; refusing destructive sync")
    owners = data.get("owners")
    if not isinstance(owners, dict) or not all(isinstance(v, dict) for v in owners.values()):
        raise ValueError("Invalid Mabel RAG manifest")
    return owners


def _save_manifest(path: Path, root: Path, owners: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": MANIFEST_VERSION, "root": str(root), "owners": owners},
                         indent=2, sort_keys=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent,
            prefix=".mabel-vault-rag-", suffix=".tmp", delete=False,
        ) as fh:
            tmp = Path(fh.name)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def _where(source: str, owner: str) -> dict[str, Any]:
    return {"$and": [
        {"source": {"$eq": source}},
        {"owner": {"$eq": owner}},
        {"mabel_namespace": {"$eq": NAMESPACE}},
    ]}


def _chunk_id(source: str, owner: str, offset: int) -> str:
    raw = f"{NAMESPACE}\\0{owner}\\0{source}\\0{offset}"
    return "mabel_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _sync_one(rag: Any, *, root: Path, owner: str, rel: str, text: str) -> int:
    source = str(root / rel)
    # Prefix the note path to make semantic retrieval provenance explicit.
    # The existing sentence-aware splitter remains the canonical chunking API.
    chunks = rag._split_into_chunks(text.strip()) if text.strip() else []
    ids = [_chunk_id(source, owner, i) for i in range(len(chunks))]
    meta = {
        "source": source, "filename": Path(rel).name,
        "directory": str((root / rel).parent), "type": ".md",
        "owner": owner, "mabel_namespace": NAMESPACE,
        "vault_path": rel,
    }
    # Don't delete the old chunks until new embeddings are accepted by ALL
    # enabled lanes. A failed remote embed lane leaves the manifest unchanged,
    # allowing a retry when it recovers.
    for lane in rag._lanes:
        collection = lane.collection
        for first in range(0, len(chunks), 32):
            part = chunks[first:first + 32]
            document_texts = [f"[Mabel vault note: {rel} | chunk {first + n + 1}]\\n{body}"
                              for n, body in enumerate(part)]
            collection.upsert(
                ids=ids[first:first + len(part)],
                documents=document_texts,
                embeddings=lane.encode(document_texts),
                metadatas=[dict(meta, chunk_id=first + n) for n in range(len(part))],
            )
    for _name, collection in rag._active_collections():
        results = collection.get(where=_where(source, owner), include=[])
        obsolete = [doc_id for doc_id in results.get("ids", []) if doc_id not in set(ids)]
        if obsolete:
            collection.delete(ids=obsolete)
    return len(chunks)


def _remove_one(rag: Any, *, root: Path, owner: str, rel: str) -> int:
    source = str(root / rel)
    removed: set[str] = set()
    for _name, collection in rag._active_collections():
        results = collection.get(where=_where(source, owner), include=[])
        ids = results.get("ids", [])
        if ids:
            collection.delete(ids=ids)
            removed.update(ids)
    return len(removed)


def sync_vault(rag: Any, *, root: Path, owners: tuple[str, ...],
               manifest_path: Path | None = None) -> dict[str, int]:
    """Incrementally sync, with exact owner/namespace deletes and safe retries.

    Intended for a single coordinator worker; don't call concurrently from
    multiple server workers against the same manifest.
    """
    root = root.resolve(strict=True)
    # Validate existence of all allowlisted roots *even when invoked directly*.
    for name in NOTE_DIRS:
        p = root / name
        if not p.is_dir() or p.is_symlink() or p.resolve() != p:
            raise ValueError(f"Missing or symlinked Mabel vault folder: {name}")
    if not owners or not all(isinstance(o, str) and o.strip() for o in owners):
        raise ValueError("Explicit RAG owner(s) required; ownerless indexing forbidden")
    if not getattr(rag, "healthy", False) or not getattr(rag, "_lanes", None):
        raise RuntimeError("RAG embedding lanes unavailable")
    path = manifest_path or _manifest_file()
    manifest = _load_manifest(path, root)
    snapshot = _read_snapshot(root)
    stats = {"indexed": 0, "unchanged": 0, "removed": 0, "failed": 0}
    for owner in owners:
        previous = dict(manifest.get(owner, {}))
        # Never remove all known documents in a single auto pass: this
        # commonly indicates a Syncthing or bind-mount issue.
        if previous and not snapshot:
            raise RuntimeError("Mabel vault suddenly has no notes; refusing to clear RAG")
        current = dict(previous)
        for rel, (digest, content) in sorted(snapshot.items()):
            if previous.get(rel) == digest:
                stats["unchanged"] += 1
                continue
            try:
                stats["indexed"] += _sync_one(rag, root=root, owner=owner, rel=rel, text=content)
                current[rel] = digest
            except Exception:
                stats["failed"] += 1
                log.exception("Failed to update Mabel note %s for owner %s", rel, owner)
        # A failed scan raises instead of deleting anything. A partial indexing
        # failure only retains affected prior entries for the next retry.
        for rel in sorted(set(previous) - set(snapshot)):
            try:
                stats["removed"] += _remove_one(rag, root=root, owner=owner, rel=rel)
                current.pop(rel, None)
            except Exception:
                stats["failed"] += 1
                log.exception("Failed to retire Mabel note %s for owner %s", rel, owner)
        manifest[owner] = current
        _save_manifest(path, root, manifest)
    return stats


def sync_configured_vault() -> dict[str, int]:
    if not enabled():
        return {"disabled": 1}
    owners = configured_owners()
    if not owners:
        raise ValueError("Set MABEL_VAULT_RAG_OWNERS to the account(s) authorized to read the vault")
    root = resolve_vault_root()
    from src.rag_singleton import get_rag_manager

    rag = get_rag_manager()
    if rag is None:
        raise RuntimeError("Odysseus RAG unavailable; will retry next poll")
    return sync_vault(rag, root=root, owners=owners)
