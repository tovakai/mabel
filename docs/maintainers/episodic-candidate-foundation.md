# Mabel Phase 00.5: candidate admission foundation (experimental)

**Status: code scaffold only. Not enabled in Mabel, not deployed or runtime-tested.**
This milestone follows the user's proposed Phase 00.5 note without claiming to
implement all of episodic memory or the Phase 00 typed event ledger.

## What exists

`src/episodic_memory/candidates.py` contains a small standard-library
candidate admission layer, deliberately separate from `src/memory.py`,
`src/memory_vector.py`, automatic fact extraction, agent processing, and
Obsidian vault mutation.

- `CandidateDraft`: bounded `experience`, `decision`, `mistake`,
  `discovery`, `open_question` records. Requires `actor`, `source_kind`,
  stable `source_ref`, stable `source_event_id`, `origin`, and summary.
  Interpretations are separate from observations. Every draft starts
  **unverified**.
- `CapturePolicy`: requires explicit `episodic_enabled=True`,
  `memory_enabled=True`, `incognito=False`. Defaults to disabled; the
  future chat integration must obtain these from trusted request/session
  state, never from a model's assertion.
- `CandidateQueue`: a per-owner, local SQLite staging queue. Insertion is
  idempotent by owner + source identity + candidate kind, including replay
  after process restart. Rejections, deferrals and quarantines are explicit.
  **No promotion, merge, recall or autobiographical injection exists yet.**
- Event **outbox**: every database mutation atomically enqueues a tiny,
  typed transition envelope with a stable event ID. This is an unpublished
  delivery buffer for the future Phase 00 ledger adapter, **not a second
  authoritative ledger**. No automatic event delivery is running.
- No heavy model inference, automatic extraction, external model/server
  dependency, scheduler, background loop, Markdown write or vault access.

The queue's caller must choose a local, non-synchronized state directory
(such as a private subdirectory of Mabel's device-local `data/`) and
pass the user's `Core of Truth` as a `protected_roots` path. Never place
the SQLite DB, `-wal` or `-shm` alongside synced Markdown.

## Things deliberately NOT implemented

The current action journal and effect log are focused on execution receipts
and operation effects; they are not yet shown to implement the proposed
`memory.candidate_*` typed event families. Consequently, this scaffold
does not claim Phase 00.5 transition/event integration. It must not be wired
to live chat until the actual typed ledger schema is inspected and a
durable idempotent publisher is provided.

The following are separate integration gates:

1. Find/confirm the Phase 00 typed event-ledger implementation; implement a
   publisher with idempotent `event_id`, publish-before-ack semantics,
   recovery of unacknowledged outbox entries, privacy filtering and
   testable authorization. A crash after ledger write but before outbox
   acknowledgement must not create duplicate events.
2. Bind the capture policy to actual memory-enabled/incognito/no-memory
   session settings and honor user deletion/retention controls.
3. Build bounded extraction and *trusted* source verification. A model's
   `source_kind` or self-interpretation is not a verification receipt.
4. Establish Mabel's writable vault target independently of Core of Truth.
   Only after source proof, explicit review and event-ledger integration
   implement promote/merge → stable-ID Markdown under `memory/`.
5. Add association graph and lexical recall beside existing fact memory,
   then opt-in prompt injection capped at a few short snippets, with
   negative retrieval fallback. Defer automatic decay, dreams and
   Curiosity Whisper to later phases.

No data from a user's actual conversation, Obsidian source vault or
legacy `memory.json` has been migrated, modified or collected by this change.

## Developer tests (synthetic temporary databases only)

```bash
.venv/bin/python -m pytest -q --tb=short \
  tests/test_episodic_candidate_foundation.py
```

The tests cover disabled/incognito capture, source identity, duplicate
replays, restart persistence, concurrent admission, owner isolation,
reject/defer/quarantine states, event-outbox redaction, invalid input,
the absence of promotion, and protected source-vault non-mutation.

**Do not interpret passing tests as permission to enable production
capture.** This is a reviewed opt-in implementation slice only.

### Prototype API

```python
from src.episodic_memory import CandidateDraft, CandidateQueue, CapturePolicy

queue = CandidateQueue(
    "/absolute/path/to/private/local/data/episodic",
    owner="example-user",
    protected_roots=("/absolute/path/to/Core of Truth",),
)
# Only trusted application code supplies the policy and source identifiers.
row = queue.propose(
    CandidateDraft(
        kind="open_question", actor="mabel",
        source_kind="tool_observation",
        source_ref="session-123/turn-4/call-8",
        source_event_id="receipt-8",
        summary="An inspected source left a question unresolved.",
        origin="verified_tool_receipt",
    ),
    policy=CapturePolicy(episodic_enabled=True, memory_enabled=True),
)
assert row["state"] == "pending"  # Never recallable.
```

This example is synthetic. Future integrations must not let a model forge a
trusted receipt simply by writing source-shaped text.
