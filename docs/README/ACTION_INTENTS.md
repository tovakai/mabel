# ACTION_INTENTS

**Path:** `src/action_intents.py`

## Purpose

Chat→agent escalation patterns for typed actions (notes, email, vault, web,
workspace). These promote a conversation into agent mode with the right
capability families; they do not grant tool authority by themselves.

## Vault / parallel notes (2026-11-03)

- Vault note-content questions (`what do my notes say…`) escalate as workspace.
- Compare notes with online/public phrasing escalates as workspace (parallel
  research path), including forms without a leading “please”.

## Related

- Capability resolution: `docs/README/TURN_CONTRACT.md`
- Coordinator: `docs/README/PARALLEL_RESEARCH.md`
