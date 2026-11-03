"""One-shot Dottie BGE-M3 semantic associations are advisory and source-verified."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src import model_roles
from src.episodic_memory import (
    CandidateAdmissionError, CandidateQueue, CapturePolicy,
    capture_completed_turn, promote_verified_conversation,
    purge_deleted_session,
)
from src.episodic_memory import semantic_advisory


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'canonical.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, owner TEXT, "
            "memory_extraction_enabled BOOLEAN, memory_injection_enabled BOOLEAN)"
        ))
        conn.execute(text(
            "CREATE TABLE chat_messages (id TEXT PRIMARY KEY,session_id TEXT, "
            "role TEXT,content TEXT,timestamp TEXT)"
        ))
    state = tmp_path / "staging"
    queue = CandidateQueue(state, owner="alice")
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    vault = tmp_path / "mabel-vault"
    vault.mkdir()
    protected = tmp_path / "core-of-truth"
    protected.mkdir()
    private = protected / "private.md"
    private.write_text("never read or modify private source-vault file")

    def stage(sid, user, assistant):
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO sessions VALUES(:sid,'alice',1,1)"),
                {"sid":sid},
            )
            conn.execute(text(
                "INSERT INTO chat_messages VALUES(:id,:sid,'user',:content,:stamp)"
            ), {"id":"user-"+sid,"sid":sid,"content":user,"stamp":"2026-11-01T10:00:00"})
            conn.execute(text(
                "INSERT INTO chat_messages VALUES(:id,:sid,'assistant',:content,:stamp)"
            ), {"id":"reply-"+sid,"sid":sid,"content":assistant,"stamp":"2026-11-01T10:01:00"})
        candidate = capture_completed_turn(
            storage_dir=state,owner="alice",session_id=sid,
            assistant_message_id="reply-"+sid,user_message=user,
            assistant_message=assistant,policy=policy,
        )
        with Session(engine) as db:
            promote_verified_conversation(
                queue,candidate["id"],db=db,vault_root=vault,
                protected_roots=(protected,),policy=policy,approved=True,
            )
        return candidate

    seed = stage(
        "chat-one",
        "Our encrypted Tailscale network connects Mabel's remote computers safely.",
        "We verified the secure overlay route for the private research machine.",
    )
    related = stage(
        "chat-two",
        "The WireGuard mesh VPN gives authenticated connectivity between our devices.",
        "This encrypted peer connection supports trusted remote model inference.",
    )
    unrelated = stage(
        "chat-three",
        "The SQ64 sequencer sends MIDI clock to a modular synthesizer.",
        "We adjusted the Eurorack oscillator and filter envelope.",
    )
    assignment = model_roles.Assignment(
        endpoint_id="owner-approved-dottie-bge",
        model="bge-m3", adapter="openai_embeddings",
        capabilities=("embedding",), space_id="BAAI/bge-m3",
        revision="synthetic-revision", dimensions=3, normalization="l2",
    )
    route = model_roles.Route(
        assignment=assignment, url="http://dottie.test:8082/v1",
        headers={"Authorization":"Bearer synthetic-test-only"},
    )
    route_calls = []
    def selected(role,owner=""):
        route_calls.append((role,owner))
        return assignment
    def resolved(selected_assignment,owner="",required=None):
        assert selected_assignment is assignment
        assert owner == "alice"
        assert required == "embedding"
        return route
    monkeypatch.setattr(model_roles, "primary_assignment", selected)
    monkeypatch.setattr(model_roles, "resolve", resolved)
    record = {"inputs": [], "quiet": []}

    async def embed(route_arg, texts):
        assert route_arg is route
        record["inputs"].append(texts)
        vectors = []
        for content in texts:
            if "Tailscale" in content:
                vectors.append([1.0,0.0,0.0])
            elif "WireGuard" in content:
                vectors.append([0.95,0.31,0.0])
            elif "SQ64" in content:
                vectors.append([0.0,1.0,0.0])
            else:
                raise AssertionError("Unexpected unverified content sent to Dottie")
        return vectors

    async def quiet(label):
        record["quiet"].append(label)

    async def invoke(**kwargs):
        opts = dict(
            queue=queue,seed_candidate_id=seed["id"],
            vault_root=vault,protected_roots=(protected,),
            policy=policy,approved=True,send_source_to_embedding=True,
            min_similarity=0.75,embed_call=embed,wait_for_quiet=quiet,
        )
        opts.update(kwargs)
        with Session(engine) as db:
            return await semantic_advisory.suggest_semantic_episode_links(db=db,**opts)

    yield (
        engine,state,queue,policy,vault,protected,private,seed,
        related,unrelated,route_calls,record,invoke,embed,quiet,
    )
    engine.dispose()


@pytest.mark.asyncio
async def test_explicit_embedding_finds_semantic_overlap_without_shared_keywords(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    before={str(p):p.read_bytes() for p in vault.rglob("*.md")}
    events_before=queue.pending_events()
    result=await invoke()
    assert result["mode"]=="explicit_semantic_advisory"
    assert result["embedding_invoked"] is True
    assert result["scanned"]==3
    assert len(result["suggestions"])==1
    proposal=result["suggestions"][0]
    assert proposal["source_id"]==seed["id"]
    assert proposal["target_id"]==related["id"]
    assert proposal["relation_type"]=="semantic_similarity_suggestion"
    assert proposal["evidence_kind"]=="inference_over_verified_transcript_excerpts"
    assert proposal["cosine_similarity"]>0.9
    assert len(proposal["embedding_space"])==64
    assert "WireGuard" in proposal["user_excerpt"]
    assert route_calls==[("embedding","alice")]
    assert len(record["inputs"])==1
    assert len(record["inputs"][0])==3
    assert record["quiet"]==["Dottie episodic semantic association"]
    assert all(len(t)<=1100 for t in record["inputs"][0])
    assert not any("private source-vault" in t for t in record["inputs"][0])
    assert {str(p):p.read_bytes() for p in vault.rglob("*.md")}==before
    assert private.read_text()=="never read or modify private source-vault file"
    assert queue.pending_events()==events_before
    assert queue.get(related["id"])["state"]=="promoted"
    assert queue.get(unrelated["id"])["state"]=="promoted"


@pytest.mark.asyncio
async def test_no_consent_never_sends_text_to_remote(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    with pytest.raises(PermissionError,match="Explicit approval"):
        await invoke(send_source_to_embedding=False)
    with pytest.raises(PermissionError,match="Explicit approval"):
        await invoke(approved=False)
    assert record["inputs"]==[] and route_calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [
    CapturePolicy(),
    CapturePolicy(episodic_enabled=True,memory_enabled=False),
    CapturePolicy(episodic_enabled=True,memory_enabled=True,incognito=True),
])
async def test_disabled_turn_policy_prevents_embedding(pilot,blocked):
    *_,record,invoke,embed,quiet=pilot
    with pytest.raises(PermissionError):
        await invoke(policy=blocked)
    assert record["inputs"]==[]


@pytest.mark.asyncio
@pytest.mark.parametrize("field",["owner","memory_extraction_enabled","memory_injection_enabled"])
async def test_source_controls_checked_before_remote_egress(pilot,field):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    value="'bob'" if field=="owner" else "0"
    with engine.begin() as conn:
        conn.execute(text(
            f"UPDATE sessions SET {field}={value} WHERE id='chat-one'"
        ))
    with pytest.raises(CandidateAdmissionError,match="verified source"):
        await invoke()
    assert record["inputs"]==[]


@pytest.mark.asyncio
async def test_deleted_target_during_inference_is_excluded(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot

    async def delete_target(route, texts):
        vectors=await embed(route,texts)
        purge_deleted_session(state,owner="alice",session_id="chat-two")
        return vectors
    result=await invoke(embed_call=delete_target)
    assert result["embedding_invoked"]
    assert all(item["target_id"]!=related["id"] for item in result["suggestions"])
    assert result["skipped_invalid"]>=1
    assert queue.get(related["id"]) is None


@pytest.mark.asyncio
async def test_seed_deleted_during_inference_aborts_without_links(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot

    async def delete_seed(route,texts):
        vectors=await embed(route,texts)
        purge_deleted_session(state,owner="alice",session_id="chat-one")
        return vectors
    with pytest.raises(CandidateAdmissionError,match="Seed source was modified"):
        await invoke(embed_call=delete_seed)


@pytest.mark.asyncio
async def test_source_edit_during_inference_rechecks_canonical_transcript(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot

    async def edit_target(route,texts):
        vectors=await embed(route,texts)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE chat_messages SET content='Edited assistant reply' "
                "WHERE id='reply-chat-two'"
            ))
        return vectors
    result=await invoke(embed_call=edit_target)
    assert all(item["target_id"]!=related["id"] for item in result["suggestions"])
    assert result["skipped_invalid"]>=1


@pytest.mark.asyncio
async def test_malformed_embedding_output_does_not_produce_advisory_links(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    async def bad(route,texts):
        return [[1.0,0.0,0.0]]
    with pytest.raises(model_roles.CapabilityUnavailable,match="batch size"):
        await invoke(embed_call=bad)
    assert queue.get(related["id"])["state"]=="promoted"


@pytest.mark.asyncio
async def test_unconfigured_embedding_route_has_no_silent_fallback(pilot,monkeypatch):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    monkeypatch.setattr(model_roles,"primary_assignment",lambda role,owner="":model_roles.Assignment(
        endpoint_id="",model="bge-m3",adapter="openai_embeddings",
        capabilities=("embedding",),space_id="BAAI/bge-m3",
        revision="synthetic",dimensions=3,normalization="l2",
    ))
    with pytest.raises(CandidateAdmissionError,match="explicit owner-scoped"):
        await invoke()
    assert record["inputs"]==[]


@pytest.mark.asyncio
async def test_wrong_owner_cannot_request_seed_or_remote_embeddings(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    bob=CandidateQueue(state,owner="bob")
    with pytest.raises(CandidateAdmissionError,match="not a promoted"):
        await invoke(queue=bob)
    assert record["inputs"]==[] and route_calls==[]


@pytest.mark.asyncio
async def test_bounded_scans_and_pagination_and_invalid_thresholds(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    first=await invoke(scan_limit=1)
    assert first["scanned"]==1
    assert first["next_after_candidate_id"] is not None
    second=await invoke(scan_limit=1,after_candidate_id=first["next_after_candidate_id"])
    assert second["scanned"]==1
    assert second["next_after_candidate_id"]!=first["next_after_candidate_id"]
    for kwargs in (
        {"scan_limit":0},{"scan_limit":17},{"max_suggestions":0},
        {"max_suggestions":9},{"min_similarity":-1},
        {"min_similarity":2},{"min_similarity":float("nan")},
        {"after_candidate_id":"invalid"},
    ):
        with pytest.raises(CandidateAdmissionError):
            await invoke(**kwargs)


@pytest.mark.asyncio
async def test_missing_or_modified_promoted_note_never_exposed(pilot):
    (engine,state,queue,policy,vault,protected,private,seed,
     related,unrelated,route_calls,record,invoke,embed,quiet)=pilot
    note=next(vault.rglob(f"{related['id']}.md"))
    note.write_text("untrusted, modified memory")
    result=await invoke()
    assert result["skipped_invalid"]>=1
    assert all(x["target_id"]!=related["id"] for x in result["suggestions"])
    assert all("untrusted, modified" not in text for batch in record["inputs"] for text in batch)
