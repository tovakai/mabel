"""Deletion propagation for source-linked episodic candidates.

Canonical session deletion and this SQLite queue cannot share a transaction.
After a committed session deletion, scrub candidate prose, reviewer rows and
undelivered event envelopes, then keep an opaque tombstone. Previously written
Markdown is tracked separately for an explicit, hash-verified cleanup in the
configured Mabel vault. Do not guess a vault path or delete unknown files.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import os
import re

from src.owner_identity import owner_key, is_request_sentinel_owner

from .candidates import CandidateAdmissionError, CandidateQueue, _now

_SESSION = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _queue_if_present(storage_dir: str | Path, owner: str) -> CandidateQueue | None:
    key = owner_key(owner)
    if not key or is_request_sentinel_owner(key):
        return None
    directory = Path(storage_dir).expanduser()
    if not directory.is_absolute():
        raise CandidateAdmissionError("Episodic retention directory must be absolute")
    path = directory / (sha256(key.encode("utf-8")).hexdigest()[:24] + ".sqlite")
    if path.is_symlink():
        raise CandidateAdmissionError("Refusing symlinked retention database")
    if path.is_file():
        return CandidateQueue(directory, owner=key)
    # A late chat response can race with a deletion before its first capture.
    # The experimental capture pilot must leave a tombstone even when a queue
    # has not been created yet. Disabled production installs create nothing.
    pilot = os.getenv("MABEL_EPISODIC_CAPTURE_ENABLED", "").strip().lower() in {
        "1", "true", "yes",
    }
    if not pilot:
        return None
    return CandidateQueue(directory, owner=key)


def _scrub_one_candidate(db, queue: CandidateQueue, row, session_id: str) -> None:
    """Remove a candidate and reviewer evidence while retaining note deletion proof."""
    if row["state"] in ("promoted", "promoting"):
        digest = row["decision_reason"]
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            digest = ""
        db.execute(
            "INSERT OR IGNORE INTO episode_deletion_queue "
            "(owner,session_id,candidate_id,expected_digest,queued_at) "
            "VALUES(?,?,?,?,?)",
            (queue.owner, session_id, row["id"], digest, _now()),
        )
    # A duplicate link to an erased source must not remain usable.
    dependents = db.execute(
        "SELECT id FROM candidates WHERE owner=? AND merged_into=?",
        (queue.owner, row["id"]),
    ).fetchall()
    for dependent in dependents:
        db.execute(
            "DELETE FROM event_outbox WHERE candidate_id=? AND kind='memory.candidate_merged'",
            (dependent["id"],),
        )
        db.execute(
            "UPDATE candidates SET merged_into=NULL, state='deferred', "
            "decision_reason='deleted_merge_target', updated_at=? "
            "WHERE owner=? AND id=?",
            (_now(), queue.owner, dependent["id"]),
        )
    db.execute("DELETE FROM event_outbox WHERE candidate_id=?", (row["id"],))
    db.execute(
        "DELETE FROM candidate_reviews WHERE owner=? AND "
        "(candidate_id=? OR target_candidate_id=?)",
        (queue.owner, row["id"], row["id"]),
    )
    db.execute(
        "DELETE FROM candidates WHERE owner=? AND id=?",
        (queue.owner, row["id"]),
    )


def purge_deleted_session(
    storage_dir: str | Path, *, owner: str, session_id: str,
) -> dict:
    """Scrub candidate/review content after a confirmed canonical deletion.

    This function is idempotent. It never reads, creates, or deletes Markdown.
    The small opaque episode deletion queue identifies notes requiring a
    separate operator-controlled vault cleanup. No network/model calls.
    """
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise CandidateAdmissionError("Deleted session ID must be a bounded token")
    queue = _queue_if_present(storage_dir, owner)
    if queue is None:
        return {"scrubbed": 0, "pending_episode_files": 0, "queue_present": False}

    prefix = f"chat:{session_id}:message:"
    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO deleted_sessions(owner,session_id,deleted_at) VALUES(?,?,?)",
            (queue.owner, session_id, _now()),
        )
        rows = db.execute(
            "SELECT id,state,decision_reason FROM candidates "
            "WHERE owner=? AND source_kind='conversation' "
            "AND substr(source_ref,1,?)=?",
            (queue.owner, len(prefix), prefix),
        ).fetchall()
        for row in rows:
            _scrub_one_candidate(db, queue, row, session_id)
        pending = db.execute(
            "SELECT count(*) FROM episode_deletion_queue "
            "WHERE owner=? AND session_id=?",
            (queue.owner, session_id),
        ).fetchone()[0]
        return {
            "scrubbed": len(rows), "pending_episode_files": pending,
            "queue_present": True,
        }


_MESSAGE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def invalidate_session_messages(
    storage_dir: str | Path, *,
    owner: str,
    session_id: str,
    removed_message_ids: tuple[str, ...] | list[str],
    invalidate_all_session_candidates: bool = False,
) -> dict:
    """Invalidate candidate sources after a committed message edit/removal.

    For a tail truncation or single assistant rewrite, pass exact removed/
    edited message IDs. For wholesale transcript replacement, also set
    invalidate_all_session_candidates, since the previous user-to-assistant
    associations no longer exist. The session stays active and *new* message
    IDs can create future candidates. Unlike whole-session deletion, no
    deleted_sessions tombstone is created.

    Call this only after the canonical message update commits. Canonical and
    episodic databases remain independently durable. Run explicit recovery
    reconciliation after an interrupted cross-database operation.
    """
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise CandidateAdmissionError("Session ID must be a bounded token")
    if type(invalidate_all_session_candidates) is not bool:
        raise CandidateAdmissionError("Invalid invalidation scope")
    if not isinstance(removed_message_ids, (list, tuple)) or len(removed_message_ids) > 100000:
        raise CandidateAdmissionError("Message IDs must be a bounded collection")
    removed_ids: set[str] = set()
    for mid in removed_message_ids:
        if not isinstance(mid, str) or not _MESSAGE.fullmatch(mid):
            raise CandidateAdmissionError("Invalid removed message ID")
        removed_ids.add(mid)
    if not removed_ids and not invalidate_all_session_candidates:
        return {"scrubbed": 0, "invalidated_message_ids": 0, "queue_present": False}
    queue = _queue_if_present(storage_dir, owner)
    if queue is None:
        return {"scrubbed": 0, "invalidated_message_ids": 0, "queue_present": False}
    prefix = f"chat:{session_id}:message:"
    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        rows = db.execute(
            "SELECT * FROM candidates WHERE owner=? AND source_kind='conversation' "
            "AND substr(source_ref,1,?)=?",
            (queue.owner,len(prefix),prefix),
        ).fetchall()
        matched = [
            row for row in rows if (
                invalidate_all_session_candidates
                or row["source_ref"][len(prefix):] in removed_ids
            )
        ]
        # A candidate must be impossible to re-create from an edited source,
        # even if its old record is already absent. No raw transcript is kept.
        invalidated_ids = removed_ids | {
            row["source_ref"][len(prefix):] for row in matched
        }
        for mid in invalidated_ids:
            db.execute(
                "INSERT OR IGNORE INTO invalidated_messages "
                "(owner,session_id,message_id,invalidated_at) VALUES(?,?,?,?)",
                (queue.owner,session_id,mid,_now()),
            )
        for row in matched:
            _scrub_one_candidate(db, queue, row, session_id)
        pending = db.execute(
            "SELECT count(*) FROM episode_deletion_queue "
            "WHERE owner=? AND session_id=?",
            (queue.owner,session_id),
        ).fetchone()[0]
    return {
        "scrubbed": len(matched), "invalidated_message_ids": len(invalidated_ids),
        "pending_episode_files": pending, "queue_present": True,
    }


def complete_episode_deletions(
    queue: CandidateQueue, *,
    vault_root: str | Path,
    protected_roots: tuple[str | Path, ...],
    session_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Delete ONLY canonical, matching-digest Mabel episode files.

    Requires an explicit vault and protected roots. A mismatch fails closed and
    leaves the pending record for manual inspection. Run only while the
    experimental reviewer/promotion pipeline is idle.
    """
    if not isinstance(queue, CandidateQueue):
        raise TypeError("Expected owner-scoped CandidateQueue")
    if not protected_roots:
        raise CandidateAdmissionError("Protected source vault roots are required")
    if session_id is not None and (
        not isinstance(session_id, str) or not _SESSION.fullmatch(session_id)
    ):
        raise CandidateAdmissionError("Invalid deleted session filter")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CandidateAdmissionError("Cleanup limit must be 1..100")
    from .promotion import _target

    with queue._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if session_id is None:
            records = db.execute(
                "SELECT * FROM episode_deletion_queue WHERE owner=? "
                "ORDER BY queued_at,candidate_id LIMIT ?",
                (queue.owner,limit),
            ).fetchall()
        else:
            records = db.execute(
                "SELECT * FROM episode_deletion_queue WHERE owner=? AND session_id=? "
                "ORDER BY queued_at,candidate_id LIMIT ?",
                (queue.owner,session_id,limit),
            ).fetchall()
        cleaned = 0
        for row in records:
            # _target checks selected Mabel vault != protected source vaults,
            # paths, symlinks and the owner bucket before any file operation.
            path = _target(queue, row["candidate_id"], vault_root, protected_roots)
            if path.is_symlink():
                raise CandidateAdmissionError("Refusing to delete symlinked episode")
            if path.exists():
                digest = row["expected_digest"]
                if not _DIGEST.fullmatch(digest) or not path.is_file():
                    raise CandidateAdmissionError("Episode cannot be safely verified for deletion")
                if sha256(path.read_bytes()).hexdigest() != digest:
                    raise CandidateAdmissionError("Episode contents changed; manual cleanup required")
                path.unlink()
                if hasattr(os, "O_DIRECTORY"):
                    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            db.execute(
                "DELETE FROM episode_deletion_queue WHERE owner=? AND candidate_id=?",
                (queue.owner,row["candidate_id"]),
            )
            cleaned += 1
        remaining = db.execute(
            "SELECT count(*) FROM episode_deletion_queue WHERE owner=?",
            (queue.owner,),
        ).fetchone()[0]
        return {"cleaned_episode_files": cleaned, "pending_episode_files": remaining}


