# Mabel Phase 00.5: candidate admission foundation (experimental)

**Status: code shipped to experimental GitHub branch; live capture is OFF by
default and has not been deployed or runtime-tested.** The initial nine queue
tests passed on the operator's Arch test environment on 2026-11-01. The new
event-delivery and capture tests still need to be run. This milestone implements
part of Phase 00.5 and extends the **existing** Phase 00 typed event ledger.

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
  typed transition envelope with a stable event ID. The explicit
  `flush_candidate_events(queue, ledger)` adapter delivers those events into
  the **already implemented Phase 00** JSONL event ledger
  (`src/agent_runtime/event_ledger.py`). The ledger now recognizes the
  memory event family, and `Ledger.append_once` deduplicates replay after
  a crash. The outbox is not a second authoritative ledger.
  No background publishing or production capture is enabled.
- `src/episodic_memory/capture.py` stages a **completed, persisted** chat/agent
  turn under its authoritative saved assistant DB message ID. It stores a short
  literal "User said / Mabel replied" excerpt, explicitly *unverified*, not
  a model-generated claim about what happened. A replay of the same message ID
  produces no duplicate candidate.
- `routes/chat_helpers.py:run_post_response_tasks` calls this capture at the
  existing post-save boundary, for normal and agent chat. The route forwards
  `no_memory`, incognito/compare flags, and saved message identity. Missing
  persistence IDs, disabled memory settings and short turns do not create
  candidates.
- `python -m src.episodic_memory` provides offline inspection and explicit
  one-shot publishing of the outbox into Phase 00's canonical ledger. Nothing
  runs a background event publisher automatically.
- No heavy model inference, automatic LLM extraction, external model/server
  dependency, scheduler, background loop, Markdown write or vault access.

The chat integration uses `DATA_DIR/episodic_candidates`: local application
state, **not** a synced Markdown directory. Standalone callers may additionally
pass `protected_roots` to reject a selected Core of Truth path. Do not put
the SQLite DB, `-wal` or `-shm` in a synced vault. No code reads or writes
Core of Truth during this milestone.

## Phase 00 is already implemented. Verified integration point

The original Phase 00 code **is present** on this branch, inherited from
`feat/automatic-dottie-vault-indexing-20261101`:
- `src/agent_runtime/event_ledger.py`: strict typed JSONL ledger, locking,
  reader and CLI;
- `src/agent_runtime/durable_jobs.py`: fenced leases, jobs and receipts;
- `docs/maintainers/phase0-contract.md` and `phase0-validation.md`:
  contracts and previous Windows tests;
- `tests/test_phase0_foundations.py`: ledger and job lifecycle tests.

Do not build another ledger or state machine. The **only Phase 00 extension**
in this slice is registering the proposed memory event types and adding
`Ledger.append_once(Event)` to safely publish already-created event IDs
with an exact-payload replay check. This is a cross-process lock + linear
scan, suitable for small local ledgers; benchmark/optimize it before
high-frequency event publication.

The currently staged events use the existing strict event envelope, so they
publish only opaque candidate/source references and decision codes. Their
full proposals and interpretations stay in owner-scoped local SQLite. A
source event that is not a canonical `evt_...` ledger event is stored as a
source reference, not asserted as an existing causal ledger link.

## Things deliberately NOT implemented

The following are separate integration gates:

1. Implement **session deletion/retention propagation** into the staged
   candidate queue, and verify post-save source IDs across the different
   actual chat paths. Do not enable capture in production until deletion
   behavior is covered.
2. Build Dottie-assisted bounded significance extraction, candidate selection
   and source verification. A model's
   self-interpretation is not a verified autobiographical occurrence.
3. Establish Mabel's writable vault target independently of Core of Truth.
   Once source proof and the existing authorization path are in place,
   implement promotion/merge into stable-ID Markdown under Mabel's `memory/`.
4. Add association graph, lexical and optionally BGE-M3 recall beside
   existing factual memory. Only after tests, enable bounded opt-in
   conversational injection; retain model-independent fallback.

No data from real conversations, Obsidian notes or `memory.json` have been
migrated, modified or collected by this change. Delivery is explicit and
disabled unless called by trusted integration code.

## Developer tests (synthetic temporary databases only)

```bash
.venv/bin/python -m pytest -q --tb=short \
  tests/test_episodic_candidate_foundation.py \
  tests/test_episodic_phase0_delivery.py \
  tests/test_episodic_completed_turn_capture.py \
  tests/test_phase0_foundations.py
```

The tests cover disabled/incognito capture, source identity, duplicate
replays, restart persistence, concurrent admission, owner isolation,
reject/defer/quarantine states, event-outbox redaction, invalid input,
the absence of promotion, and protected source-vault non-mutation.

**Do not interpret passing tests as permission to enable production
capture.** This is a reviewed opt-in implementation slice only.


## Operator pilot: initially disabled

Production Mabel has no automatic episodic extraction enabled by this branch.
For a controlled test environment only, set `MABEL_EPISODIC_CAPTURE_ENABLED=1`
on the Mabel server process **before** starting a test conversation.

The capture hook runs only after a completed assistant reply is saved with a
persistent message ID; it is skipped for incognito, `no_memory`,
memory-disabled sessions/preferences, compare turns, explicit approval
continuations and internal tool requests. It makes **no additional LLM calls**
and does not persist raw thinking traces or tool outputs.

Inspect with the same Mabel account owner whose session is being tested:

```bash
.venv/bin/python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER list
.venv/bin/python -m src.episodic_memory --owner ACTUAL_MABEL_OWNER events
```

The `publish-events` command explicitly replays and acknowledges pending
transitions in `DATA_DIR/phase0/events.jsonl` using the same idempotent typed
ledger from Phase 00. **Do not enable unattended publishing until operators
validate the memory event schema and session deletion controls.**

Turn the experimental capture off by removing the env var. No existing
`memory.json`, Markdown notes, embedding endpoints or prompt fragments are
changed by this experimental feature.

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
