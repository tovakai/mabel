"""Phase 00.5: evidence-bound promotion, isolation and crash recovery.

Uses a synthetic *persisted* conversation, never production Mabel data.
"""
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.agent_runtime.event_ledger import Ledger
from src.episodic_memory import (
    CandidateAdmissionError, CandidateQueue, CapturePolicy,
    capture_completed_turn, flush_candidate_events, promote_verified_conversation,
)
from src.episodic_memory import promotion


@pytest.fixture
def scenario(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'messages.sqlite'}")
    user_text = "We traced the tool permission bug in Mabel and found a broken approval boundary."
    assistant_text = "I checked the persisted tool receipt, but this answer is not independent evidence of the cause."
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, owner TEXT, memory_extraction_enabled BOOLEAN)"
        ))
        conn.execute(text(
            "CREATE TABLE chat_messages(id TEXT PRIMARY KEY, session_id TEXT, "
            "role TEXT, content TEXT, timestamp TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO sessions(id,owner,memory_extraction_enabled) "
            "VALUES('session-123','alice',1)"
        ))
        conn.execute(text(
            "INSERT INTO chat_messages VALUES(:id,:sid,'user',:body,:at)"
        ), {"id": "message-41", "sid": "session-123", "body": user_text,
            "at": "2026-11-01T10:00:00"})
        conn.execute(text(
            "INSERT INTO chat_messages VALUES(:id,:sid,'assistant',:body,:at)"
        ), {"id": "message-42", "sid": "session-123", "body": assistant_text,
            "at": "2026-11-01T10:01:00"})
    policy = CapturePolicy(episodic_enabled=True, memory_enabled=True)
    queue_dir = tmp_path / "runtime"
    queue = CandidateQueue(queue_dir, owner="alice")
    candidate = capture_completed_turn(
        storage_dir=queue_dir, owner="alice",
        session_id="session-123", assistant_message_id="message-42",
        user_message=user_text, assistant_message=assistant_text, policy=policy,
    )
    vault = tmp_path / "mabel-vault"
    vault.mkdir()
    protected = tmp_path / "core-of-truth"
    protected.mkdir()
    original = protected / "private.md"
    original.write_bytes(b"untouched")
    options = dict(queue=queue, candidate_id=candidate["id"],
                   vault_root=vault, protected_roots=(protected,),
                   policy=policy, approved=True)
    yield engine, candidate, options, original
    engine.dispose()


def _promote(engine, options):
    with Session(engine) as db:
        return promote_verified_conversation(**options, db=db)


def test_promotion_from_exact_saved_source_is_durable_and_idempotent(scenario, tmp_path):
    engine, candidate, opts, private_note = scenario
    row = _promote(engine, opts)
    assert row["changed"] and row["state"] == "promoted"
    assert row["evidence_status"] == "transcript_verified"
    path = Path(row["episode_path"])
    assert path.exists() and path.is_relative_to(opts["vault_root"])
    assert "Mabel reply (source excerpt)" in path.read_text()
    assert "does **not** verify the truth" in path.read_text()
    assert "source_ref: \"chat:session-123:message:message-42\"" in path.read_text()
    assert sha256(path.read_bytes()).hexdigest() == row["decision_reason"]
    assert private_note.read_bytes() == b"untouched"

    restarted = CandidateQueue(opts["queue"].path.parent, owner="alice")
    repeated = _promote(engine, {**opts, "queue": restarted})
    assert repeated["changed"] is False
    assert repeated["episode_path"] == str(path)
    assert len(list(opts["vault_root"].rglob("*.md"))) == 1
    ledger = Ledger(tmp_path / "phase0" / "events.jsonl")
    assert flush_candidate_events(restarted, ledger).acknowledged == 2
    assert flush_candidate_events(restarted, ledger).acknowledged == 0
    events, errors = ledger.read()
    assert not errors
    assert [e.kind for e in events] == [
        "memory.candidate_created", "memory.candidate_promoted",
    ]
    assert "tool permission bug" not in ledger.path.read_text()
    assert len(restarted.list(state="promoted")) == 1


