"""Phase 00.5 message-edit and transcript-replacement privacy regressions."""
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from core.models import ChatMessage as LiveMessage
from src.episodic_memory import (
    CandidateAdmissionError, CandidateQueue, CapturePolicy,
    capture_completed_turn, invalidate_session_messages, reconcile_invalid_sources,
)


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "episodic_candidates"
    queue = CandidateQueue(state, owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)

    def add(sid="chat-a", mid="assistant-one",
            user="We investigated a tricky source-evidence problem in Mabel today.",
            assistant="I found a source-receipt mismatch and explained the observation."):
        return capture_completed_turn(
            storage_dir=state, owner="alice",
            session_id=sid, assistant_message_id=mid,
            user_message=user, assistant_message=assistant,
            policy=policy,
        )
    return state, queue, policy, add


def test_specific_message_invalidation_revokes_only_target_and_never_tombstones_session(setup):
    state, queue, policy, add = setup
    first = add(mid="assistant-one")
    second = add(mid="assistant-two")
    other_session = add(sid="chat-b",mid="assistant-three")
    result = invalidate_session_messages(
        state, owner="alice", session_id="chat-a",
        removed_message_ids=["assistant-one"],
    )
    assert result["scrubbed"] == 1
    assert result["invalidated_message_ids"] == 1
    assert queue.get(first["id"]) is None
    assert queue.get(second["id"]) is not None
    assert queue.get(other_session["id"]) is not None
    assert all(e["candidate_id"] != first["id"] for e in queue.pending_events())
    assert not queue.list(state="promoted")
    with queue._connect() as db:
        assert db.execute("SELECT count(*) FROM deleted_sessions").fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM invalidated_messages WHERE owner='alice'"
        ).fetchone()[0] == 1
    with pytest.raises(CandidateAdmissionError, match="recapture forbidden"):
        add(mid="assistant-one")
    assert add(mid="assistant-four")["state"] == "pending"


def test_whole_transcript_replacement_scrubs_old_candidates_but_allows_new_id(setup):
    state, queue, policy, add = setup
    first = add(mid="assistant-one")
    second = add(mid="assistant-two")
    other = add(sid="chat-b",mid="assistant-three")
    result = invalidate_session_messages(
        state, owner="alice", session_id="chat-a",
        removed_message_ids=["assistant-one", "assistant-two"],
        invalidate_all_session_candidates=True,
    )
    assert result["scrubbed"] == 2
    assert queue.get(first["id"]) is None
    assert queue.get(second["id"]) is None
    assert queue.get(other["id"]) is not None
    with pytest.raises(CandidateAdmissionError, match="recapture forbidden"):
        add(mid="assistant-two")
    fresh = add(mid="assistant-fresh")
    assert fresh and fresh["created"] and fresh["state"] == "pending"
    assert not queue.list(state="promoted")


def test_message_invalidation_is_owner_scoped_and_idempotent(setup):
    state, queue, policy, add = setup
    original = add()
    bob = CandidateQueue(state, owner="bob")
    from src.episodic_memory import CandidateDraft
    separate = bob.propose(CandidateDraft(
        kind="experience", actor="mabel", source_kind="conversation",
        source_ref="chat:chat-a:message:assistant-one",
        source_event_id="message:assistant-one",
        summary="Bob owns an unrelated conversation with overlapping IDs.",
        origin="completed_persisted_turn",
    ), policy=policy)
    first = invalidate_session_messages(
        state,owner="alice",session_id="chat-a",
        removed_message_ids=["assistant-one"],
    )
    again = invalidate_session_messages(
        state,owner="alice",session_id="chat-a",
        removed_message_ids=["assistant-one"],
    )
    assert first["scrubbed"] == 1 and again["scrubbed"] == 0
    assert bob.get(separate["id"]) is not None
    assert queue.get(original["id"]) is None


