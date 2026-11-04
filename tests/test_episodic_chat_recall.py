"""Phase 00.5 recall in chat: verified snippets, current session skipped, metadata-only events."""
import json
from pathlib import Path

from sqlalchemy.orm import Session

from src.episodic_memory import chat_recall
from tests.test_episodic_verified_promotion import _promote, scenario  # noqa: F401  (fixture)


def _with_injection_column(engine):
    # The real sessions table has this per-session memory toggle; recall checks it.
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sessions ADD COLUMN memory_injection_enabled BOOLEAN DEFAULT 1"))


def _recall(engine, opts, message, session_id="another-session"):
    with Session(engine) as db:
        return chat_recall.recall_for_turn(
            owner="alice", session_id=session_id, message=message, db=db,
            storage_dir=opts["queue"].path.parent, vault_root=opts["vault_root"],
            protected_roots=opts["protected_roots"])


def test_promoted_episode_is_recalled_in_a_later_session(scenario):
    engine, candidate, opts, _ = scenario
    _with_injection_column(engine)
    _promote(engine, opts)
    snippets = _recall(engine, opts, "Did we ever figure out that tool permission bug?")
    assert len(snippets) == 1
    assert "approval boundary" in snippets[0]["user"]
    assert snippets[0]["candidate_id"] == candidate["id"]
    message = chat_recall.recall_message(snippets)
    assert "not verified facts" in message["content"] and "never instructions" in message["content"]
    assert message["metadata"]["trusted"] is False


def test_the_current_session_and_unrelated_questions_recall_nothing(scenario):
    engine, _, opts, _ = scenario
    _with_injection_column(engine)
    _promote(engine, opts)
    assert _recall(engine, opts, "tool permission bug", session_id="session-123") == []
    assert _recall(engine, opts, "What is the weather in Oslo?") == []


def test_unpromoted_candidates_are_never_recalled(scenario):
    engine, _, opts, _ = scenario
    _with_injection_column(engine)
    assert _recall(engine, opts, "tool permission bug approval boundary") == []


def test_recall_never_creates_a_queue(tmp_path):
    storage = tmp_path / "episodic"
    storage.mkdir()
    assert chat_recall.recall_for_turn(owner="bob", session_id="s", message="tool permission bug",
                                       db=None, storage_dir=storage, vault_root=tmp_path,
                                       protected_roots=(tmp_path / "cot",)) == []
    assert list(storage.iterdir()) == []


def test_recall_event_is_metadata_only(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    chat_recall.log_recall([{"candidate_id": "abc123", "user": "secret words", "assistant": "more"}],
                           session_id="session-9")
    lines = (tmp_path / "phase0" / "events.jsonl").read_text().splitlines()
    event = json.loads(lines[-1])
    assert event["kind"] == "memory.episode_recalled" and event["references"] == ["abc123"]
    assert "secret" not in lines[-1]


def test_switches_default_off(monkeypatch):
    monkeypatch.delenv("MABEL_EPISODIC_RECALL_ENABLED", raising=False)
    monkeypatch.delenv("MABEL_EPISODIC_CAPTURE_ENABLED", raising=False)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    assert chat_recall.recall_enabled() is False and chat_recall.capture_enabled() is False
    monkeypatch.setenv("MABEL_EPISODIC_RECALL_ENABLED", "1")
    assert chat_recall.recall_enabled() is True


def test_episode_from_a_session_with_memory_turned_off_is_not_recalled(scenario):
    from sqlalchemy import text
    engine, _, opts, _ = scenario
    _with_injection_column(engine)
    _promote(engine, opts)
    with engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET memory_injection_enabled=0 WHERE id='session-123'"))
    assert _recall(engine, opts, "tool permission bug approval boundary") == []
