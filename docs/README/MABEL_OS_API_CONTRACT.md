# MABEL-OS client contract

The HTTP surface both MABEL-OS clients speak: the Python prototype (`tui/`) and the Rust port
(`tui-rs/`). They share no code, so this file is the shared part. When a client starts using an
endpoint or a field, add it here; when the server changes one, both clients change with it.

Written 2026-11-18 from the Python client as it stood at ea6082d6.

## Config (client side, not HTTP)

`~/.config/mabel-tui/config.toml` (honours `$XDG_CONFIG_HOME`), mode 600. Both clients read the same file.

| key | type | default | meaning |
|---|---|---|---|
| `url` | string | **required** | Mabel's base URL, no trailing slash (`http://127.0.0.1:7000`, or the tailnet URL) |
| `token` | string | **required**, starts `ody_` | API token, profile `tui` |
| `volume` | int | 70 | player start volume |
| `index_url` | string | `http://127.0.0.1:8766` | tk-index (Gremlin Index) |
| `vault` | string | `~/Documents/core of truth` | his Obsidian vault |
| `journal` | string | `~/Documents/mabel/journal` | Mabel's journal (second vault) |
| `preset` | string | `custom` | chat preset carrying her character; `""` = none |
| `avatar` | string | `""` | avatar folder (avatar.json + strips) or one PNG |
| `avatar_fps` | float | 4.0 | avatar strip speed |
| `offline_bitrate` | int | 0 | downloads: 0 = originals, else kbit/s |
| `pixel_dir` | string | `~/Pictures/MABEL-OS` | PIXEL files |
| `tracker_dir` | string | `~/Music/MABEL-OS` | TRACKER songs |
| `tracker_rate` | int | 44100 | TRACKER sample rate |
| `deck` | bool | false | the cyberdeck: full SYS |
| `auto_update` | bool | false | install pushed updates after 5 idle minutes |
| `palette` | string | `default` | `default` or `deck` |
| `[colors]` | table | — | per-colour overrides, `#rrggbb`, keys as in the palette |
| `[navidrome]` | table | — | `url`, `user`, `password` (all three, or the section is an error): AUDIO goes straight to Navidrome |

Install state: `~/.local/share/mabel-tui/build` holds the build id an installed copy came from.
A copy run from source has no such file and never offers updates.

## Auth

Every `/api/*` call sends `Authorization: Bearer <token>`. Profile `tui` grants these scopes:

| scope | covers |
|---|---|
| `chat` | `/api/sessions`, `/api/session`, `/api/history/*`, `/api/chat_stream` |
| `music` | `/api/music/*` |
| `life:read` | `/api/tui/life`, `/api/tui/dream-image` |
| `messages` | `/api/beeper/*` |

Defined in `routes/api_token_routes.py` (`TOKEN_PROFILES["tui"]`).

## Chat (`chat`)

**GET `/api/sessions`** → a list, or `{"sessions": [...]}`. Per session the client uses
`id`, `name`, `archived`, `last_message_at`, `updated_at`, `endpoint_id`, `endpoint_url`, `model`.
The client drops archived ones and sorts newest first by `last_message_at`, falling back to `updated_at`.

**GET `/api/history/{session_id}`** → `{"history": [{"role", "content", ...}]}`. Only `user` and `assistant` roles are shown.

**POST `/api/session`** (form) `name, endpoint_id, endpoint_url, model` → the new session object (`id`, …).
Tokens can't make sessions with `skip_validation` or credentials, so the client copies
the endpoint and model from the newest existing session.

**POST `/api/chat_stream`** (form) `message, session, mode=chat`, plus `preset_id` when the preset isn't empty.
To resume after an approval, send `message=""`, `tool_approval_id`, and `tool_approval_decision` (`approve_once` | `deny`).
The response is SSE. Each line is `data: <json>`, and the stream ends with `data: [DONE]`. Events:

| shape | meaning |
|---|---|
| `{"delta": "..."}` (no `type`) | reply text chunk |
| `{"type": "final_response", "content"}` | the whole reply (replaces streamed text) |
| `{"type": "tool_start", "tool" \| "name"}` | a tool began |
| `{"type": "tool_output", "ask_user": {"approval_id", ...}}` | needs approval: show allow / deny |
| `{"type": "ui_control", "data": {"music_command": {...}}}` | control_music: the client plays it |
| `{"error": "..."}` or `{"type": "error", "error"}` | an error line |

Thinking is not streamed. A non-2xx status means the body is the error.

## LIFE (`life:read`)

**GET `/api/tui/life`** → `at, doing, jobs, dream{title, at, first_line, text, has_image, note},
song{title, at, text}, paper{at, why, text, note}, book{title, author, progress}, news{day, message},
groceries, gremlin_out[{item, id, days_out}]`. Any block may be `null`.

**GET `/api/tui/dream-image`** → the last dream's picture as bytes, or 404.

## Music (`music`), Mabel's Navidrome proxy

- **GET `/api/music/rest/{method}?…`** returns Subsonic JSON (the `subsonic-response` body).
- **POST `/api/music/rest/{method}`** takes a JSON body of params and is used for writes.
- Methods the client uses: `getAlbum getAlbumList2 getArtist getPlaylist getPlaylists getRandomSongs search3 createPlaylist updatePlaylist scrobble`.
- **GET `/api/music/cover/{id}?size=300`** returns image bytes.
- **GET `/api/music/stream/{song_id}`** returns audio. mpv sends the bearer header.

With `[navidrome]` in the config, the client talks Subsonic to Navidrome directly instead of using these routes.

## INBOX (`messages`), Beeper relayed

- **GET `/api/beeper/status`** returns `url, token (bool), reachable, version, accounts, token_error?, error?`.
- **`/api/beeper/{path}`** relays to Beeper Desktop's API. Only the paths in `src/beeper.ALLOWED` pass; anything else is a 403.
- 503 means Beeper has no token yet.
- Errors carry `detail` or `message`.
- The client uses `v1/chats`, `v1/chats/search`, `v1/chats/{id}/messages` (GET and POST), `v1/chats/{id}/messages/{mid}` (PUT), `…/reactions` (POST), `v1/chats/{id}/read|unread` (POST), and `v1/assets/serve`.

## Updates and install (no auth)

- **GET `/tui/version`** returns `{"build": "<hash of tui/>"}`.
- **GET `/tui/install.sh`** returns the installer.
- **GET `/api/tui/whoami`** and **POST `/api/tui/pair`** handle tailnet pairing.

The installer and versioning will need a per-architecture Rust binary later (phase 6). Until then these routes serve only the Python build.

## tk-index (INVENTORY), separate service at `index_url`, no auth

- **GET `/api/world`** returns `layouts, items, synonyms, icons, …` (the whole place model).
- **GET `/api/items/{id}`** returns one item with `movements, children, images, nicknames, …`.
- **GET `/images/{path}`** returns bytes.
- **POST `/api/items/{id}/edit`** takes the changed fields.
- **POST `/api/items/{id}/move`** takes `{destination, reason}`.
- **POST `/api/items/{id}/checkout`** takes `{destination|null, reason}`.
- **POST `/api/items/{id}/return`** takes `{}`.
- **POST `/api/items/{id}/nickname`** takes `{add}`.
- **GET `/api/undo`** returns `{last}`, and **POST `/api/undo`** undoes it.

The place model (routes, redirects, titles) is ported from tk-index's web UI. See `tui/mabel_tui/tkindex.py`.

## Print server (the deck, `mabel-print`)

`GET /status` and `POST /print` live on the deck, not on Mabel. See `tui/mabel_tui/printserver.py`.
