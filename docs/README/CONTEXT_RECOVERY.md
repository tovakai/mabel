# CONTEXT_RECOVERY

**Path:** `src/agent_runtime/context_recovery.py`

## Purpose

Compact the agent’s canonical message history after a successful provider context recovery without discarding harness metadata.

## Behavior

| Helper | Role |
|---|---|
| `compact_canonical_messages_for_limit` | Runs `trim_for_context` against the live messages using the learned ceiling, preserving `_protected` tails and tool-call/result pairs |
| `answering_recovery_bag` | Selects the applied, uncommitted bag for the answering candidate index only |

Diagnostics are optional and named to separate stages: `compaction_prepared` (proactive trim), `context_recovery_prepared` / `context_recovery_applied` (provider retry), and `context_recovery_committed` (canonical adopt). Correct persistence does not require any of them.
