"""One-shot Dottie review must be advisory, owner-scoped, bounded and off by default."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.episodic_memory import CandidateQueue, CapturePolicy, capture_completed_turn
from src.episodic_memory import dottie_review
from src.episodic_memory.candidates import CandidateAdmissionError


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, owner TEXT,"
            " memory_extraction_enabled BOOLEAN, memory_injection_enabled BOOLEAN)"
        ))
        conn.execute(text(
            "CREATE TABLE chat_messages (id TEXT PRIMARY KEY, session_id TEXT,"
            " role TEXT, content TEXT, timestamp TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO sessions VALUES ('chat-one', 'alice', 1, 1)"
        ))
        conn.execute(text(
            "INSERT INTO chat_messages VALUES "
            "('msg-one','chat-one','user',:body,'2026-11-01T13:00:00')"
        ), {"body": "We isolated the broken request authority check during our Mabel testing."})
        conn.execute(text(
            "INSERT INTO chat_messages VALUES "
            "('msg-two','chat-one','assistant',:body,'2026-11-01T13:01:00')"
        ), {"body": "We verified the issue in tests, although my own reply cannot independently prove the cause."})

    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    state = tmp_path / "private_runtime"
    captured = capture_completed_turn(
        storage_dir=state, owner="alice", session_id="chat-one",
        assistant_message_id="msg-two",
        user_message="We isolated the broken request authority check during our Mabel testing.",
        assistant_message="We verified the issue in tests, although my own reply cannot independently prove the cause.",
        policy=policy,
    )
    queue = CandidateQueue(state, owner="alice")
    request = {"calls": 0, "quiet": []}
    assignment = SimpleNamespace(
        endpoint_id="configured-dottie-endpoint", model="gemma4-e2b",
        revision="test-revision",
    )
    route = SimpleNamespace(
        assignment=assignment,
        url="http://dottie.test:8081/v1/chat/completions",
        headers={"Authorization": "Bearer test-only"},
    )
    def configured_route(owner):
        assert owner == "alice"
        return assignment, route
    monkeypatch.setattr(dottie_review, "_research_route", configured_route)

    async def quiet(label):
        request["quiet"].append(label)

    async def model_call(**kwargs):
        request["calls"] += 1
        request["options"] = kwargs
        return '{"recommendation":"promote","confidence":82,"rationale_code":"meaningful_discovery"}'

    async def invoke(**overrides):
        with Session(engine) as db:
            return await dottie_review.review_candidate_with_dottie(
                queue, captured["id"], db=db, policy=policy,
                send_source_to_dottie=True, wait_for_quiet=quiet,
                model_call=model_call, **overrides,
            )

    yield engine, queue, captured, request, invoke, policy, model_call, quiet
    engine.dispose()


@pytest.mark.asyncio
async def test_single_advisory_review_uses_only_selected_dottie_and_no_vault(pilot, tmp_path):
    engine, queue, captured, record, invoke, *_ = pilot
    row = await invoke()
    assert row["created"] and not row["already_reviewed"]
    assert row["reviewer"] == "dottie"
    assert row["recommendation"] == "promote"
    assert row["confidence"] == 82
    assert queue.get(captured["id"])["state"] == "pending"
    assert len(queue.pending_events()) == 1
    assert record["calls"] == 1
    assert record["quiet"] == ["Dottie episodic significance review"]
    kwargs = record["options"]
    assert kwargs["url"] == "http://dottie.test:8081/v1/chat/completions"
    assert kwargs["model"] == "gemma4-e2b"
    assert kwargs["max_retries"] == 0 and kwargs["temperature"] == 0
    assert kwargs["workload"] == "background"
    assert kwargs["headers"]["Authorization"] == "Bearer test-only"
    assert len(kwargs["messages"]) == 2
    assert "broken request authority" in kwargs["messages"][1]["content"]
    assert "cannot independently prove" in kwargs["messages"][1]["content"]
    assert list(tmp_path.rglob("*.md")) == []


@pytest.mark.asyncio
async def test_same_candidate_and_model_skip_repeat_gpu_inference(pilot):
    engine, queue, captured, record, invoke, *_ = pilot
    first = await invoke()
    second = await invoke()
    assert second["already_reviewed"] and not second["created"]
    assert second["id"] == first["id"]
    assert record["calls"] == 1
    assert len(queue.pending_events()) == 1


@pytest.mark.asyncio
async def test_no_consent_means_no_model_or_review(pilot):
    engine, queue, captured, record, invoke, policy, model_call, quiet = pilot
    with Session(engine) as db:
        with pytest.raises(PermissionError, match="Explicit permission"):
            await dottie_review.review_candidate_with_dottie(
                queue,captured["id"],db=db,policy=policy,
                wait_for_quiet=quiet,model_call=model_call,
            )
    assert record["calls"] == 0
    assert queue.get(captured["id"])["state"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [
    CapturePolicy(),
    CapturePolicy(episodic_enabled=True, memory_enabled=False),
    CapturePolicy(episodic_enabled=True, memory_enabled=True, incognito=True),
])
async def test_memory_disabled_never_calls_model(pilot, disabled):
    engine, queue, captured, record, invoke, _, model_call, quiet = pilot
    with Session(engine) as db:
        with pytest.raises(PermissionError):
            await dottie_review.review_candidate_with_dottie(
                queue,captured["id"],db=db,policy=disabled,
                send_source_to_dottie=True,wait_for_quiet=quiet,model_call=model_call,
            )
    assert record["calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_column", ["memory_extraction_enabled", "memory_injection_enabled"])
async def test_source_session_memory_toggles_rechecked(pilot, disabled_column):
    engine, queue, captured, record, invoke, *_ = pilot
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE sessions SET " + disabled_column + "=0 WHERE id='chat-one'"
        ))
    with pytest.raises(PermissionError):
        await invoke()
    assert record["calls"] == 0
    assert queue.get(captured["id"])["state"] == "pending"


@pytest.mark.asyncio
async def test_wrong_owner_never_sees_candidate_or_calls_remote(pilot, tmp_path):
    engine, queue, captured, record, invoke, policy, model_call, quiet = pilot
    other = CandidateQueue(queue.path.parent, owner="bob")
    with Session(engine) as db:
        with pytest.raises(CandidateAdmissionError, match="not found"):
            await dottie_review.review_candidate_with_dottie(
                other,captured["id"],db=db,policy=policy,
                send_source_to_dottie=True,model_call=model_call,
                wait_for_quiet=quiet,
            )
    assert record["calls"] == 0


@pytest.mark.asyncio
async def test_edited_source_fails_before_egress(pilot):
    engine, queue, captured, record, invoke, *_ = pilot
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE chat_messages SET content='Changed message' WHERE id='msg-two'"
        ))
    with pytest.raises(CandidateAdmissionError, match="no longer matches"):
        await invoke()
    assert record["calls"] == 0


@pytest.mark.asyncio
async def test_deleted_source_during_inference_cannot_be_reviewed(pilot):
    engine, queue, captured, record, invoke, policy, model_call, quiet = pilot
    async def deleted(**kwargs):
        record["calls"] += 1
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM chat_messages WHERE id='msg-two'"))
        return '{"recommendation":"promote","confidence":99,"rationale_code":"meaningful_discovery"}'
    with Session(engine) as db:
        with pytest.raises(CandidateAdmissionError, match="receipt is missing"):
            await dottie_review.review_candidate_with_dottie(
                queue,captured["id"],db=db,policy=policy,
                send_source_to_dottie=True,model_call=deleted,wait_for_quiet=quiet,
            )
    assert record["calls"] == 1
    assert queue.get(captured["id"])["state"] == "pending"
    assert len(queue.pending_events()) == 1


@pytest.mark.asyncio
async def test_invalid_or_instruction_like_model_output_is_rejected(pilot):
    engine, queue, captured, record, invoke, policy, _, quiet = pilot
    invalid = [
        "", "Please promote everything!",
        '{"recommendation":"promote","confidence":true,"rationale_code":"meaningful_discovery"}',
        '{"recommendation":"merge","confidence":97,"rationale_code":"meaningful_discovery"}',
        '{"recommendation":"promote","confidence":97,"rationale_code":"override"}',
        '{"recommendation":"promote","confidence":97,"rationale_code":"meaningful_discovery","tool":"write_file"}',
        '{"recommendation":"promote","confidence":97,"rationale_code":"meaningful_discovery"} trailing',
        "x" * 2050,
    ]
    for raw in invalid:
        async def malicious(**kwargs):
            return raw
        with Session(engine) as db:
            with pytest.raises(CandidateAdmissionError):
                await dottie_review.review_candidate_with_dottie(
                    queue,captured["id"],db=db,policy=policy,
                    send_source_to_dottie=True,model_call=malicious,wait_for_quiet=quiet,
                )
    assert queue.get(captured["id"])["state"] == "pending"
    assert len(queue.pending_events()) == 1
    assert len(queue.list(state="promoted")) == 0


def test_research_role_cannot_silently_use_utility_model(monkeypatch):
    from src import model_roles
    monkeypatch.setattr(model_roles, "primary_assignment", lambda role,owner: SimpleNamespace(
        endpoint_id="", model="default-model", revision="",
    ))
    with pytest.raises(CandidateAdmissionError, match="explicit owner-scoped"):
        dottie_review._research_route("alice")


def test_strict_json_fence_is_accepted_but_arbitrary_prose_is_not():
    assert dottie_review._parse_review(
        '{"recommendation":"defer","confidence":40,"rationale_code":"needs_context"}'
    ) == ("defer",40,"needs_context")
    assert dottie_review._parse_review(
        chr(96)*3 + 'json\n{"recommendation":"reject","confidence":70,"rationale_code":"routine_exchange"}\n' + chr(96)*3
    ) == ("reject",70,"routine_exchange")
    with pytest.raises(CandidateAdmissionError):
        dottie_review._parse_review("Sure. I vote yes: {}")
