"""Read-only episodic recall against durable promoted Markdown and saved receipts."""
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.episodic_memory import (
    CandidateAdmissionError, CandidateQueue, CapturePolicy,
    capture_completed_turn, promote_verified_conversation,
    purge_deleted_session,
)
from src.episodic_memory.recall import search_verified_episodes


@pytest.fixture
def scenario(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'saved-chat.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, owner TEXT, "
            "memory_extraction_enabled BOOLEAN, memory_injection_enabled BOOLEAN)"
        ))
        conn.execute(text(
            "CREATE TABLE chat_messages (id TEXT PRIMARY KEY,session_id TEXT,"
            "role TEXT,content TEXT,timestamp TEXT)"
        ))
    state = tmp_path / "local-state"
    queue = CandidateQueue(state, owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    vault = tmp_path / "mabel-vault"
    vault.mkdir()
    protected = tmp_path / "core-of-truth"
    protected.mkdir()
    private = protected / "secret.md"
    private.write_text("source vault unchanged", encoding="utf-8")

    def stage(sid="chat-one", mid="answer-one",
              user="We investigated a permission boundary in Mabel's tool router.",
              assistant="We confirmed the tool authorization path in synthetic tests.") -> dict:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO sessions VALUES(:sid,'alice',1,1)"),
                {"sid":sid},
            )
            conn.execute(text(
                "INSERT INTO chat_messages VALUES(:id,:sid,'user',:body,:ts)"
            ), {"id":"user-"+sid,"sid":sid,"body":user,
                "ts":"2026-11-01T10:00:00"})
            conn.execute(text(
                "INSERT INTO chat_messages VALUES(:id,:sid,'assistant',:body,:ts)"
            ), {"id":mid,"sid":sid,"body":assistant,
                "ts":"2026-11-01T10:01:00"})
        return capture_completed_turn(
            storage_dir=state,owner="alice",
            session_id=sid,assistant_message_id=mid,
            user_message=user,assistant_message=assistant,policy=policy,
        )

    def promote(candidate):
        with Session(engine) as db:
            return promote_verified_conversation(
                queue,candidate["id"],db=db,
                vault_root=vault,protected_roots=(protected,),
                policy=policy,approved=True,
            )

    def search(query, **overrides):
        opts={
            "queue":queue,"db":None,"vault_root":vault,
            "protected_roots":(protected,),"policy":policy,
            "approved":True,
        }
        opts.update(overrides)
        with Session(engine) as db:
            opts["db"]=db
            return search_verified_episodes(query=query,**opts)

    yield engine,state,queue,policy,vault,protected,private,stage,promote,search
    engine.dispose()


def test_only_promoted_exact_digest_and_source_verified_returns(scenario):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    row = stage()
    assert search("permission")["results"] == []
    promoted = promote(row)
    before = set(vault.rglob("*"))
    result = search("permission boundary")
    assert result["mode"] == "explicit_read_only"
    assert result["scanned"] == 1
    assert result["matched"] == 1
    assert result["skipped_invalid"] == 0
    hit=result["results"][0]
    assert hit["candidate_id"] == row["id"]
    assert hit["evidence_status"] == "transcript_verified"
    assert "permission boundary" in hit["user_excerpt"]
    assert "tool authorization" in hit["assistant_excerpt"]
    assert Path(hit["note_path"]) == Path(promoted["episode_path"])
    assert hit["lexical_score"] >= 2
    assert sha256(Path(hit["note_path"]).read_bytes()).hexdigest() == promoted["decision_reason"]
    assert set(vault.rglob("*")) == before
    assert private.read_text() == "source vault unchanged"


def test_tampered_or_missing_markdown_is_excluded_without_leaking_text(scenario):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    candidate=stage()
    result=promote(candidate)
    path=Path(result["episode_path"])
    path.write_text("manually edited")
    found=search("permission")
    assert found["results"] == []
    assert found["skipped_invalid"] == 1
    path.unlink()
    assert search("permission")["results"] == []


