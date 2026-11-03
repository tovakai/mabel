# VAULT_TARGET

**Path:** `src/agent_runtime/vault_target.py`

## Purpose

Choose the Obsidian vault independently of the active workspace and of authorization roots.

## Resolution

1. An explicit tool path is used as given and still has to pass resource binding.
2. An omitted vault or index path uses `MABEL_VAULT_ROOT`, then `data/vault_target.json` when that file already exists.
3. If neither is set, the tool returns `vault_unconfigured`. The authorization root is not scanned and the index is not rebuilt.

## Default active workspace (2026-11-03)

When no workspace is posted and the user has not opted out, chat binds the
configured vault path as the active workspace (same path as above). WebUI
`GET /api/workspace/default` prefers `ODYSSEUS_WORKSPACE_DEFAULT`, then this
vault target. Clearing the workspace pill sets an opt-out sentinel so reload
does not silently re-bind it.

The index cache stays outside the vault (`VaultIndex` rejects a cache inside the Markdown tree). Property audit (`vault_audit`) does not validate broken wikilinks; unresolved links are reported by `vault_index_status`.

Inaccessible Windows reparse points during an audit are recorded as partial unreadable entries. Symlinks still fail closed.
