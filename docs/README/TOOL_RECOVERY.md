# TOOL_RECOVERY

**Path:** `src/agent_runtime/tool_recovery.py`, `src/agent_runtime/tool_catalog.py`

## Purpose

Classify tool and discovery failures so a recoverable error is returned to the model, and so a finished turn cannot be an empty assistant message.

## Behavior

| Situation | Result |
|---|---|
| Unknown tool name | `unknown_tool`, recoverable |
| Name removed from this request catalog | `unavailable_tool`, recoverable |
| Authorization denial | `authorization_denied`, not recoverable |
| Bad discovery arguments | `invalid_arguments`, recoverable |
| Broken schema | `invalid_schema`, not recoverable |
| Discovery exception | `internal_discovery_failure`, not recoverable |
| Repeated identical failure | bounded by the existing failed-round limit (2 for interactive turns) |

`failure_is_recoverable` stops the agent loop from treating a long draft plus one of these errors as a completed answer. `ensure_visible_failure` fills an empty final reply from the last failed tool event.

Logs include the error code and requested tool names only.
