# TURN_CONTRACT

**Path:** `src/turn_contract.py`

## Purpose

Turn-level capability selection: which tools are **offered** (model-facing
schemas) vs merely **executable** (inventory). Selection is not authorization;
`RequestAuthority` and policy still gate execution.

## Conversational grounding (2026-11-03)

- `CONTRACT_CORE_TOOLS` includes `research_parallel` with core web tools so
  empty-capability agent turns expose combined web+vault retrieval to Gyro-S.
- `conversational_retrieval_intent(message, workspace=...)` returns
  `casual` | `factual_public` | `vault` | `parallel` | `none` via lexical
  patterns only (no intent-classifier LLM).
- `media_progress_boundary(message)` returns `{unit, reached}` when the user
  expresses finishing/reaching a numbered chapter/episode/act/part (spoiler
  restraint for later units).
- Bound-workspace note-content questions (`what do my notes say…`) resolve to
  `workspace_read`. Notes+online compares resolve to
  `workspace_read` + `search_browser`.

## Related

- Compact Gyro-S surface: `src/agent_loop.py`
- Chat→agent vault/compare intents: `src/action_intents.py`
- Coordinator: `docs/README/PARALLEL_RESEARCH.md`
- Tests: `docs/README/TEST_CONVERSATIONAL_RESEARCH_ROUTING.md`
