# WORKSPACE_ROUTES

**Path:** `routes/workspace_routes.py`

## Purpose

Admin-only APIs to browse host folders, vet a workspace path, and return the
default active workspace for WebUI.

## Endpoints

- `GET /api/workspace/browse` — list child directories
- `GET /api/workspace/vet` — validate a candidate bind path
- `GET /api/workspace/default` — default workspace path

## Default resolution (2026-11-03)

1. `ODYSSEUS_WORKSPACE_DEFAULT` when set and vettable
2. Else configured vault target (`MABEL_VAULT_ROOT` / `data/vault_target.json`,
   typically Core of Truth)

Chat binds that default when no workspace is posted and the client did not
send `workspace_opt_out`. Clearing the workspace pill is the opt-out.

## Related

- `static/js/workspace.js`
- `routes/chat_routes.py` (`_resolve_default_vault_workspace`)
- `docs/README/VAULT_TARGET.md`
