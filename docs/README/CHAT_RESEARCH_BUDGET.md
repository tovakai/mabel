# CHAT_RESEARCH_BUDGET

**Path:** `src/agent_runtime/chat_research_budget.py`

## Purpose

Converge ordinary chat web research without a hard four-round ceiling. Deep research keeps its own planner; this budget only stops interactive loops that stop making progress.

## Settings

| Key | Default | Role |
|---|---|---|
| `agent_web_research_max_searches` | 8 | Absolute search executions (hard stop) |
| `agent_web_research_max_fetches` | 12 | Absolute fetch executions (hard stop) |
| `agent_web_research_stall_rounds` | 3 | Rounds without new unique query/URL (hard stop) |
| `agent_web_research_soft_synthesis_rounds` | 10 | Advisory nudge only; productive research continues |
| `agent_web_research_hard_rounds` | 24 | Absolute research-tool round ceiling (hard stop) |

Soft synthesis appends a prefer-to-answer instruction once and emits a `research_budget`/`soft_nudge` diagnostic when enabled. Hard reasons force a tool-free synthesis round and emit `research_budget`/`exhausted`.
