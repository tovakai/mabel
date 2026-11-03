"""Visited-URL ledger and chat research convergence budgets."""
from src.agent_runtime.chat_research_budget import ChatResearchBudget
from src.agent_runtime.web_visit_ledger import (
    FetchKind,
    WebVisitLedger,
    classify_fetch_kind,
    fetch_request_from_tool_content,
    normalize_fetch_url,
    request_urls,
)


def test_normalize_fetch_url_drops_fragment_keeps_query():
    assert normalize_fetch_url("HTTPS://Example.com/Path/?q=1#frag") == (
        "https://example.com/Path?q=1"
    )


def test_classify_distinguishes_partial_focused_and_complete():
    assert classify_fetch_kind(truncated=True) is FetchKind.PARTIAL
    assert classify_fetch_kind(extraction_goal="table scores") is FetchKind.FOCUSED
    assert classify_fetch_kind(full=True) is FetchKind.COMPLETE
    assert classify_fetch_kind() is FetchKind.COMPLETE


def test_ledger_allows_upgrades_and_new_goals_but_suppresses_repeats():
    ledger = WebVisitLedger()
    ledger.record("https://docs.example/a", FetchKind.PARTIAL)
    assert ledger.should_suppress("https://docs.example/a", FetchKind.PARTIAL)
    assert not ledger.should_suppress("https://docs.example/a", FetchKind.COMPLETE)
    ledger.record("https://docs.example/a", FetchKind.COMPLETE)
    assert ledger.should_suppress("https://docs.example/a", FetchKind.COMPLETE)
    assert not ledger.should_suppress(
        "https://docs.example/a",
        FetchKind.FOCUSED,
        extraction_goal="install steps",
    )
    ledger.record(
        "https://docs.example/a",
        FetchKind.FOCUSED,
        extraction_goal="install steps",
    )
    assert ledger.should_suppress(
        "https://docs.example/a",
        FetchKind.FOCUSED,
        extraction_goal="install steps",
    )
    # Compaction cannot erase visit memory held outside the prompt.
    assert ledger.lookup("https://docs.example/a").kind is FetchKind.COMPLETE


def test_mixed_batch_filters_visited_urls_individually():
    ledger = WebVisitLedger()
    ledger.record("https://example.com/old", FetchKind.COMPLETE)
    request = fetch_request_from_tool_content(
        '{"urls":["https://example.com/old","https://example.com/new"],'
        '"extraction_goal":""}'
    )
    urls = request_urls(request)
    kind = classify_fetch_kind(extraction_goal="", full=False)
    allowed = [
        url for url in urls
        if not ledger.should_suppress(url, kind, extraction_goal="")
    ]
    suppressed = [url for url in urls if url not in allowed]
    assert allowed == ["https://example.com/new"]
    assert suppressed == ["https://example.com/old"]
    # Focused extraction on a completed page remains an intentional upgrade path.
    assert not ledger.should_suppress(
        "https://example.com/old",
        FetchKind.FOCUSED,
        extraction_goal="pricing table",
    )
    # full=true after partial remains available.
    ledger.record("https://example.com/partial", FetchKind.PARTIAL)
    assert not ledger.should_suppress(
        "https://example.com/partial",
        FetchKind.COMPLETE,
    )


def test_research_budget_forces_synthesis_on_stall_not_four_round_cap():
    budget = ChatResearchBudget(
        max_searches=8,
        max_fetches=12,
        stall_rounds=2,
        soft_synthesis_rounds=10,
        hard_synthesis_rounds=24,
    )
    first = budget.observe_round([
        {
            "tool_name": "web_search",
            "content": '{"query":"alpha beta"}',
            "result": {"exit_code": 0, "output": "hits"},
        }
    ])
    assert first.hard_reason is None
    assert first.soft_nudge is None
    assert budget.stall_streak == 0
    assert "alpha beta" in budget.unique_queries
    # Identical follow-ups accumulate stall; two stalled rounds force synthesis.
    second = budget.observe_round([
        {
            "tool_name": "web_search",
            "content": '{"query":"alpha beta"}',
            "result": {"exit_code": 0, "output": "hits"},
        }
    ])
    assert second.hard_reason is None
    assert budget.stall_streak == 1
    third = budget.observe_round([
        {
            "tool_name": "web_search",
            "content": '{"query":"alpha beta"}',
            "result": {"exit_code": 0, "output": "hits"},
        }
    ])
    assert third.hard_reason == "stall"
    assert "Stop calling web tools" in budget.synthesis_instruction(third.hard_reason)


def test_soft_synthesis_is_advisory_while_productive_research_continues():
    budget = ChatResearchBudget(
        max_searches=20,
        max_fetches=20,
        stall_rounds=5,
        soft_synthesis_rounds=4,
        hard_synthesis_rounds=24,
    )
    decisions = []
    for index in range(6):
        decisions.append(
            budget.observe_round([
                {
                    "tool_name": "web_fetch",
                    "content": json_url(index),
                    "result": {
                        "exit_code": 0,
                        "output": f"Source: https://example.com/{index}\nbody",
                        "url": f"https://example.com/{index}",
                    },
                }
            ])
        )
    soft_hits = [d for d in decisions if d.soft_nudge == "soft_synthesis"]
    assert len(soft_hits) == 1
    assert soft_hits[0].hard_reason is None
    # Productive rounds past the soft threshold must not force synthesis.
    assert all(d.hard_reason is None for d in decisions)
    assert budget.research_rounds == 6
    assert budget.exhausted_reason is None
    assert "Prefer synthesizing" in budget.synthesis_instruction("soft_synthesis")
    assert "Stop calling web tools" not in budget.synthesis_instruction("soft_synthesis")


def test_hard_rounds_eventually_converge_after_soft_nudge():
    budget = ChatResearchBudget(
        max_searches=50,
        max_fetches=50,
        stall_rounds=20,
        soft_synthesis_rounds=3,
        hard_synthesis_rounds=5,
    )
    last = None
    for index in range(5):
        last = budget.observe_round([
            {
                "tool_name": "web_fetch",
                "content": json_url(index),
                "result": {
                    "exit_code": 0,
                    "output": f"Source: https://example.com/{index}\nbody",
                    "url": f"https://example.com/{index}",
                },
            }
        ])
    assert last is not None
    assert last.hard_reason == "hard_rounds"
    assert budget.exhausted_reason == "hard_rounds"


def json_url(index: int) -> str:
    return '{"url":"https://example.com/%d"}' % index