def reconcile_missing_sessions(
    queue: CandidateQueue, *, db, limit: int = 100,
    after_session_id: str | None = None,
) -> dict:
    """Manually replay privacy scrubs missed by a crash after canonical delete.

    Checks the current owner-scoped session table, not model advice. Does not
    touch Markdown; verified episode files remain in the deletion queue for
    a separately approved target-vault cleanup.
    """
    if not isinstance(queue, CandidateQueue):
        raise TypeError("Expected owner-scoped CandidateQueue")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise CandidateAdmissionError("Reconciliation limit must be 1..1000")
    if after_session_id is not None and (
        not isinstance(after_session_id, str) or not _SESSION.fullmatch(after_session_id)
    ):
        raise CandidateAdmissionError("Invalid reconciliation cursor")
    from sqlalchemy import text

    with queue._connect() as local:
        refs = [row[0] for row in local.execute(
            "SELECT source_ref FROM candidates WHERE owner=? "
            "AND source_kind='conversation' ORDER BY created_at,id",
            (queue.owner,),
        )]
    discovered = set()
    for ref in refs:
        match = re.fullmatch(
            r"chat:([A-Za-z0-9_.:-]{1,128}):message:[A-Za-z0-9_.:-]{1,128}",
            ref,
        )
        if match and (after_session_id is None or match.group(1) > after_session_id):
            discovered.add(match.group(1))
    ordered = sorted(discovered)
    ids = ordered[:limit]
    next_after = ids[-1] if len(ordered) > len(ids) and ids else None
    scrubbed = 0
    deleted_sessions = 0
    for session_id in ids:
        owner = db.execute(
            text("SELECT owner FROM sessions WHERE id=:sid"),
            {"sid": session_id},
        ).scalar_one_or_none()
        if owner_key(owner) != queue.owner:
            result = purge_deleted_session(
                queue.path.parent, owner=queue.owner, session_id=session_id,
            )
            scrubbed += result["scrubbed"]
            deleted_sessions += 1
    return {
        "checked_sessions": len(ids), "deleted_sessions": deleted_sessions,
        "scrubbed": scrubbed, "next_after_session_id": next_after,
    }


