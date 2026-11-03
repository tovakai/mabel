# TEST_RESEARCH_PARALLEL_TURN_AUTHORITY

**Path:** `tests/test_research_parallel_turn_authority.py`

## Purpose

Focused integration coverage for the live `research_parallel(mode=both)` defect
where the parent was admitted after `discover_tools` selected only
`research_parallel`, while nested `web_search` / `vault_knowledge_search` failed
turn-contract (and, when disabled, request authority) even though later direct
calls succeeded after a second discover.

## What it exercises

Runs through real `create_request_authority`, conversational turn catalog,
`discover_tools` (parent only), and `execute_tool_block` admission gates.
Child **bodies** are stubbed only after those gates (`_execute_tool_block_impl`),
so authority, disabled-tool restrict, turn-contract permits, and resource
binding still run.

## Cases

| Test | Expectation |
|---|---|
| Both children individually authorized | `complete`; both children execute |
| Web allowed, vault denied | Web runs; vault `authorization_denied` |
| Vault allowed, web denied | Vault runs; web `authorization_denied` |
| Neither allowed | Both denied; no child execution |
| Explicit web toggle deny | Parent may still be granted via workspace; web branch denied |
| Child missing from turn catalog | Parent advertised; missing child `turn_contract_denied` |
| Approval resume | Completed sibling sealed; resume does not re-run it |

## Related

- Coordinator: `src/agent_runtime/parallel_research.py`
- Docs: `docs/README/PARALLEL_RESEARCH.md`
