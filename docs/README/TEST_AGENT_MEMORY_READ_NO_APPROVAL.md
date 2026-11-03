# TEST_AGENT_MEMORY_READ_NO_APPROVAL

**Path:** `tests/test_agent_memory_read_no_approval.py`

## Purpose

Pin that interactive Agent-mode pure reads (memory list, notes search, email /
vault / brokered web reads) stay free after tool discovery or web-search taint,
without a UI approval card, while writes and shell remain gated.

## Coverage

- `manage_memory` list after `discover_tools`
- `manage_notes` search/find/list, `read_email`, `vault_search`, `web_fetch`
  after `web_search` taint
- Ownerless runs: gate allows pure reads; authority still has no grant; writes
  stay blocked

## Related

- `src/tool_capabilities.py` (`READ_ONLY_EFFECTS`, `action_is_read_only`)
- `src/agent_runtime/authority.py` (`_with_owner_read_grants`)
