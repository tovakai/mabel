# TEST_VAULT_NOTE_REWRITE_ROUTING

**Path:** `tests/test_vault_note_rewrite_routing.py`

## Purpose

Pin that “rewrite that vault/genre note” stays on a lean file-editor surface
(`read_file` / `write_file` / `edit_file` + `ls`/`glob`/`grep`) — not
Documents-panel tools, `bash`, `vault_batch_edit`, or web/parallel research —
and that WebUI agent turns always enable one degenerate-stream retry after
tool results.

## Failure class

1. Rewrite Visual Novel note → many reads → bash `wc` approval → model
   repetition loop → HTTP 502 with `fallback_eligible: false` and no retry
   because `retry_degenerate_stream_once` was terminal-agent only.
2. Follow-up still offered `vault_batch_edit` (703-game preview) while
   `write_file`/`read_file` were selected but not offered; textual `read_file`
   dropped as unoffered; clarify turn timed out (504). Root causes: warm tools
   reopened bulk vault tools after an exact rewrite seal, and
   `workspace_read`-only families intersected away writers.

## Checks

- Detector matches rewrite/edit + note/page/genre/vault language
- Intent domains include `files`, exclude documents/notes_calendar_tasks
- Turn contract offers writers even with prior-turn warm `vault_batch_edit` /
  `bash` / `web_search`, and even when capabilities are only `workspace_read`
- Prompt steers off `vault_batch_edit` / bash inventory
- Degenerate-stream retry is unconditional (`True`)

## Related

- `src/turn_contract.py` (`VAULT_NOTE_REWRITE_TOOLS`, `looks_like_vault_note_rewrite`,
  exact-seal warm skip, writer survival over read-only families)
- `src/agent_loop.py` (rewrite core floor, contract∩relevant backfill,
  late rewrite clamp, `retry_degenerate_stream_once=True`)
- `src/llm_core.py` (degenerate-stream recovery)