def test_promoted_and_inflight_sources_queue_only_verified_note_cleanup(setup):
    state, queue, policy, add = setup
    first = add()
    second = add(mid="assistant-two")
    with queue._connect() as db:
        db.execute(
            "UPDATE candidates SET state='promoted',decision_reason=? WHERE id=?",
            ("a" * 64,first["id"]),
        )
        db.execute(
            "UPDATE candidates SET state='promoting',decision_reason=? WHERE id=?",
            ("b" * 64,second["id"]),
        )
    result = invalidate_session_messages(
        state,owner="alice",session_id="chat-a",
        removed_message_ids=["assistant-one","assistant-two"],
    )
    assert result["scrubbed"] == 2
    assert result["pending_episode_files"] == 2
    with queue._connect() as db:
        notes = db.execute(
            "SELECT candidate_id, expected_digest FROM episode_deletion_queue "
            "ORDER BY candidate_id"
        ).fetchall()
    assert {row[0]:row[1] for row in notes} == {
        first["id"]:"a"*64, second["id"]:"b"*64,
    }
    assert queue.get(first["id"]) is None
    assert queue.get(second["id"]) is None


def test_invalid_message_id_never_mutates_staging(setup):
    state, queue, policy, add = setup
    row = add()
    for bad in ("../evil","hello world",""):
        with pytest.raises(CandidateAdmissionError):
            invalidate_session_messages(
                state,owner="alice",session_id="chat-a",removed_message_ids=[bad],
            )
    assert queue.get(row["id"]) is not None


def test_reconcile_recovers_deleted_assistant_or_edited_user_source(setup, tmp_path):
    state, queue, policy, add = setup
    user = "We investigated a tricky source-evidence problem in Mabel today."
    assistant = "I found a source-receipt mismatch and explained the observation."
    first = add(sid="chat-a",mid="assistant-one",user=user,assistant=assistant)
    second = add(sid="chat-b",mid="assistant-two",user=user,assistant=assistant)
    third = add(sid="chat-c",mid="assistant-three",user=user,assistant=assistant)
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.sqlite'}")
    with engine.begin() as db:
        db.execute(text(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, "
            "memory_extraction_enabled BOOLEAN)"
        ))
        db.execute(text(
            "CREATE TABLE chat_messages(id TEXT PRIMARY KEY,session_id TEXT,"
            "role TEXT,content TEXT,timestamp TEXT)"
        ))
        for sid, mid in [
            ("chat-a","assistant-one"),
            ("chat-b","assistant-two"),
            ("chat-c","assistant-three"),
        ]:
            db.execute(
                text("INSERT INTO sessions VALUES(:sid,'alice',1)"), {"sid":sid},
            )
            db.execute(
                text("INSERT INTO chat_messages VALUES(:id,:sid,'user',:content,:at)"),
                {"id":"user-"+sid,"sid":sid,"content":user,"at":"2026-11-01T10:00:00"},
            )
            db.execute(
                text("INSERT INTO chat_messages VALUES(:id,:sid,'assistant',:content,:at)"),
                {"id":mid,"sid":sid,"content":assistant,"at":"2026-11-01T10:01:00"},
            )
        db.execute(text("DELETE FROM chat_messages WHERE id='assistant-one'"))
        db.execute(
            text("UPDATE chat_messages SET content='A completely edited user message.' "
                 "WHERE id='user-chat-b'")
        )
    with Session(engine) as db:
        result = reconcile_invalid_sources(queue, db=db,limit=100)
    assert result["checked_candidates"] == 3
    assert result["scrubbed"] == 2
    assert result["deleted_sessions"] == 0
    assert result["next_after_candidate_id"] is None
    assert queue.get(first["id"]) is None
    assert queue.get(second["id"]) is None
    assert queue.get(third["id"]) is not None
    assert queue.get(third["id"])["state"] == "pending"
    engine.dispose()


def test_missing_source_reconciliation_can_page_deterministically(setup, tmp_path):
    state, queue, policy, add = setup
    rows = [add(sid=f"chat-{n}", mid=f"reply-{n}") for n in range(3)]
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.sqlite'}")
    with engine.begin() as db:
        db.execute(text("CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT)"))
    cursors = []
    cursor = None
    scrubbed = 0
    with Session(engine) as db:
        for _ in range(3):
            result = reconcile_invalid_sources(
                queue,db=db,limit=1,after_candidate_id=cursor,
            )
            scrubbed += result["scrubbed"]
            cursor = result["next_after_candidate_id"]
            cursors.append(cursor)
            if cursor is None:
                break
    assert scrubbed == 3
    assert cursors[-1] is None
    assert all(queue.get(row["id"]) is None for row in rows)
    engine.dispose()


