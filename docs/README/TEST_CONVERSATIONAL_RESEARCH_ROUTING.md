# TEST_CONVERSATIONAL_RESEARCH_ROUTING

**Path:** `tests/test_conversational_research_routing.py`

## Purpose

Focused regression for conversational grounding and `research_parallel`
visibility. Reproduces the empty-capability / compact-web failure where
`research_parallel` was executable but never offered to Gyro-S, so casual
named-work turns either fabricated familiarity or fell back to `web_search`
alone.

## What it exercises

- `conversational_retrieval_intent` (casual / factual_public / vault / parallel)
- Real `resolve_turn_contract` model-facing `schema_json` / `offered` sets
- Compact web route preservation of `research_parallel`
- Conversational bootstrap + bounded `discover_tools` recovery
- Tool description / API rule policy strings (not prompt-only routing)

Titles in fixtures are generic; routing must not depend on a specific franchise.

## Cases

| Case | Expectation |
|---|---|
| A casual reaction | Intent `casual`; core offers `research_parallel` + web without requiring use |
| B factual chapter question | Intent `factual_public`; web + parallel schemas offered |
| C compare notes + online | Intent `parallel`; `research_parallel` + web + vault tools offered |
| D vault notes question | Intent `vault`; workspace_read only; no forced `web_search` |
| E public “what is” | Intent `factual_public`; web lookup surface offered |
| F web disabled / compound missing | No unauthorized web or compound schemas |
| G registered but unselected | `discover_tools` selects `research_parallel` into model-facing schemas |
| Casual named media | Intent stays `casual`; policy forbids forcing parallel on every mention |
| Spoiler boundary | `media_progress_boundary` reads finished chapter N |
| Empty web notice | Provider-empty snippets are not substantive sources |

## Related

- Routing: `src/turn_contract.py` (`CONTRACT_CORE_TOOLS`, `conversational_retrieval_intent`)
- Compact Gyro-S surface: `src/agent_loop.py` (`_COMPACT_AGENT_CORE_TOOLS`, `_compact_native_route_tools`)
- Coordinator: `docs/README/PARALLEL_RESEARCH.md`