def test_symlinked_note_and_protected_source_vault_fail_closed(scenario):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    candidate=stage()
    result=promote(candidate)
    path=Path(result["episode_path"])
    path.unlink()
    path.symlink_to(private)
    assert search("permission")["results"] == []
    assert private.read_text() == "source vault unchanged"
    with pytest.raises(CandidateAdmissionError, match="protected"):
        search("permission",vault_root=protected)
    path.unlink()
    (vault / "Memory").rename(vault / "episodes-moved")
    (vault / "Memory").symlink_to(protected,target_is_directory=True)
    with pytest.raises(CandidateAdmissionError, match="Symlinked"):
        search("permission")


@pytest.mark.parametrize("bad_policy",[
    CapturePolicy(),
    CapturePolicy(episodic_enabled=True,memory_enabled=False),
    CapturePolicy(episodic_enabled=True,memory_enabled=True,incognito=True),
])
def test_consent_and_memory_controls_block_recall(scenario,bad_policy):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    candidate=stage()
    promote(candidate)
    with pytest.raises(PermissionError):
        search("permission",policy=bad_policy)
    with pytest.raises(PermissionError,match="Explicit read consent"):
        search("permission",approved=False)


@pytest.mark.parametrize("column",[
    "owner", "memory_extraction_enabled", "memory_injection_enabled",
])
def test_source_owner_and_session_memory_toggles_block_recall(scenario,column):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    candidate=stage()
    promote(candidate)
    value="'bob'" if column=="owner" else "0"
    with engine.begin() as db:
        db.execute(text(
            "UPDATE sessions SET "+column+"="+value+" WHERE id='chat-one'"
        ))
    found=search("permission")
    assert found["results"] == []
    assert found["skipped_invalid"] == 1


def test_changed_or_deleted_canonical_transcript_never_returns_stale_excerpts(scenario):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    candidate=stage()
    promote(candidate)
    with engine.begin() as db:
        db.execute(text(
            "UPDATE chat_messages SET content='changed user text' WHERE id='user-chat-one'"
        ))
    assert search("permission")["results"] == []
    with engine.begin() as db:
        db.execute(text(
            "DELETE FROM chat_messages WHERE id='answer-one'"
        ))
    assert search("permission")["results"] == []


def test_deleted_session_pending_note_cleanup_never_leaks_in_search(scenario):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    candidate=stage()
    promoted=promote(candidate)
    result=purge_deleted_session(state,owner="alice",session_id="chat-one")
    assert result["scrubbed"] == 1
    assert result["pending_episode_files"] == 1
    assert search("permission")["results"] == []
    assert Path(promoted["episode_path"]).exists()


def test_ranked_lexical_scoring_and_bounded_pagination(scenario):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    one=stage(
        sid="chat-one",mid="answer-one",
        user="The permission boundary is still important to our workflow.",
        assistant="We should retain a clear source receipt.",
    )
    two=stage(
        sid="chat-two",mid="answer-two",
        user="We discussed two other unrelated background processes.",
        assistant="The permission boundary is worth preserving.",
    )
    promote(one)
    promote(two)
    all_found=search("permission boundary")
    assert len(all_found["results"]) == 2
    assert all_found["results"][0]["candidate_id"]==one["id"]
    assert all_found["results"][0]["lexical_score"]>all_found["results"][1]["lexical_score"]

    first=search("permission boundary",scan_limit=1,max_results=1)
    assert first["scanned"]==1
    assert first["next_after_candidate_id"] is not None
    second=search(
        "permission boundary",scan_limit=1,max_results=1,
        after_candidate_id=first["next_after_candidate_id"],
    )
    assert second["scanned"]==1
    assert second["next_after_candidate_id"] is None
    assert {r["candidate_id"] for r in first["results"]+second["results"]}=={
        one["id"],two["id"],
    }


def test_owner_isolation_even_with_matching_search_words(scenario,tmp_path):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    original=stage()
    promote(original)
    bob=CandidateQueue(state,owner="bob")
    with Session(engine) as db:
        result=search_verified_episodes(
            bob,"permission",db=db,vault_root=vault,
            protected_roots=(protected,),policy=policy,approved=True,
        )
    assert result["results"] == []
    assert result["scanned"] == 0


@pytest.mark.parametrize("query",["","x","*"*256,"z"*257])
def test_unusable_queries_fail_closed(scenario,query):
    engine,state,queue,policy,vault,protected,private,stage,promote,search=scenario
    with pytest.raises(CandidateAdmissionError):
        search(query)
