# FS_DEVICES

**Path:** `src/fs_devices/`

## Purpose

Central multi-device filesystem registry and adapters. Devices describe where files live; allowed directories are persistent grants; conversation scope selects active grants for a turn.

## Modules

| File | Responsibility |
|---|---|
| `ssh_parse.py` | Parse familiar `ssh …` lines into structured settings; reject shell metacharacters |
| `registry.py` | Durable device/grant store (`data/fs_registry.json`) with atomic writes and locking |
| `adapters.py` | `LocalAdapter` and `SftpAdapter` behind a shared list/stat/read/write surface |
| `connections.py` | Paramiko SSH/SFTP connect, known_hosts accept flow, password-auth guidance |
| `path_ops.py` | POSIX/Windows logical path join/confine without OS mounts |
| `scope.py` | Intersect conversation active ids with enabled grants |
| `turn_bind.py` | Bind request/session scope for a chat turn (no FormData grant minting) |
| `seal.py` | Seal local active grants into `RequestAuthority.resource_roots` |
| `remote_ops.py` | Scope-aware resolve/read/write with RO enforcement |

## Related routes

`routes/device_routes.py` — `/api/devices`, `/api/allowed-directories`, browse/test/host-key.

A primary local directory is an optional shell working directory. Tools do not require it. Remote-only conversations do not inherit a local cwd. Bash and Python accept an optional per-call `cwd` inside an authorized local directory or the documented default workspace.