def reconcile_invalid_sources(
    queue: CandidateQueue, *, db, limit: int = 100,
    after_candidate_id: str | None = None,
) -> dict:
    """Manually scrub candidates after missed message deletions or edits.

    Unlike deleted-session reconciliation, this checks the exact assistant
    receipt *and* the prior user excerpt. It preserves unrelated sessions
    and makes no network request or vault write. The cursor uses stable
    candidate IDs, so deletion during the sweep does not skip later records.
    """
    if not isinstance(queue, CandidateQueue):
        raise TypeError("Expected owner-scoped CandidateQueue")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise CandidateAdmissionError("Source reconciliation limit must be 1..1000")
    if after_candidate_id is not None and (
        not isinstance(after_candidate_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", after_candidate_id)
    ):
        raise CandidateAdmissionError("Invalid source reconciliation cursor")
    from sqlalchemy import text
    from .promotion import _SOURCE, _verified_transcript

    with queue._connect() as local:
        rows = [dict(row) for row in local.execute(
            "SELECT * FROM candidates WHERE owner=? AND source_kind='conversation' "
            "AND kind='experience' AND id>? "
            "ORDER BY id LIMIT ?",
            (queue.owner,after_candidate_id or "",limit+1),
        )]
    page = rows[:limit]
    next_after = page[-1]["id"] if len(rows) > limit and page else None
    scrubbed = 0
    deleted_sessions = 0
    for candidate in page:
        match = _SOURCE.fullmatch(candidate["source_ref"])
        if match is None:
            continue
        session_id, message_id = match.group("session"), match.group("message")
        session_owner = db.execute(
            text("SELECT owner FROM sessions WHERE id=:sid"),
            {"sid":session_id},
        ).scalar_one_or_none()
        if owner_key(session_owner) != queue.owner:
            result = purge_deleted_session(
                queue.path.parent, owner=queue.owner, session_id=session_id,
            )
            scrubbed += result["scrubbed"]
            deleted_sessions += 1
            continue
        try:
            _verified_transcript(queue, candidate, db)
        except (CandidateAdmissionError, PermissionError):
            result = invalidate_session_messages(
                queue.path.parent, owner=queue.owner, session_id=session_id,
                removed_message_ids=[message_id],
            )
            scrubbed += result["scrubbed"]
    return {
        "checked_candidates": len(page), "scrubbed": scrubbed,
        "deleted_sessions": deleted_sessions,
        "next_after_candidate_id": next_after,
    }
