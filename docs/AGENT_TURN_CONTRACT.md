# Agent turn contract

Scope: product Agent turns on 7011. Environment-owned native/TUI bridges retain
their existing execution contract. No model weights or training settings change.

## Boundaries

1. `src/turn_contract.py` classifies capabilities, including explicit compound
   requests and referential follow-ups. Classification is selection, not permission.
2. `routes/chat_routes.py` resolves toggles, privileges, global/plan/incognito
   restrictions, fixture restrictions and available schema inventory before
   freezing the offered set. Web enabled alone does not select web tools.
3. `TurnContract` checks `required <= offered <= executable`, stores immutable
   serialized schema copies, and records unavailable requirements. An unavailable
   request stops without inference or substitution; unknown actions ask for clarity.
   Exact account-discovery requests narrow selection to account metadata only;
   compounds retain their declared family scope. Media operations declare their
   existing tool dependencies rather than falling back to shell generation.
4. The agent's prompt/schema route and fallback use that same logical scope.
   Native versus textual serialization remains model-specific. Answer-only phases
   can suppress tool calls without granting a different scope.
   Contract turns preserve the already-compacted conversation and tool-call/result
   IDs. The standalone specialist prompt's latest-message-only behavior is not used
   for these product turns. Prompt domains also come from the contract.
   Accepted in-scope calls retain their model-provided arguments and native IDs;
   the explicit-intent fallback must not overwrite them with the whole user turn.
5. The context-bound dispatcher checks membership **and** existing runtime policy,
   owner restrictions and exact-action approvals. A contract is not authorization
   to bypass those gates. Contract work bypasses terminating legacy shortcuts.
6. `_AgentRenderState` explicitly identifies streamed versus canonical output.
   Later synthesis transfers ownership with turn-scoped replacement. The frontend
   reconciles visible DOM, not just accumulated strings; tool evidence is retained.
   Ownership is included in saved metrics and `message_saved` events.
   History and resume honor replacement scope. Single-capability turns retain
   canonical output: an always-synthesize trial caused a live notes loop and was
   reverted. Compound turns cannot terminate after only one capability's result.

7. The contract is the single capability decision for chat turns. The agent loop
   executes it; it does not re-classify intent (no web-only narrowing of a
   combined vault+web contract). Its only narrowing is the explicit single-note
   vault-rewrite seal. Callers without a contract (scheduler, background jobs)
   keep the legacy tool-RAG path.

## Web toggle

The Agent web toggle is sent as `allow_web_search=true|false` on every UI turn.

- **Explicit off (`false`) is absolute for that request.** Every web-retrieval
  entry point is denied in schema selection *and* at execution:
  `web_search`, `web_fetch`, `get_weather`, `private_browser`, browser MCP tools,
  `youtube_tool`, `search_hf_models` and `trigger_research` (Deep Research is
  web-only). Earlier exemptions no longer apply when the toggle is explicitly off:
  - a **pasted URL** no longer re-enables `web_fetch` for that exact page;
  - an **explicit browser request**, including the reviewed **email unsubscribe**
    flow that opens the unsubscribe link in `private_browser`, is refused. Turn
    web on to use those flows;
  - a contextual web follow-up cannot re-enable web.
- A `web_egress` marker travels with the request authority into every child
  authority. The dispatcher refuses web forms of local-capable tools before
  admission, so no grant or approval can bypass the toggle: `pdf_extract` with a
  URL and `research_parallel`'s web branch are refused; local PDFs and the vault
  branch still run.
- Authorized local work continues: vault search/reads, saved research
  (`manage_research`), `research_parallel` with `mode=vault`, workspace reads.
  A combined vault+web request answers the vault part and states that the web
  part could not be checked; a purely web request gets a short refusal.
- **Unset toggle** (API callers that omit the field) is not explicit off: web
  tools follow prompt intent as before, and Deep Research stays available.
