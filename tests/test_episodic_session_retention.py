"""Phase 00.5 session-deletion scrub and durable episode cleanup tests."""
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.episodic_memory import (
    CandidateAdmissionError, CandidateDraft, CandidateQueue, CapturePolicy,
    ReviewProposal, candidate_fingerprint, propose_review,
    purge_deleted_session, complete_episode_deletions, reconcile_missing_sessions,
)
from src.episodic_memory.promotion import _target


@pytest.fixture
def sample(tmp_path):
    state = tmp_path / "runtime"
    queue = CandidateQueue(state, owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    def make(sid="chat-1", mid="message-1"):
        draft = CandidateDraft(
            kind="experience", actor="mabel", source_kind="conversation",
            source_ref=f"chat:{sid}:message:{mid}",
            source_event_id=f"message:{mid}",
            origin="completed_persisted_turn",
            summary="User said: This is a lengthy, synthetic event.\n"
                    "Mabel replied: I recorded a separate synthetic answer.",
        )
        return queue.propose(draft, policy=policy)
    one = make()
    two = make(mid="message-2")
    other = make(sid="chat-2",mid="message-3")
    review = propose_review(
        queue, one["id"], ReviewProposal(
            run_id="test-run", recommendation="promote", confidence=90,
            rationale_code="meaningful_discovery",
            source_fingerprint=candidate_fingerprint(one),
        ), policy=policy,
    )
    return state, queue, policy, one, two, other, review, make


def test_committed_deletion_scrubs_only_matching_owner_and_session(sample, tmp_path):
    state, queue, policy, one, two, other, review, make = sample
    bob = CandidateQueue(state, owner="bob")
    bob_proposal = CandidateDraft(
        kind="experience", actor="mabel", source_kind="conversation",
        source_ref="chat:chat-1:message:bob-msg",
        source_event_id="message:bob-msg",
        origin="completed_persisted_turn", summary="A different account",
    )
    bob_row = bob.propose(bob_proposal, policy=policy)

    result = purge_deleted_session(state, owner="alice", session_id="chat-1")
    assert result == {
        "scrubbed": 2, "pending_episode_files": 0, "queue_present": True,
    }
    assert queue.get(one["id"]) is None
    assert queue.get(two["id"]) is None
    assert queue.get(other["id"]) is not None
    assert bob.get(bob_row["id"]) is not None
    assert all("chat:chat-1:" not in str(e) for e in queue.pending_events())
    with queue._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM candidate_reviews"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM deleted_sessions WHERE owner=? AND session_id=?",
            ("alice","chat-1"),
        ).fetchone()[0] == 1

    assert purge_deleted_session(state, owner="alice", session_id="chat-1")["scrubbed"] == 0
    with pytest.raises(CandidateAdmissionError, match="recapture forbidden"):
        make(sid="chat-1", mid="message-4")
    assert queue.get(other["id"]) is not None
    assert list(tmp_path.rglob("*.md")) == []


def test_no_existing_queue_does_not_create_database_or_directory(tmp_path):
    state = tmp_path / "not-created"
    assert purge_deleted_session(state, owner="alice", session_id="gone") == {
        "scrubbed": 0, "pending_episode_files": 0, "queue_present": False,
    }
    assert not state.exists()


@pytest.mark.parametrize("bad", ["", "../bad", "x" * 129, "hello world"])
def test_invalid_session_token_rejected(sample, bad):
    state, *_ = sample
    with pytest.raises(CandidateAdmissionError, match="bounded token"):
        purge_deleted_session(state, owner="alice", session_id=bad)


def _prepare_note(sample, tmp_path, *, state="promoted"):
    storage, queue, _, one, *_ = sample
    vault = tmp_path / "mabel-vault"
    vault.mkdir()
    protected = tmp_path / "core-of-truth"
    protected.mkdir()
    untouched = protected / "private.md"
    untouched.write_bytes(b"private")
    target = _target(queue, one["id"], vault, (protected,))
    content = b"---\ntype: episodic\n---\nThis is a synthetic Mabel note.\n"
    target.write_bytes(content)
    digest = sha256(content).hexdigest()
    with queue._connect() as db:
        db.execute(
            "UPDATE candidates SET state=?,decision_reason=? WHERE id=?",
            (state,digest,one["id"]),
        )
    return vault, protected, target, untouched


