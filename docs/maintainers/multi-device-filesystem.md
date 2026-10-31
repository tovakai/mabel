# Multi-device filesystem (Devices / Allowed / Active Directories)

## Model

| Concept | Meaning |
|---|---|
| **Device** | A machine Mabel’s desktop backend can reach (`local` builtin, or `ssh` via SFTP). |
| **Allowed directory** | Persistent grant: device + path + `ro`/`rw`. |
| **Active directories** | Per-conversation subset of allowed grants. |
| **Primary local directory** | Explicit local RW grant used for bash/python containment. |
| **Tool approval** | Existing exact-operation authorization, unchanged. |

The browser client never supplies the filesystem. Tailscale users talk to the desktop-hosted backend; SSH connections originate there.

## Persistence

- Registry: `data/fs_registry.json` (atomic write, `0o600`, flock).
- Host keys: `data/ssh_known_hosts` (explicit accept only; never auto-disable checking).
- Session columns: `active_directory_ids`, `primary_local_directory_id`; `cwd` mirrors the primary local path only.

## Migration

Trusted sources only:

1. `ODYSSEUS_WORKSPACE_DEFAULT`
2. Existing session `cwd` values that pass `vet_workspace`

Chat FormData / legacy `workspace` posts **do not** create grants. Unknown paths require Settings → Devices → Allowed Directories.

## SSH auth (v1)

- Key / `ssh-agent` / OpenSSH config alias + `IdentityFile`
- No password storage
- Password-only hosts return actionable setup guidance from Test Connection
- Supported parse subset: `ssh user@host`, alias, `-p`, `-i`, `-o IdentityFile=`

## Shell / remote

- Filesystem tools: local multi-root + SFTP
- `bash` / `python`: local primary only; remote-only sessions error (no stale cwd fallback)
- No remote command channel

## Modules

| Path | Role |
|---|---|
| `src/fs_devices/` | Registry, SSH parse, adapters, connections, scope, turn bind |
| `routes/device_routes.py` | Devices + allowed-directories + browse APIs |
| `static/js/devices.js` | Settings → Devices UI |
| `static/js/activeDirectories.js` | Composer active-directory selector |

## Manual validation

1. Desktop: Settings → Devices → confirm local device; authorize a local folder.
2. Laptop over Tailscale: open Mabel; add SSH device (`ssh user@tailscale-ip` or alias); Test; accept host key if prompted; browse; authorize a directory.
3. Conversation: activate local + remote grants; set primary local for shell; ask to read remote and write local.
4. Windows OpenSSH host: key auth in `authorized_keys`; browse Windows paths; authorize a folder; confirm RO grants reject writes.
5. Reopen conversation from another client: active directories restore.
