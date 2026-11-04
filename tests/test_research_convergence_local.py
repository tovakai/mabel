"""Local retrieval participates in research convergence (productive vs stalled)."""
from src.agent_runtime.chat_research_budget import ChatResearchBudget


def _rec(tool, content, output, exit_code=0):
    return {"tool_name": tool, "content": content, "result": {"output": output, "exit_code": exit_code}}


def test_productive_vault_reads_never_stall_or_hit_hard_rounds():
    budget = ChatResearchBudget(stall_rounds=3, soft_synthesis_rounds=10, hard_synthesis_rounds=20)
    for i in range(30):
        decision = budget.observe_round([_rec("read_file", f'{{"path": "/v/n{i}.md"}}', f"note {i}")])
        assert decision.hard_reason is None


def test_reading_a_file_a_search_only_listed_is_progress():
    budget = ChatResearchBudget(stall_rounds=2)
    budget.observe_round([_rec("grep", "{}", "/v/a.md:3: Lessons in Love")])
    budget.observe_round([_rec("glob", "{}", "/v/a.md")])          # duplicate path
    decision = budget.observe_round([_rec("read_file", '{"path": "/v/a.md"}', "body")])  # verification
    assert decision.hard_reason is None and budget.stall_streak == 0


def test_duplicate_empty_and_failed_local_rounds_stall():
    budget = ChatResearchBudget(stall_rounds=3)
    budget.observe_round([_rec("grep", "{}", "/v/a.md:3: hit")])
    budget.observe_round([_rec("glob", "{}", "/v/a.md")])                       # duplicate
    budget.observe_round([_rec("glob", "{}", "No files matching '*x*' under /v")])  # empty
    decision = budget.observe_round([_rec("vault_retrieve_batch", "{}", "", exit_code=1)])  # failure
    assert decision.hard_reason == "stall"
    assert "State remaining gaps" in budget.synthesis_instruction("stall")


def test_soft_nudge_counts_local_rounds_once():
    budget = ChatResearchBudget(soft_synthesis_rounds=4, stall_rounds=10)
    nudges = [budget.observe_round([_rec("read_file", f'{{"path": "/v/{i}.md"}}', "x")]).soft_nudge
              for i in range(6)]
    assert nudges.count("soft_synthesis") == 1 and nudges[3] == "soft_synthesis"


def test_count_fields_in_status_payloads_do_not_crash_the_budget():
    # vault_index_status returns {"notes": 1267, ...}; iterating the count
    # crashed the whole agent run ("'int' object is not iterable").
    import json as _json
    from src.agent_runtime.chat_research_budget import _local_evidence_keys
    record = {"tool_name": "vault_index_status", "content": "{}",
              "result": {"exit_code": 0, "output": _json.dumps({"notes": 1267, "matches": 3, "results": None})}}
    assert isinstance(_local_evidence_keys(record), set)


def test_mixed_web_and_local_rounds_hit_the_hard_cap():
    # parallel_compare ran 21 rounds: research_parallel, then media lookups and
    # globs/ls. Once web is involved, local rounds count toward the hard cap.
    budget = ChatResearchBudget(stall_rounds=50, soft_synthesis_rounds=5, hard_synthesis_rounds=12)
    budget.observe_round([_rec("research_parallel", '{"mode": "both", "query": "Berserk"}', '{"ok": 1}')])
    reasons = [budget.observe_round([_rec("read_file", f'{{"path": "/v/n{i}.md"}}', f"note {i}")]).hard_reason
               for i in range(15)]
    assert "hard_rounds" in reasons and reasons.index("hard_rounds") == 10  # 12th retrieval round overall


def test_media_lookups_count_as_searches_and_repeat_queries_stall():
    budget = ChatResearchBudget(stall_rounds=2, max_searches=50)
    args = '{"source": "anilist", "query": "Monster"}'
    budget.observe_round([_rec("media_lookup", args, '{"results": []}')])
    assert budget.search_count == 1 and budget.stall_streak == 0
    budget.observe_round([_rec("media_lookup", args, '{"results": []}')])
    decision = budget.observe_round([_rec("media_lookup", args, '{"results": []}')])
    assert decision.hard_reason == "stall"
