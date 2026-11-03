# TOOL_CAPABILITIES

**Path:** `src/tool_capabilities.py`

## Purpose

Deterministic effect classification for built-in tools, plus the run-local
external-untrusted-context approval gate (`ToolRunSecurityContext`).

Model output requests an action; it never supplies authority. Effects decide
whether a tool stays free after external content has been observed, or must
stop for exact user approval.

## Read vs egress (2026-11-03)

- Known pure reads never need a post-taint approval card. That set is
  `READ_ONLY_EFFECTS`: `READ_PUBLIC`, `READ_WORKSPACE`, `READ_PRIVATE`,
  `BROKERED_NETWORK_READ`, and `USER_INTERACTION`. Concrete helpers:
  `action_is_read_only` / `capabilities_for_action`.
- Examples that pass without approval after search/vault/discovery taint:
  `manage_notes` search/find/list/view, `manage_memory` list/search,
  `read_email` / `list_emails`, `vault_search`, `web_search`, `web_fetch`,
  workspace `read_file` / `grep`.
- Brokered public reads (`web_search`, `web_fetch`, `get_weather`,
  `youtube_tool`, `private_browser`, `pdf_extract`) use
  `BROKERED_NETWORK_READ` only (no open `NETWORK_EGRESS`).
- Open egress / mutation (`bash`, writers, email sends, `chat_with_model`,
  `pipeline`, …) keep write / execute / egress / side-effect effects in
  `POST_EXTERNAL_BLOCKED_EFFECTS` and remain blocked once external untrusted
  context is seen, unless an exact or chat-session approval lifts the gate.
- Interactive owner authority also seals personal store read grants
  (`_with_owner_read_grants`: notes/memory/calendar/email/vault/…, not admin
  managers). Tool-level owner checks still apply. Ungranted non-personal
  tools (custom MCP, shell, writes) still use authority-gap approval.
- Local `/workspace/` `web_fetch` paths still classify as `READ_WORKSPACE`
  via `capabilities_for_action`.

## Related

- Approvals store: `src/tool_approvals.py`
- Authority grants: `src/agent_runtime/authority.py`
- Tests: `tests/test_web_fetch_post_search_approval.py`,
  `tests/test_external_context_tool_gate.py`,
  `tests/test_agent_memory_read_no_approval.py`
