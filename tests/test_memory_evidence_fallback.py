"""A failed model synthesis must not erase previously approved memory evidence."""
import pytest

from src.agent_runtime.memory_evidence_fallback import memory_recap_evidence_fallback


MEMORY_LIST = (
    "Memory: 2 saved entries (fact 1, preference 1).\n"
    "- [fact abc123](#memory-abc123) — Keeps an Obsidian knowledge vault.\n"
    "- [preference def456](#memory-def456) — Prefers direct technical answers."
)


@pytest.mark.parametrize("prompt", [
    "Tell me what you know about me.",
    "What do you remember about me?",
    "Show my saved memories.",
    "Summarize my memories.",
])
def test_verified_personal_recap_uses_bounded_memory_evidence(prompt):
    answer = memory_recap_evidence_fallback(prompt, MEMORY_LIST, verified=True)
    assert "saved memories" in answer
    assert "Obsidian knowledge vault" in answer
    assert "Prefers direct technical answers" in answer
    assert "not a verified biography" in answer
    assert "couldn't pull a clean answer" not in answer


def test_unapproved_or_failed_read_never_exposes_memory_evidence():
    assert memory_recap_evidence_fallback(
        "Tell me what you know about me.", MEMORY_LIST, verified=False,
    ) == ""


@pytest.mark.parametrize("prompt", [
    "What is the latest Python release?",
    "Please delete my old files.",
    "What do you know about Stockholm?",
])
def test_unrelated_turn_does_not_replay_private_memory(prompt):
    assert memory_recap_evidence_fallback(prompt, MEMORY_LIST, verified=True) == ""


@pytest.mark.parametrize("summary", [
    "",
    "Couldn't fetch memory.",
    "Error: permission denied",
    "Sample response written by an assistant",
])
def test_invalid_or_failed_payload_does_not_become_memory_recap(summary):
    assert memory_recap_evidence_fallback(
        "What do you know about me?", summary, verified=True,
    ) == ""


def test_explicitly_empty_store_stays_empty():
    assert memory_recap_evidence_fallback(
        "What do you know about me?",
        "No saved memories found.",
        verified=True,
    ) == "No saved memories found."


def test_recovery_bounds_long_summary_without_losing_truncation_notice():
    body = "Memory: 900 saved entries.\n" + "\n".join(
        f"- [fact {i}](#memory-{i}) — " + "x" * 100 for i in range(150)
    )
    answer = memory_recap_evidence_fallback(
        "Tell me what you know about me.", body, verified=True,
    )
    assert len(answer) < 8500
    assert "listing truncated" in answer
