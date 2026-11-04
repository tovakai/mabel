"""The simple Obsidian lookup budget cannot change authority or deep research."""
from types import SimpleNamespace

import pytest

from src.agent_runtime.vault_lookup_budget import (
    VaultLookupBudget, simple_vault_lookup, READ_TOOLS,
)


@pytest.mark.parametrize("user_text", [
    "Find my documentation about Mabel in my vault.",
    "Locate my Obsidian notes on Curiosity Drive.",
    "Where is the Markdown file for the roadmap?",
])
def test_document_locator_turn_uses_budget(user_text):
    assert simple_vault_lookup(user_text)


@pytest.mark.parametrize("user_text", [
    "Audit all my vault's properties.",
    "Find and compare every note in my Obsidian vault.",
    "Research the Mabel architecture deeply across my vault.",
    "Update the documentation about Curiosity Drive.",
    "Explain my documentation architecture.",
    "Find documentation for a Python library on the web.",
])
def test_deep_write_or_unrelated_requests_are_not_capped(user_text):
    assert not simple_vault_lookup(user_text)


def _record(tool, *, success=True):
    return {"tool_name": tool, "result": {
        "exit_code": 0 if success else 1,
        **({} if success else {"error": "denied", "failure_kind": "resource_identity_denied"}),
        "output": '{"hits":[{"path":"notes/mabel.md"}]}' if success else "",
    }}


def test_successful_vault_reads_converge_without_consuming_an_extra_tool_call():
    budget = VaultLookupBudget()
    for _ in range(5):
        assert budget.observe([_record("vault_knowledge_search")]) is None
    assert budget.observe([_record("vault_read_range")]) == "bounded_vault_evidence"
    assert budget.attempts == 6
    assert budget.successful_reads == 6
    assert budget.observe([_record("vault_knowledge_search")]) is None
    assert budget.attempts == 6


def test_repeated_failed_reads_stop_instead_of_switching_between_tools_forever():
    budget = VaultLookupBudget()
    assert budget.observe([_record("vault_knowledge_search", success=False)]) is None
    assert budget.observe([_record("vault_index_status", success=False)]) == "repeated_vault_failures"
    assert budget.successful_reads == 0


def test_distinct_reads_are_still_bounded_and_unrelated_tools_do_not_count():
    budget = VaultLookupBudget()
    for i in range(8):
        tool = "vault_knowledge_search" if i % 2 else "vault_index_status"
        assert budget.observe([_record(tool)]) is None
    assert budget.observe([_record("vault_index_status")]) == "vault_lookup_budget"
    assert budget.attempts == 9


def test_one_failure_between_successes_does_not_trigger_a_failure_stop():
    budget = VaultLookupBudget()
    assert budget.observe([_record("vault_knowledge_search", success=False)]) is None
    assert budget.observe([_record("vault_read_range")]) is None
    assert budget.observe([_record("vault_traverse_graph", success=False)]) is None
    assert budget.consecutive_failures == 1
    assert budget.observe([_record("discover_tools"), _record("vault_knowledge_search")]) is None
    assert budget.consecutive_failures == 0
    assert budget.attempts == 4
    assert "discover_tools" not in READ_TOOLS


def test_vault_convergence_hook_reuses_existing_tool_free_finalization():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("src", "agent_loop.py").read_text()
    assert "_vault_lookup_budget.observe(tool_result_records)" in source
    assert "if _vault_lookup_reason:" in source
    assert "vault_lookup_convergence" in source
    assert "Never invent files" in source


@pytest.mark.parametrize("user_text", [
    "What do my notes say about Lessons in Love?",
    "what does my vault say about Berserk",
    "What's in my notes about Takashi Miike?",
    "What have I written about the History Study Plan in my notes?",
    "Do I have any notes on Dome Keeper?",
])
def test_knowledge_questions_use_the_budget(user_text):
    assert simple_vault_lookup(user_text)


@pytest.mark.parametrize("user_text", [
    "Compare my notes on Lessons in Love with public information about the game.",
    "What do my notes say about X? Then update them with the release date.",
    "Summarize everything in my notes about Lost.",
])
def test_knowledge_questions_that_compare_edit_or_summarize_everything_are_not_capped(user_text):
    assert not simple_vault_lookup(user_text)


def test_workspace_reads_count_toward_the_budget():
    budget = VaultLookupBudget()
    sequence = ("vault_knowledge_search", "grep", "glob", "read_file", "read_file", "read_file", "read_file")
    assert all(budget.observe([_record(tool)]) is None for tool in sequence)
    assert budget.successful_reads == 5 and budget.attempts == 7
    # The sixth source read converges, as in the 14-call live run around its fifth round.
    assert budget.observe([_record("read_file")]) == "bounded_vault_evidence"
