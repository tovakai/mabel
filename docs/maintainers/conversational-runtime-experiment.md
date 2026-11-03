# Experimental conversational Agent runtime

This is a **server-owned, default-off routing experiment**, not a new dispatcher and not a final Chat/Agent unification. It lets a normal Chat turn use Mabel's existing Agent lifecycle so models can request tools without first matching a Chat-mode keyword classifier.

## Enable and revert

Set `ODYSSEUS_CONVERSATIONAL_AGENT=true` in the backend process environment, then restart that process. Unset or set it to `false` to restore normal routing. Do **not** set it through untrusted client FormData or a request header.

This switch only affects ordinary Chat turns. Explicit Agent, comparison, plan, and approval-continuation flows retain their established paths. Accounts with `can_use_agent=false` cannot activate the experiment.

Normal Chat, with the switch off, is unchanged.

## Runtime boundaries

- Chat is promoted before the context and capability pipeline, so it uses the existing Agent stream, request authority, tool policy, exact approval, event journal, and completion checks.
- Interactive non-compact models now receive an immutable, policy-filtered discovery contract with only `discover_tools`, `ask_user`, and `update_plan` initially offered. The model can select additional schemas from this request's catalog; doing so does not grant execution authority. If `discover_tools` is itself disabled, no discovery is offered.
- The legacy Agent loop still performs relevance work for prompts and uses the model's configured tool transport. Catalog selection is available to the existing native-tool loop; malformed tool calls or missing provider support remain possible.
- The server switch does not alter a model's persisted tool-surface setting (full/compact/none). `full` still means all policy-allowed native schemas, while other settings retain their normal behavior.
- Compact models retain the configured compact runtime and its selected request contract. The experiment does not yet eliminate the compact contract's relevance selector.
- Lightweight Chat promotion still blocks bash, python, and file writes when there is no explicit workspace-agent intent. File reading remains unavailable unless an existing *server-validated* workspace or active-directory scope is bound.
- Current privilege, disabled-tool, incognito, delegated credential, review, and path-containment gates remain authoritative. Model tool choice cannot override them.
- The Chat UI receives an auto-escalated Agent mode event; this is a visible runtime experiment, not yet a presentation-neutral unified mode.

## What this does not yet fix

- Legacy agent tool schema retrieval may still fail or miss relevant tools.
- Backend models may produce malformed tool calls, ignore offered schemas, or fail to continue after a tool result. The first dynamic discovery call must be model-initiated; the model can also answer conversationally without it.
- Conversational context loading, provider normalization, request-local discovery, authorization, tool results and completion remain distributed across modules. This switch reuses that machinery; it doesn't merge the implementations.
- Direct Chat remains the fallback/rollback path until a wider integration suite and live inference show acceptable behavior.

## Regression gate

```sh
.venv/bin/python -m pytest -q --tb=short \
  tests/test_conversational_agent_opt_in.py \
  tests/test_reasoning_fallback_contract.py \
  tests/test_chat_runtime_audit.py \
  tests/test_chat_processor_pinned_memory.py \
  tests/test_foreground_model_routing.py
```

Before turning the switch on in a real account, manually verify: short no-tool talk, follow-up contextual read, denied and approved exact tool requests, out-of-scope filesystem read rejection, incognito, missing tool discovery, model tool-call failure, incomplete stream, and disabled Agent privileges. Keep it off for unattended tasks until those cases pass.
