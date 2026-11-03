# OVERVIEW

Project documentation map for Mabel/Odysseus script and module notes under `docs/README/`.

## Multi-device filesystem (2026-10-28)

Mabel’s desktop-hosted backend owns filesystem access across registered devices:

- **Devices** — local builtin + SSH/SFTP remotes (`src/fs_devices/`, Settings → Devices)
- **Allowed Directories** — persistent grants (`ro`/`rw`) per device
- **Active Directories** — per-conversation subset; a preferred local shell directory is optional and is not required for tools
- **Tool authorization** — existing RequestAuthority / approvals unchanged

See `docs/maintainers/multi-device-filesystem.md` for architecture, migration, and manual validation.

## Workspace-less execution and tool recovery

Tool failures return structured, distinct error codes and stay recoverable when the operation was not denied. A long draft followed by a recoverable tool error does not end the turn. An empty completed reply is replaced with the tool failure. Vault and index tools use an explicit or configured vault path and do not substitute an authorization root. Shell cwd prefers an optional primary, otherwise the documented data-directory workspace; remote-only conversations do not inherit a local cwd. Model traces label provider timings separately from wall-clock time and mark queue, cache, and model-load timing unavailable when they were not measured.

See `docs/README/TOOL_RECOVERY.md` and `docs/README/VAULT_TARGET.md`.
