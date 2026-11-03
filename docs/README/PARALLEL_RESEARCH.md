# PARALLEL_RESEARCH

**Path:** `src/agent_runtime/parallel_research.py`

## Purpose

First-class `research_parallel` tool that concurrently (when safe) runs authorized `web_search` and/or `dispatch_dottie` children, then returns one compact evidence package for Mabel to synthesize. It is a coordinator, not a second research agent, vault index, or synthesis LLM.

## Public contract

```json
{
  "mode": "both",
  "web_query": "Public information to retrieve",
  "vault_objective": "Related information to find in the vault",
  "vault_constraints": {
    "required_sources": true,
    "max_hops": 1
  }
}
```

| Mode | Behavior |
|---|---|
| `web` | Public search only (`web_query` required) |
| `vault` | Authorized Dottie/vault research only (`vault_objective` required) |
| `both` | Both branches independently; concurrent when endpoints differ |

Missing branch inputs are rejected; queries are never invented. Direct `web_search`, `web_fetch`, and `dispatch_dottie` remain registered.

## Authorization

- Parent admission uses mode-dependent effects (`BROKERED_NETWORK_READ` / `READ_WORKSPACE`).
- Each enabled child is checked with `RequestAuthority.permits` **before** work starts.
- Children execute only through `execute_tool_block` (same policy, resource binding, accounting).
- A compound call cannot authorize an individually unauthorized child.
- Approval/resumption bubbles `approval_required` without fake success. Completed sibling evidence is sealed for the exact parent operation + request id; resume reuses it and executes only the pending child (exact scope), so siblings are not re-run or duplicated.

## Dottie / endpoint contention

`dispatch_dottie` uses the configured **research** model role.

**Verified placement (Arch host):** endpoint `2505e2f2` is registered as `endpoint_kind=local` at `http://127.0.0.1:8081` for model `dottie-research`. Nothing listens on Arch `:8081`. There is **no active SSH `LocalForward`/autossh/cloudflared tunnel** for that port in the current process table or `~/.ssh/config`. Tailscale host `dottie` (`100.113.197.19`) mounts the CIFS vault share, but `:8081`/`:8082`/`:8083` on that host currently **time out**. Docs describe Gemma/`dottie-research` as a Windows/llama.cpp worker intended for Dottie's GPU (RX 5700 XT validation notes), but the **configured URL is Arch loopback**, not Dottie's Tailscale address. Until a live listener or explicit tunnel exists, research inference does **not** run on either machine; `dispatch_dottie` fails closed into degraded/partial vault status (never a false empty success).

When the research URL equals the default chat URL, the coordinator falls back to **sequential** execution and reports `contention_fallback=sequential`. Metrics set `concurrent=true` only when both child windows actually overlap.

## Result shape

```json
{
  "status": "complete|partial|failed|approval_required",
  "web": {"status": "...", "results": []},
  "vault": {"status": "...", "findings": []},
  "unresolved": [],
  "metrics": {"web_ms": 0, "vault_ms": 0, "total_ms": 0, "concurrent": true}
}
```

Empty vault findings are `empty`, not a backend failure. One successful + one failed branch is `partial`. Output is fair-budget truncated while preserving URLs and vault note IDs/paths/ranges.

## Runtime integration

- Registered via `src/knowledge_engine/tools.py` (schema + handler).
- Discoverable in chat and agent modes when `search_browser` and/or `workspace_read` capabilities apply; vault/Dottie intents escalate chat→agent.
- Chat research budget counts the web branch of `research_parallel` toward search caps.
- Diagnostics: `parallel_research_started`, `parallel_branch_completed`, `parallel_branch_failed`, `parallel_research_completed` (observational; safe when disabled/incognito).
- Recursion into `research_parallel` from its own children is denied.

## Tests / benchmark

- `tests/test_research_parallel.py` — overlap, partial/failure, auth, approval, truncation, discovery, cancel, budgets, empty vault, diagnostics, recursion, sequential fallback.
- `tests/benchmark_research_parallel.py` — synthetic fake-backend wall-clock comparison only.
