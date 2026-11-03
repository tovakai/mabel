# CHAT_RENDERER

**Path:** `static/js/chatRenderer.js` (live wiring also in `static/js/chat.js`,
styles in `static/css/03-agent-chat.css`)

## Purpose

Message rendering helpers for chat history and shared tool-thread chrome.

## Agent thread action fold (2026-11-03)

- `refreshAgentThreadFold(thread)` keeps a single summary button
  (`.agent-thread-fold`: “N actions · labels…”) at the top of each
  `.agent-thread`.
- Completed action rows stay hidden until the thread has `.expanded`.
  Click the summary to expand/collapse.
- While a tool is `.running` (or a private-browser preview node), that row
  stays visible so live progress is not buried.
- Per-tool detail panes still use `.agent-thread-node.open` / chevron as
  before; expanding a single row also opens the thread summary.

## Related

- Live stream: `static/js/chat.js` (`tool_start` / `tool_output`)
- Styles: `static/css/03-agent-chat.css`
- Tests: `tests/test_agent_thread_chevron_static.py`
