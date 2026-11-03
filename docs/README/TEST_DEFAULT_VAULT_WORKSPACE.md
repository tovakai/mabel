# TEST_DEFAULT_VAULT_WORKSPACE

**Path:** `tests/test_default_vault_workspace.py`

## Purpose

Pin that the configured vault target (Core of Truth) binds as the default
active workspace unless the user opts out, and that interactive Agent
authority seals `web_search` / `web_fetch` / workspace readers once that
workspace is present.

## Coverage

- `_resolve_default_vault_workspace` binds, respects opt-out, keeps an explicit
  workspace
- Interactive review grants permit brokered web reads and `read_file`, not bash

## Related

- `routes/chat_routes.py`, `routes/workspace_routes.py`
- `static/js/workspace.js` (opt-out sentinel)
- `src/agent_runtime/vault_target.py`