@pytest.mark.parametrize("state", ["promoted", "promoting"])
def test_deleted_promoted_or_inflight_note_requires_verified_cleanup(sample, tmp_path, state):
    storage, queue, policy, one, two, other, review, make = sample
    vault, protected, note, private = _prepare_note(sample, tmp_path, state=state)
    result = purge_deleted_session(storage, owner="alice", session_id="chat-1")
    assert result["scrubbed"] == 2
    assert result["pending_episode_files"] == 1
    assert queue.get(one["id"]) is None
    assert note.exists()
    assert private.read_bytes() == b"private"
    with pytest.raises(CandidateAdmissionError, match="Protected source"):
        complete_episode_deletions(queue, vault_root=vault, protected_roots=())
    with pytest.raises(CandidateAdmissionError, match="protected"):
        complete_episode_deletions(
            queue, vault_root=protected, protected_roots=(protected,),
        )
    cleaned = complete_episode_deletions(
        queue, vault_root=vault, protected_roots=(protected,),
    )
    assert cleaned == {"cleaned_episode_files": 1, "pending_episode_files": 0}
    assert not note.exists()
    assert private.read_bytes() == b"private"
    assert complete_episode_deletions(
        queue, vault_root=vault, protected_roots=(protected,),
    )["cleaned_episode_files"] == 0
    assert queue.get(other["id"]) is not None


def test_tampered_promoted_note_is_not_deleted(sample, tmp_path):
    storage, queue, policy, one, *_ = sample
    vault, protected, note, private = _prepare_note(sample, tmp_path)
    purge_deleted_session(storage, owner="alice", session_id="chat-1")
    note.write_text("User modified this note.", encoding="utf-8")
    with pytest.raises(CandidateAdmissionError, match="contents changed"):
        complete_episode_deletions(
            queue, vault_root=vault, protected_roots=(protected,),
        )
    assert note.read_text() == "User modified this note."
    with queue._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM episode_deletion_queue"
        ).fetchone()[0] == 1


def test_unknown_or_missing_digest_never_deletes_an_existing_note(sample, tmp_path):
    storage, queue, policy, one, *_ = sample
    vault, protected, note, private = _prepare_note(sample, tmp_path)
    with queue._connect() as db:
        db.execute(
            "UPDATE candidates SET decision_reason='' WHERE id=?",
            (one["id"],),
        )
    purge_deleted_session(storage, owner="alice", session_id="chat-1")
    with pytest.raises(CandidateAdmissionError, match="safely verified"):
        complete_episode_deletions(
            queue, vault_root=vault, protected_roots=(protected,),
        )
    assert note.exists()


def test_reconciliation_recovers_scrub_lost_after_canonical_session_delete(sample, tmp_path):
    storage, queue, policy, one, two, other, *_ = sample
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT)"))
        conn.execute(text("INSERT INTO sessions VALUES('chat-2','alice')"))
    with Session(engine) as db:
        result = reconcile_missing_sessions(queue, db=db)
    assert result == {"checked_sessions": 2, "deleted_sessions": 1, "scrubbed": 2, "next_after_session_id": None}
    assert queue.get(one["id"]) is None
    assert queue.get(two["id"]) is None
    assert queue.get(other["id"]) is not None
    with Session(engine) as db:
        assert reconcile_missing_sessions(queue, db=db)["scrubbed"] == 0
    engine.dispose()


