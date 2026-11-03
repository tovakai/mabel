"""Avoid false mutation notices for historical prose without excusing false execution claims."""
from src.agent_evidence import EvidenceLedger
from src.agent_runtime.completion import _current_run_claims, completion_answer


def test_vault_summary_conjunction_does_not_claim_mabel_mutated_anything():
    prose = (
        "The team built the original roadmap and updated its phases over time. "
        "Mabel's documentation now links those phases to Curiosity Drive."
    )
    ledger = EvidenceLedger()
    answer, reason = completion_answer(prose, ledger, ledger.evaluate())
    assert answer == prose
    assert not reason
    assert "Unsupported execution claims" not in answer


def test_first_person_execution_is_still_filtered_without_receipts():
    prose = "I created report.md and updated config.py."
    ledger = EvidenceLedger()
    answer, reason = completion_answer(prose, ledger, ledger.evaluate())
    assert reason
    assert "I created report.md" not in answer
    assert "Unsupported execution claims" in answer
    claims = _current_run_claims(prose, execution_required=False)
    assert len([kind for kind, _ in claims if kind == "mutation"]) == 2


def test_passive_current_mutation_remains_guarded():
    ledger = EvidenceLedger()
    answer, reason = completion_answer("config.py was updated successfully.", ledger, ledger.evaluate())
    assert reason
    assert "config.py was updated successfully." not in answer


def test_historical_conjunction_is_not_a_current_run_claim():
    claims = _current_run_claims(
        "Your earlier agent researched and created a spec tree.", execution_required=False,
    )
    assert claims == []


def test_explicit_first_person_execution_followed_by_conjunction_still_is_claim():
    claims = _current_run_claims(
        "We ran tests and updated the file.", execution_required=False,
    )
    assert len(claims) == 2
