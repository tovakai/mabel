# Phase 0 implementation and validation on zum

The verified runtime map is in `architecture-baseline.md`; the instruction,
authority, privacy, durability and rollback contracts are in
`phase0-contract.md`. The implementation is in the zum checkout. Existing
uncommitted files, settings, conversations and vault documents were preserved.

`src/agent_runtime/tool_catalog.py` reports semantic-index state separately
from catalog matches. When workspace inspection is relevant and the semantic
index is unavailable, it retains the native search/read alternatives alongside
lexical matches. Selection does not grant execution authority.

`src/agent_runtime/event_ledger.py` implements a typed JSONL ledger with locked
concurrent appends, tail detection, validation and an offline reader.
`src/agent_runtime/durable_jobs.py` implements opt-in SQLite job state, fenced
leases, checkpoints, explicit retry/yield/cancellation and an atomic database
artifact plus completion receipt. Its default producer records job transition
metadata in the ledger. It neither starts a worker nor alters the existing
scheduler. `tests/test_phase0_foundations.py` exercises the new paths.

Windows verification on 2026-10-29:

- Phase 0, discovery, index lifecycle and readiness: 35 passed, 6 warnings.
- Existing approval-gap and context routing: 46 passed, 1 warning.
- Workspace intent and denial boundaries: 12 passed, 1 warning.
- A larger combined approval/workspace/audit selection timed out after 300
  seconds; it is not counted as passing. A smaller audit selection was launched
  separately. No real model, GPU or vault was used by these fixture tests.
- Ordinary and compact mock agent sequences covered an invalid discovery call,
  recovery, workspace selection, listing, a file read and model continuation.
  A separate test calls the production executor with an autonomous authority
  and verifies that an attempted write does not change a synthetic note.

The authenticated readiness endpoint already reports tool-index readiness;
an unauthenticated live request returned 401. The updated launcher was restarted
and returned HTTP 200 on `127.0.0.1:7011`. The original discovery file is backed
up under `C:\Users\ZUM\.mabel-deploy-backups\phase0-cfc8ca44892b4e0396527b4a483716ab`.

Known limits: the ledger currently records job transitions when the job store
is used, and non-incognito turn/discovery events are now bridged by the debug trace writer on a best-effort basis; the
job/ledger cross-file handoff is not transactional and a crash can lose a
diagnostic event. SQLite completion protects its database artifact and receipt,
not external side effects. No autonomous worker is enabled, so a future worker
must explicitly bind `autonomous_authority` and disable interactive approval
escalation. No live model conversation was run as validation. See the contract
for data retention, Windows ACLs and rollback.