def test_additive_v2_to_v3_migration_preserves_candidate_and_review(sample):
    storage, queue, policy, one, two, other, review, make = sample
    with sqlite3.connect(queue.path) as db:
        db.execute("DROP TABLE episode_deletion_queue")
        db.execute("DROP TABLE deleted_sessions")
        db.execute("PRAGMA user_version=2")
    migrated = CandidateQueue(storage, owner="alice")
    assert migrated.get(one["id"]) is not None
    with migrated._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert db.execute(
            "SELECT id FROM candidate_reviews WHERE id=?", (review["id"],)
        ).fetchone() is not None
    assert purge_deleted_session(storage, owner="alice", session_id="chat-1")["scrubbed"] == 2


def test_actual_session_manager_delete_scrubs_candidate_after_commit(tmp_path, monkeypatch):
    """Exercise the canonical SessionManager deletion boundary, not just helper."""
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, Session as DbSession, ChatMessage as DbMessage
    from core.session_manager import SessionManager
    import core.session_manager as sm
    from src import constants

    engine = create_engine(f"sqlite:///{tmp_path / 'canonical-manager.sqlite'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory() as db:
        db.add(DbSession(
            id="retained-chat", owner="alice",
            name="Synthetic session", endpoint_url="test", model="test",
        ))
        db.add(DbMessage(
            id="retained-message", session_id="retained-chat",
            role="assistant", content="Synthetic answer for deletion test",
        ))
        db.commit()
    state = tmp_path / "episodic_candidates"
    queue = CandidateQueue(state, owner="alice")
    proposal = CandidateDraft(
        kind="experience", actor="mabel", source_kind="conversation",
        source_ref="chat:retained-chat:message:retained-message",
        source_event_id="message:retained-message",
        origin="completed_persisted_turn", summary="Synthetic memory excerpt",
    )
    candidate = queue.propose(
        proposal, policy=CapturePolicy(episodic_enabled=True, memory_enabled=True),
    )
    monkeypatch.setattr(sm, "SessionLocal", factory)
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    manager = SessionManager.__new__(SessionManager)
    manager.sessions = {}
    assert manager.delete_session("retained-chat") is True

    assert queue.get(candidate["id"]) is None
    with factory() as db:
        assert db.query(DbSession).filter(DbSession.id == "retained-chat").first() is None
        assert db.query(DbMessage).filter(DbMessage.session_id == "retained-chat").count() == 0
    engine.dispose()


def test_pilot_deletion_before_first_capture_persists_tombstone(tmp_path, monkeypatch):
    state = tmp_path / "new_runtime"
    monkeypatch.setenv("MABEL_EPISODIC_CAPTURE_ENABLED", "1")
    result = purge_deleted_session(state, owner="alice", session_id="late-chat")
    assert result == {
        "scrubbed": 0, "pending_episode_files": 0, "queue_present": True,
    }
    queue = CandidateQueue(state, owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    with pytest.raises(CandidateAdmissionError, match="recapture forbidden"):
        queue.propose(CandidateDraft(
            kind="experience", actor="mabel", source_kind="conversation",
            source_ref="chat:late-chat:message:late-reply",
            source_event_id="message:late-reply",
            origin="completed_persisted_turn",
            summary="An out-of-order synthetic reply after source deletion",
        ), policy=policy)


def test_reconciliation_cursor_reaches_later_sessions(sample, tmp_path):
    storage, queue, policy, one, two, other, *_ = sample
    engine = create_engine(f"sqlite:///{tmp_path / 'cursor.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT)"))
    with Session(engine) as db:
        first = reconcile_missing_sessions(queue, db=db, limit=1)
        assert first["checked_sessions"] == 1
        assert first["deleted_sessions"] == 1
        assert first["next_after_session_id"] == "chat-1"
        second = reconcile_missing_sessions(
            queue, db=db, limit=1,
            after_session_id=first["next_after_session_id"],
        )
        assert second["checked_sessions"] == 1
        assert second["deleted_sessions"] == 1
        assert second["next_after_session_id"] is None
    assert queue.get(one["id"]) is None
    assert queue.get(other["id"]) is None
    engine.dispose()