def _real_session(tmp_path, monkeypatch):
    import core.session_manager as sm
    from core.database import Base, Session as DbSession, ChatMessage as DbMessage
    from core.database import utcnow_naive
    from src import constants

    engine = create_engine(f"sqlite:///{tmp_path / 'message-manager.sqlite'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    sid = "edit-chat"
    stamp = utcnow_naive()
    with factory() as db:
        db.add(DbSession(id=sid,owner="alice",name="Synthetic",
                         endpoint_url="test",model="test"))
        db.flush()
        for idx, (mid, role, content) in enumerate((
            ("user-one","user","First question with enough content for memory."),
            ("assistant-one","assistant","First substantive assistant response with a separate source."),
            ("user-two","user","Second question which will be removed."),
            ("assistant-two","assistant","Second assistant response which will be removed."),
        )):
            db.add(DbMessage(
                id=mid,session_id=sid,role=role,content=content,
                timestamp=stamp + timedelta(seconds=idx),
            ))
        db.commit()
    monkeypatch.setattr(sm, "SessionLocal", factory)
    monkeypatch.setattr(sm, "reserve_message_upload_references", lambda *a, **k: None)
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    manager = sm.SessionManager.__new__(sm.SessionManager)
    manager.sessions = {}
    live = SimpleNamespace(
        owner="alice",history=[
            LiveMessage(role="user",content="First question with enough content for memory."),
            LiveMessage(role="assistant",content="First substantive assistant response with a separate source."),
            LiveMessage(role="user",content="Second question which will be removed."),
            LiveMessage(role="assistant",content="Second assistant response which will be removed."),
        ],
        message_count=4,
    )
    manager.get_session = lambda session_id: live
    return engine, factory, manager, live


def test_truncate_messages_propagates_exact_deleted_receipts_after_commit(setup, tmp_path, monkeypatch):
    state, queue, policy, add = setup
    first = add(sid="edit-chat",mid="assistant-one",
                user="First question with enough content for memory.",
                assistant="First substantive assistant response with a separate source.")
    second = add(sid="edit-chat",mid="assistant-two",
                 user="Second question which will be removed.",
                 assistant="Second assistant response which will be removed.")
    engine, factory, manager, live = _real_session(tmp_path,monkeypatch)
    assert manager.truncate_messages("edit-chat",2)
    assert len(live.history) == 2
    with factory() as db:
        from core.database import ChatMessage as DbMessage
        assert db.query(DbMessage).filter(DbMessage.session_id=="edit-chat").count() == 2
    assert queue.get(first["id"]) is not None
    assert queue.get(second["id"]) is None
    with pytest.raises(CandidateAdmissionError, match="recapture forbidden"):
        add(sid="edit-chat",mid="assistant-two",
            user="Second question which will be removed.",
            assistant="Second assistant response which will be removed.")
    engine.dispose()


def test_compaction_replacement_invalidates_all_prior_source_ids_but_allows_new_messages(setup, tmp_path, monkeypatch):
    state, queue, policy, add = setup
    row = add(sid="edit-chat",mid="assistant-one",
              user="First question with enough content for memory.",
              assistant="First substantive assistant response with a separate source.")
    engine, factory, manager, live = _real_session(tmp_path,monkeypatch)
    new_history = [
        LiveMessage(role="system",content="[Conversation summary] We discussed evidence."),
        LiveMessage(role="assistant",content="The new response belongs to the compacted transcript."),
    ]
    assert manager.replace_messages("edit-chat",new_history)
    assert queue.get(row["id"]) is None
    with pytest.raises(CandidateAdmissionError, match="recapture forbidden"):
        add(sid="edit-chat",mid="assistant-one",
            user="First question with enough content for memory.",
            assistant="First substantive assistant response with a separate source.")
    assert not queue.list(state="pending")
    new_id = new_history[-1].metadata["_db_id"]
    assert add(sid="edit-chat",mid=new_id,
               user="A fresh user message after compaction can still make memory.",
               assistant="A new independently saved assistant response remains eligible.")["created"]
    engine.dispose()


def test_additive_schema_v3_to_v4_preserves_prior_candidates(setup):
    state, queue, policy, add = setup
    before = add()
    with queue._connect() as db:
        db.execute("DROP TABLE invalidated_messages")
        db.execute("PRAGMA user_version=3")
    after = CandidateQueue(state,owner="alice")
    assert after.get(before["id"]) is not None
    with after._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert invalidate_session_messages(
        state,owner="alice",session_id="chat-a",removed_message_ids=["assistant-one"],
    )["scrubbed"] == 1