- **Read-only tools never need an approval prompt** when their scope is enabled
  (web search/fetch, weather, read-only browser actions, Deep Research, vault and
  workspace reads). Approvals remain for writes, shell and other effects.

## Seamless chat

Chat mode asks the same capability classifier the contract uses. When it
selects a real family (vault notes reachable through an authorized root,
personal stores, ...) or the message is a computation, the turn runs on the
agent runtime; casual turns stay plain chat. Web-only and Deep Research needs
keep their existing toggle-respecting escalation rules. The UI presents an
escalated turn as a chat turn (chat pill, neutral status text, mode toggle
untouched). Permissions, toggles and approval prompts are unchanged.

## Pre-stream chat errors

`/api/chat_stream` failures before streaming carry a code in
`detail.error` (`TOOL_APPROVAL_EXPIRED`, `TOOL_APPROVAL_UNAVAILABLE`,
`TOOL_APPROVAL_INVALID_DECISION`, `TOOL_APPROVAL_PLAN_MODE`,
`TOOL_APPROVAL_NOT_ALLOWED`). `static/js/chatErrors.js` classifies by code,
never by wording; only `MODEL_TOOLS_UNSUPPORTED` (reserved for an explicitly
detected capability failure) may say the model cannot use tools or switch the
user's mode. Pending approvals live in memory for 10 minutes and do not
survive a restart; an expired one asks the user to resend.

## Routing diagnostics

Every chat turn writes one `tool_routing` event to the debug console
(`data/debug_console.sqlite3`): the runtime profile, contract families, and for
each tool whether it was registered, authorized, discoverable, selected and
advertised, plus the first stage that excluded it and the schema-token estimate.
Names, reason codes and counts only — no prompt text, arguments or outputs.

```sh
python scripts/tool_routing_why.py --tool research_parallel   # latest turn
python scripts/tool_routing_why.py --excluded                 # exclusions by reason
python scripts/tool_routing_replay.py --live --isolate        # replay eval vs. the live model
```

The replay runs the real route and agent loop against a scratch copy of `data/`
and never executes tools; the app key is copied only for `--live` and removed as
soon as the endpoint headers are resolved.

## Verification

Use the project's configured Python environment, not an unrelated system Python:

```sh
python -m pytest -q \
  tests/test_turn_contract.py tests/test_turn_contract_integration.py \
  tests/test_agent_turn_contract_boundaries.py tests/test_turn_rendering_js.py \
  tests/test_contract_prompt_conversation.py tests/test_product_turn_contract_route.py \
  tests/test_contract_explicit_fallback.py \
  tests/test_history_resume_rendering_js.py \
  tests/test_chat_route_tool_policy.py tests/test_tool_policy.py \
  tests/test_frontend_module_version_parity.py
node scripts/verify_agent_turn_contract.mjs --max-turns 80 --total-ms 900000
```

The browser verifier uses `sft_alex_creator` and actual 7011 Agent controls. It
captures request toggles, SSE contract/tool events, visible output and persisted
history. Ten families have four initial/follow-up Web-toggle combinations.
Blocked or unrun cases are not passes. Email requires verified fixture isolation;
do not enable global fixture mode on the user's live service to make a test pass.

## Remaining limits

- Classification is deterministic and vocabulary-based, not a proof of semantic
  understanding. Add independent behavior examples for confirmed misses.
- Schema registration and policy permission do not guarantee a remote provider
  stays healthy throughout a turn. Runtime failure must remain visible.
- Separate tool/argument errors, tool-service failures, rendering failures and
  verifier defects in reports. Do not infer model accuracy from routing alone.
- Canonical summaries can still ignore presentation constraints such as a
  requested item count. Do not count those as full functional passes. Forcing an
  extra model round is not a validated general repair for this deployed model.
- Keep all imports of a local JS module on the same URL identity. Distinct query
  versions instantiate separate module state even when source files are identical.

Live baseline and current matrix results are in `reports/agent-turn-contract-*`.
The implementation is not a claim that every family has passed live verification.
