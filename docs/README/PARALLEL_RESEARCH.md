# PARALLEL_RESEARCH

**Path:** `src/agent_runtime/parallel_research.py`

## Purpose

First-class `research_parallel` tool that concurrently (when safe) runs authorized
`web_search` plus a vault branch, then returns one compact evidence package for
Mabel to synthesize. It is a coordinator, not a second research agent, vault
index, or synthesis LLM.

## Public contract

```json
{
  "mode": "both",
  "vault_strategy": "fast",
  "web_query": "Public information to retrieve",
  "vault_objective": "Related information to find in the vault",
  "vault_constraints": {
    "path_prefix": null,
    "required_sources": true,
    "max_hops": 1
  }
}
```

| Mode | Behavior |
|---|---|
| `web` | Public search only (`web_query` required) |
| `vault` | Authorized vault retrieval only (`vault_objective` required) |
| `both` | Both branches independently; concurrent when safe |

| `vault_strategy` | Behavior |
|---|---|
| `fast` (**default**) | Indexed `vault_knowledge_search` with `mode=lexical`, no Dottie LLM, no embeddings/reranker requirement. Returns `kind=index_candidates` matches (not verified claims). |
| `deep` | Existing `dispatch_dottie` bounded research with evidence verification and the deep timeout. Returns `kind=verified_findings`. |

Missing branch inputs are rejected; queries are never invented. Direct
`web_search`, `web_fetch`, `vault_knowledge_search`, and `dispatch_dottie`
remain registered.

## Authorization

- Parent admission uses mode-dependent effects (`BROKERED_NETWORK_READ` / `READ_WORKSPACE`).
- Each enabled child is checked with `RequestAuthority.permits` (after the same
  policy/disabled-tool restrict as `execute_tool_block`) **before** work starts.
- Fast checks `vault_knowledge_search`; deep checks `dispatch_dottie`. Neither
  inherits the other tool’s permission.
- Children execute only through `execute_tool_block` (same policy, resource binding, accounting).
- A compound call cannot authorize an individually unauthorized child.
- Compact/conversational turns often discover only `research_parallel`. The
  coordinator temporarily promotes already-inventoried, authority-permitted
  children into the request-local catalog selection for nested dispatch only.
  That derived turn scope never invents tools, never mints grants, and never
  bypasses approval. Children absent from the turn catalog stay denied.
- Approval/resumption bubbles `approval_required` without fake success. Completed sibling evidence is sealed for the exact parent operation + request id; resume reuses it and executes only the pending child (exact scope), so siblings are not re-run or duplicated.

## Dottie / endpoint contention

`vault_strategy=deep` uses the configured **research** model role via
`dispatch_dottie`. When that research URL equals the default chat URL, deep
`mode=both` falls back to **sequential** execution and reports
`contention_fallback=sequential`.

`vault_strategy=fast` never starts the research LLM, so shared research/chat
endpoints do **not** force sequential execution. Metrics set `concurrent=true`
only when both child windows actually overlap.

## Result shape

```json
{
  "status": "complete|partial|failed|approval_required",
  "vault_strategy": "fast",
  "web": {"status": "...", "results": []},
  "vault": {
    "status": "...",
    "strategy": "fast",
    "kind": "index_candidates",
    "matches": [{"note_id": "...", "rel_path": "...", "excerpt": "..."}],
    "findings": []
  },
  "unresolved": [],
  "metrics": {
    "web_ms": 0,
    "vault_ms": 0,
    "vault_index_ms": 0,
    "vault_match_count": 0,
    "total_ms": 0,
    "concurrent": true
  }
}
```

Fast matches are candidate index hits. Deep findings are source-validated claims.
Empty vault results are `empty`, not a backend failure. One successful + one
failed branch is `partial`. Output is fair-budget truncated while preserving
URLs and vault note IDs/paths/ranges. Diagnostics report vault timing and match
counts without logging vault text.

## Runtime integration

- Registered via `src/knowledge_engine/tools.py` (schema + handler).
- Discoverable in chat and agent modes when `search_browser` and/or `workspace_read` capabilities apply; vault/Dottie intents escalate chat→agent.
- Chat research budget counts the web branch of `research_parallel` toward search caps.
- Diagnostics: `parallel_research_started`, `parallel_branch_completed`, `parallel_branch_failed`, `parallel_research_completed` (observational; safe when disabled/incognito).
- Recursion into `research_parallel` from its own children is denied.

## Tests / benchmark

- `tests/test_research_parallel.py` — fast/deep routing, overlap, partial/failure, auth, approval, truncation, discovery, cancel, budgets, empty vault, diagnostics, recursion, sequential deep fallback, concurrent fast-with-shared-endpoint.
- `tests/test_research_parallel_turn_authority.py` — real turn-contract/authority path: both-mode with individually authorized children, web-only, vault-only, neither, explicit web deny, child missing from catalog, approval resume without sibling re-run.
- `tests/benchmark_research_parallel.py` — synthetic fake-backend wall-clock comparison only.
- `tests/live_research_parallel_fast_benchmark.py` — bounded live chat API fast-path latency check.