def test_explicit_approval_and_current_memory_controls_are_required(scenario):
    engine, _, options, _ = scenario
    for overrides in (
        {"approved": False},
        {"policy": CapturePolicy()},
        {"policy": CapturePolicy(episodic_enabled=True, memory_enabled=True, incognito=True)},
    ):
        with pytest.raises(PermissionError):
            _promote(engine, {**options, **overrides})
    assert list(options["vault_root"].rglob("*.md")) == []
    assert options["queue"].get(options["candidate_id"])["state"] == "pending"


def test_source_owner_and_transcript_mismatch_fail_closed(scenario):
    engine, candidate, opts, _ = scenario
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE sessions SET owner='bob' WHERE id='session-123'"
        ))
    with pytest.raises(CandidateAdmissionError, match="another owner"):
        _promote(engine, opts)
    with engine.begin() as conn:
        conn.execute(text("UPDATE sessions SET owner='alice' WHERE id='session-123'"))
        conn.execute(text(
            "UPDATE chat_messages SET content='Someone edited the reply' WHERE id='message-42'"
        ))
    with pytest.raises(CandidateAdmissionError, match="no longer matches"):
        _promote(engine, opts)
    assert opts["queue"].get(candidate["id"])["state"] == "pending"
    assert list(opts["vault_root"].rglob("*.md")) == []


def test_disabled_source_session_blocks_promotion(scenario):
    engine, _, opts, _ = scenario
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE sessions SET memory_extraction_enabled=0 WHERE id='session-123'"
        ))
    with pytest.raises(PermissionError, match="disabled"):
        _promote(engine, opts)


@pytest.mark.parametrize("decision", ["rejected", "quarantined"])
def test_settled_candidate_cannot_be_promoted(scenario, decision):
    engine, _, opts, _ = scenario
    opts["queue"].decide(opts["candidate_id"], outcome=decision, reason="not_supported")
    with pytest.raises(CandidateAdmissionError, match="not promotable"):
        _promote(engine, opts)
    assert list(opts["vault_root"].rglob("*.md")) == []


def test_no_writes_to_source_vault_even_if_selected_as_destination(scenario):
    engine, _, opts, original = scenario
    with pytest.raises(CandidateAdmissionError, match="protected"):
        _promote(engine, {**opts, "vault_root": original.parent})
    assert original.read_bytes() == b"untouched"


def test_replay_after_file_written_but_before_sqlite_finalization(scenario, monkeypatch):
    engine, _, opts, _ = scenario
    real = promotion._write_once

    def simulate_crash(path, content):
        real(path, content)
        raise OSError("synthetic power failure after durable file creation")

    monkeypatch.setattr(promotion, "_write_once", simulate_crash)
    with pytest.raises(OSError, match="power failure"):
        _promote(engine, opts)
    assert opts["queue"].get(opts["candidate_id"])["state"] == "promoting"
    assert len(opts["queue"].pending_events()) == 1
    assert len(list(opts["vault_root"].rglob("*.md"))) == 1

    monkeypatch.setattr(promotion, "_write_once", real)
    result = _promote(engine, opts)
    assert result["changed"]
    assert result["state"] == "promoted"
    assert len(list(opts["vault_root"].rglob("*.md"))) == 1
    assert len(opts["queue"].pending_events()) == 2


def test_failure_before_episode_write_leaves_recoverable_intent(scenario, monkeypatch):
    engine, _, opts, _ = scenario
    real = promotion._write_once

    def fail_without_writing(_path, _content):
        raise OSError("filesystem unavailable")

    monkeypatch.setattr(promotion, "_write_once", fail_without_writing)
    with pytest.raises(OSError, match="filesystem unavailable"):
        _promote(engine, opts)
    assert opts["queue"].get(opts["candidate_id"])["state"] == "promoting"
    assert list(opts["vault_root"].rglob("*.md")) == []
    monkeypatch.setattr(promotion, "_write_once", real)
    assert _promote(engine, opts)["state"] == "promoted"


