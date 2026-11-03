"""Two-hop associations only from still-valid promoted conversation evidence."""
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.episodic_memory import (
    CandidateAdmissionError, CandidateQueue, CapturePolicy,
    capture_completed_turn, promote_verified_conversation,
    purge_deleted_session,
)
from src.episodic_memory.associations import related_verified_episodes


@pytest.fixture
def graph(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, owner TEXT, "
            "memory_extraction_enabled BOOLEAN, memory_injection_enabled BOOLEAN)"
        ))
        conn.execute(text(
            "CREATE TABLE chat_messages (id TEXT PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp TEXT)"
        ))
    state = tmp_path / "local-state"
    queue = CandidateQueue(state, owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    vault = tmp_path / "mabel-vault"
    vault.mkdir()
    protected = tmp_path / "core-of-truth"
    protected.mkdir()
    (protected / "private.md").write_text("Not for episodic retrieval",encoding="utf-8")
    counter = 0

    def stage(sid, user, assistant):
        nonlocal counter
        counter += 1
        suffix = f"{counter:03d}"
        user_id, assistant_id = "user-" + suffix, "assistant-" + suffix
        with engine.begin() as conn:
            conn.execute(
                text("INSERT OR IGNORE INTO sessions VALUES(:sid,'alice',1,1)"),
                {"sid":sid},
            )
            conn.execute(
                text("INSERT INTO chat_messages VALUES(:id,:sid,'user',:body,:ts)"),
                {"id":user_id,"sid":sid,"body":user,
                 "ts":f"2026-11-01T10:{counter * 2:02d}:00"},
            )
            conn.execute(
                text("INSERT INTO chat_messages VALUES(:id,:sid,'assistant',:body,:ts)"),
                {"id":assistant_id,"sid":sid,"body":assistant,
                 "ts":f"2026-11-01T10:{counter * 2 + 1:02d}:00"},
            )
        return capture_completed_turn(
            storage_dir=state, owner="alice",session_id=sid,
            assistant_message_id=assistant_id,
            user_message=user,assistant_message=assistant,policy=policy,
        )

    def promote(row):
        with Session(engine) as db:
            return promote_verified_conversation(
                queue,row["id"],db=db,vault_root=vault,
                protected_roots=(protected,),policy=policy,approved=True,
            )

    def related(seed, **kwargs):
        opts = dict(
            queue=queue,seed_candidate_id=seed["id"],
            vault_root=vault,protected_roots=(protected,),
            policy=policy,approved=True,
        )
        opts.update(kwargs)
        with Session(engine) as db:
            return related_verified_episodes(db=db,**opts)

    def populate():
        seed = stage(
            "shared-session",
            "We configured Tailscale networking to reach the Gemma Dottie machine safely.",
            "The Tailscale tunnel endpoint now works with scoped authentication.",
        )
        lexical = stage(
            "network-session",
            "The Tailscale endpoint for Dottie Gemma needs another authentication check.",
            "We confirmed the endpoint supports bounded Dottie review requests.",
        )
        same = stage(
            "shared-session",
            "Background indexing used BGE embeddings and reranking of source chunks.",
            "The independent indexing job remains separate from chat.",
        )
        second_hop = stage(
            "search-session",
            "Our indexing pipeline uses embeddings and reranking for knowledge retrieval.",
            "The embedding cache should keep the knowledge index rebuildable.",
        )
        unrelated = stage(
            "unrelated-session",
            "We assembled a modular synthesizer patch using oscillator and envelopes.",
            "The sequencer and the analog filter made that patch much easier to play.",
        )
        for item in (seed,lexical,same,second_hop,unrelated):
            promote(item)
        return seed,lexical,same,second_hop,unrelated

    yield engine,state,queue,policy,vault,protected,stage,promote,related,populate
    engine.dispose()


def _ids(result):
    return {n["candidate_id"] for n in result["nodes"]}


def _edge_for(result, a, b):
    expected = {a["id"], b["id"]}
    return next(
        edge for edge in result["edges"]
        if {edge["source_id"],edge["target_id"]} == expected
    )


def test_lexical_and_same_session_provenance_with_two_hops(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, lexical, same, second_hop, unrelated = populate()
    before = {str(path): path.read_bytes() for path in vault.rglob("*.md")}
    result = related(seed)
    assert result["mode"] == "explicit_read_only_associations"
    assert result["seed_candidate_id"] == seed["id"]
    assert result["skipped_invalid"] == 0
    assert result["scanned"] == 5
    assert seed["id"] in _ids(result)
    assert lexical["id"] in _ids(result)
    assert same["id"] in _ids(result)
    assert second_hop["id"] in _ids(result)
    assert unrelated["id"] not in _ids(result)

    shared = _edge_for(result, seed, lexical)
    assert "shared_source_terms" in shared["relation_types"]
    assert {"tailscale","dottie"}.issubset(set(shared["shared_terms"]))
    assert shared["association_score"] >= 4
    assert shared["evidence_kind"] == "observed_transcript_relationship"

    session = _edge_for(result, seed, same)
    assert "same_source_session" in session["relation_types"]
    assert session["source_id"] != session["target_id"]
    hop = _edge_for(result, same, second_hop)
    assert "shared_source_terms" in hop["relation_types"]
    assert {"indexing","embeddings"}.issubset(set(hop["shared_terms"]))
    assert {n["candidate_id"]:n["distance"] for n in result["nodes"]}[second_hop["id"]] == 2
    assert {str(path):path.read_bytes() for path in vault.rglob("*.md")} == before
    assert (protected/"private.md").read_text() == "Not for episodic retrieval"


def test_depth_one_does_not_inject_second_hop(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, lexical, same, second_hop, unrelated = populate()
    found = related(seed,max_depth=1)
    assert same["id"] in _ids(found)
    assert lexical["id"] in _ids(found)
    assert second_hop["id"] not in _ids(found)
    assert max(n["distance"] for n in found["nodes"]) == 1


def test_unpromoted_or_invalid_seed_is_rejected(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    pending = stage(
        "pending-session",
        "We investigated why a source transcript is not an automatic truth claim.",
        "A saved answer requires its own approval and independent source evidence.",
    )
    with pytest.raises(CandidateAdmissionError,match="not a promoted"):
        related(pending)
    with pytest.raises(CandidateAdmissionError,match="Invalid association seed"):
        related(pending,seed_candidate_id="../bad")


def test_tampered_or_deleted_intermediate_is_not_related(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, lexical, same, second_hop, unrelated = populate()
    note = next(vault.rglob(f"{same['id']}.md"))
    note.write_text("Someone changed the Markdown source.")
    result = related(seed)
    assert same["id"] not in _ids(result)
    assert second_hop["id"] not in _ids(result)
    assert result["skipped_invalid"] == 1
    # A source deletion also removes all of its outgoing relations.
    purge_deleted_session(state, owner="alice", session_id="network-session")
    assert lexical["id"] not in _ids(related(seed))


def test_changed_canonical_source_or_disabled_memory_cannot_link(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed,lexical,same,second_hop,unrelated=populate()
    # Modify the actual assistant receipt, not candidate metadata.
    with engine.begin() as db:
        db.execute(text(
            "UPDATE chat_messages SET content='Rewritten response' "
            "WHERE session_id='network-session' AND role='assistant'"
        ))
        db.execute(text(
            "UPDATE sessions SET memory_injection_enabled=0 "
            "WHERE id='search-session'"
        ))
    result=related(seed)
    assert lexical["id"] not in _ids(result)
    assert second_hop["id"] not in _ids(result)
    assert result["skipped_invalid"] == 2


@pytest.mark.parametrize("bad_policy", [
    CapturePolicy(),
    CapturePolicy(episodic_enabled=True,memory_enabled=False),
    CapturePolicy(episodic_enabled=True,memory_enabled=True,incognito=True),
])
def test_memory_controls_and_consent_block_graph(graph,bad_policy):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, *_ = populate()
    with pytest.raises(PermissionError):
        related(seed,policy=bad_policy)
    with pytest.raises(PermissionError,match="Explicit read consent"):
        related(seed,approved=False)


def test_different_owner_and_protected_roots_are_rejected(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, *_ = populate()
    bob=CandidateQueue(state,owner="bob")
    with Session(engine) as db:
        with pytest.raises(CandidateAdmissionError,match="not a promoted"):
            related_verified_episodes(
                bob,seed["id"],db=db,vault_root=vault,
                protected_roots=(protected,),policy=policy,approved=True,
            )
    with pytest.raises(CandidateAdmissionError,match="protected"):
        related(seed,vault_root=protected)
    with pytest.raises(CandidateAdmissionError,match="protected"):
        related(seed,protected_roots=())


def test_bounded_nodes_edges_and_cursor_are_explicit(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, lexical, same, second_hop, unrelated = populate()
    # Only the seed and at most one linked neighbor, no more than one edge.
    result = related(seed,max_nodes=2,max_edges=1)
    assert len(result["nodes"]) == 2
    assert len(result["edges"]) == 1
    assert result["truncated"]

    page = related(seed,scan_limit=1)
    assert page["scanned"] == 1
    assert page["next_after_candidate_id"] is not None
    second = related(seed,scan_limit=1,
                     after_candidate_id=page["next_after_candidate_id"])
    assert second["scanned"] == 1
    assert page["next_after_candidate_id"] != second.get("next_after_candidate_id")
    assert all(n["evidence_status"] == "transcript_verified" for n in result["nodes"])


@pytest.mark.parametrize("kwargs", [
    {"max_depth":0},{"max_depth":3},{"max_nodes":1},{"max_nodes":31},
    {"max_edges":0},{"max_edges":101},{"scan_limit":0},{"scan_limit":501},
    {"after_candidate_id":"wrong"},
])
def test_unbounded_graph_requests_rejected(graph,kwargs):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, *_ = populate()
    with pytest.raises(CandidateAdmissionError):
        related(seed,**kwargs)


def test_symlinked_episode_note_and_bucket_are_rejected(graph):
    engine,state,queue,policy,vault,protected,stage,promote,related,populate = graph
    seed, lexical, same, second_hop, unrelated = populate()
    path=next(vault.rglob(f"{lexical['id']}.md"))
    path.unlink()
    path.symlink_to(protected/"private.md")
    assert lexical["id"] not in _ids(related(seed))
    # The entire bucket may not be replaced by an untrusted link.
    owner_dir=path.parent
    owner_dir.rename(owner_dir.with_name(owner_dir.name+"-moved"))
    owner_dir.symlink_to(protected,target_is_directory=True)
    with pytest.raises(CandidateAdmissionError,match="Symlinked"):
        related(seed)
