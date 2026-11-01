"""Completed-turn capture is opt-in, source-linked and never self-verifying."""
from types import SimpleNamespace

import pytest

from src.episodic_memory import (
    CandidateQueue, CapturePolicy, capture_completed_turn,
)


def _capture(tmp_path, **overrides):
    settings = dict(
        storage_dir=tmp_path / "runtime",
        owner="alice",
        session_id="session-123",
        assistant_message_id="message-42",
        user_message="We figured out the broken permission route in Mabel today.",
        assistant_message="I checked the tool calls and found the permission mismatch.",
        policy=CapturePolicy(episodic_enabled=True, memory_enabled=True),
    )
    settings.update(overrides)
    return capture_completed_turn(**settings)


def test_completed_turn_is_source_linked_and_not_verified(tmp_path):
    row = _capture(tmp_path)
    assert row and row["created"] is True and row["state"] == "pending"
    assert row["source_kind"] == "conversation"
    assert row["source_ref"] == "chat:session-123:message:message-42"
    assert row["source_event_id"] == "message:message-42"
    assert row["evidence_status"] == "unverified"
    assert "User said:" in row["summary"] and "Mabel replied:" in row["summary"]
    assert row["interpretation"] == ""
    assert _capture(tmp_path)["created"] is False


@pytest.mark.parametrize("overrides", [
    {"assistant_message_id": None},
    {"assistant_message_id": "../not-a-db-id"},
    {"session_id": ""},
    {"session_id": "../unsafe"},
    {"user_message": "hi"},
    {"assistant_message": "OK"},
    {"policy": CapturePolicy()},
    {"policy": CapturePolicy(episodic_enabled=True, memory_enabled=False)},
    {"policy": CapturePolicy(episodic_enabled=True, memory_enabled=True, incognito=True)},
])
def test_unsupported_or_unpersisted_turn_does_not_write(tmp_path, overrides):
    assert _capture(tmp_path, **overrides) is None
    assert not (tmp_path / "runtime").exists()


def test_capture_does_not_modify_source_vault(tmp_path):
    vault = tmp_path / "core-of-truth"
    vault.mkdir()
    note = vault / "Sensitive.md"
    note.write_text("Private Markdown remains untouched.", encoding="utf-8")
    row = _capture(tmp_path, protected_roots=(vault,))
    assert row
    assert note.read_text(encoding="utf-8") == "Private Markdown remains untouched."
    assert sorted(vault.iterdir()) == [note]


@pytest.mark.parametrize("case,disabled", [
    ("enabled", False),
    ("env_off", True),
    ("incognito", True),
    ("no_memory", True),
    ("compare_mode", True),
    ("user_memory_off", True),
    ("session_memory_off", True),
    ("assistant_not_saved", True),
    ("internal_request", True),
])
def test_existing_post_response_boundary_never_bypasses_user_controls(
    monkeypatch, tmp_path, case, disabled
):
    from routes import chat_helpers
    from src import constants

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MABEL_EPISODIC_CAPTURE_ENABLED", "1" if case != "env_off" else "0")
    monkeypatch.setattr(chat_helpers, "needs_auto_name", lambda _: False)
    sess = SimpleNamespace(
        owner="alice",
        model="test-model",
        name="Existing Session",
        history=[SimpleNamespace(
            role="assistant",
            content="I located a failed model route and traced the root cause.",
            metadata={"_db_id": "message-42"} if case != "assistant_not_saved" else {},
        )],
        memory_extraction_enabled=case != "session_memory_off",
    )
    uprefs = {
        "memory_enabled": case != "user_memory_off",
        "auto_memory": False,
        "auto_skills": False,
    }
    chat_helpers.run_post_response_tasks(
        sess, None, "session-123",
        "We traced a serious failure in the memory runtime today.",
        sess.history[-1].content, None, uprefs,
        None, None, None,
        incognito=case == "incognito",
        compare_mode=case == "compare_mode",
        no_memory=case == "no_memory",
        allow_episodic_capture=case != "internal_request",
        allow_background_extraction=False,
        owner="alice",
    )
    queue_path = tmp_path / "episodic_candidates"
    if disabled:
        assert not queue_path.exists(), case
    else:
        assert len(CandidateQueue(queue_path, owner="alice").list()) == 1