def test_source_modified_during_incomplete_promotion_blocks_replay(scenario, monkeypatch):
    engine, _, opts, _ = scenario

    def interrupted(_path, _content):
        raise OSError("interrupted")

    monkeypatch.setattr(promotion, "_write_once", interrupted)
    with pytest.raises(OSError):
        _promote(engine, opts)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE chat_messages SET content='Edited out of band' WHERE id='message-42'"
        ))
    with pytest.raises(CandidateAdmissionError, match="no longer matches"):
        _promote(engine, opts)
    assert opts["queue"].get(opts["candidate_id"])["state"] == "promoting"


def test_tampered_promoted_markdown_is_not_silently_accepted(scenario):
    engine, _, opts, _ = scenario
    row = _promote(engine, opts)
    Path(row["episode_path"]).write_text("changed!")
    with pytest.raises(CandidateAdmissionError, match="changed"):
        _promote(engine, opts)


def test_symlinked_episode_directory_is_refused(scenario, tmp_path):
    engine, _, opts, original = scenario
    (opts["vault_root"] / "memory").symlink_to(original.parent, target_is_directory=True)
    with pytest.raises(CandidateAdmissionError, match="symlink"):
        _promote(engine, opts)
    assert original.read_bytes() == b"untouched"


# --- the repo-is-the-vault layout (Documents/mabel) ---------------------------------------

def _git_vault(tmp_path, ignore_memory):
    import subprocess
    vault = tmp_path / "repo-vault"
    vault.mkdir()
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    (vault / ".gitignore").write_text("data/\n" + ("/memory/**\n" if ignore_memory else ""))
    return vault


def _queue_inside(vault, candidate_opts, scenario_queue_dir):
    """Move the runtime queue under the vault's data/ folder, as in the real repo."""
    import shutil
    target = vault / "data" / "episodic_candidates"
    shutil.copytree(scenario_queue_dir, target)
    return CandidateQueue(target, owner="alice")


def test_repo_vault_writes_lowercase_memory_episodes_when_git_ignores_it(scenario, tmp_path):
    engine, candidate, opts, _ = scenario
    vault = _git_vault(tmp_path, ignore_memory=True)
    queue = _queue_inside(vault, opts, opts["queue"].path.parent)
    row = _promote(engine, {**opts, "queue": queue, "vault_root": vault})
    path = Path(row["episode_path"])
    assert path.relative_to(vault).parts[:2] == ("memory", "episodes")
    import subprocess
    assert subprocess.run(["git", "-C", str(vault), "status", "--porcelain"],
                          capture_output=True, text=True).stdout.strip() in ("", "?? .gitignore")


def test_repo_vault_refuses_when_episodes_would_be_committable(scenario, tmp_path):
    engine, candidate, opts, _ = scenario
    vault = _git_vault(tmp_path, ignore_memory=False)
    queue = _queue_inside(vault, opts, opts["queue"].path.parent)
    with pytest.raises(CandidateAdmissionError, match="not Git-ignored"):
        _promote(engine, {**opts, "queue": queue, "vault_root": vault})
    assert not (vault / "memory").exists()


def test_queue_inside_the_synced_memory_folder_is_still_refused(scenario, tmp_path):
    engine, candidate, opts, _ = scenario
    vault = tmp_path / "plain-vault"
    vault.mkdir()
    import shutil
    target = vault / "memory" / "queue"
    shutil.copytree(opts["queue"].path.parent, target)
    with pytest.raises(CandidateAdmissionError, match="synced memory folder"):
        _promote(engine, {**opts, "queue": CandidateQueue(target, owner="alice"), "vault_root": vault})
