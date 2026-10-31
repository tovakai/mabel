# Phase 0 instruction, event and job contracts

## Instruction and permission boundary

Protected runtime instructions and the server-owned `RequestAuthority`,
`TurnContract`, tool policy, resource binding, exact approval and containment
checks govern execution. No prompt fragment, retrieved note, model response or
tool output can create a grant, override a denial, change an approved operation
or widen an allowed directory. Retrieved vault, web and Dottie content remains
untrusted data. Existing `prompt_security.untrusted_context_message` identifies
such content; execution still checks authority independently.

Phase 1 may propose versioned fragments with a unique opaque ID, layer,
content hash, enabled flag, bounded UTF-8 content and declared compatible
functions. Proposed precedence is protected runtime, CORE, FUNCTION, MOOD,
CURIOSITY, then untrusted evidence. CORE may express stable identity and values;
FUNCTION may suggest workflow; MOOD may change expression and cadence only;
CURIOSITY may supply optional context. A tired mood cannot reduce source
verification. A function that requests a forbidden write remains denied by
the actual authority and resource checks. Neither fragment is an authorization.

For a future composer, use explicit configuration with a default-off kill
switch and initial ceilings of 1,024 CORE, 768 FUNCTION, 256 MOOD and 128
CURIOSITY tokens, within the resolved model context. Reject unknown fields,
incompatible functions and any fragment exceeding its budget; choose at most
one function by configured priority with stable ID as tie breaker. Preserve
protected instructions during compaction and drop CURIOSITY, then MOOD, then
FUNCTION before CORE. A shadow composition must never be sent to the model;
compare shadow and baseline behavior with controlled A/B tests before activation.
These are a future contract, not an activated prompt composer.

`durable_jobs.autonomous_authority(parent)` derives only native read grants
already present in the parent, denies other registered tools, disables MCP and
process launches, and marks the result inherited. A future worker must bind
that authority and the normal resource/turn contracts and must not enable
interactive authority-gap approvals for unattended work. Phase 0 has no worker
or scheduler wired to this helper. No autonomous vault write is enabled.

## Event ledger

`src.agent_runtime.event_ledger` supplies a strict version-1 event envelope,
UTC time, opaque identifiers, typed kinds and actors, causal references,
optional usage and symbolic rationale codes. Free-form prompts, transcripts,
vault text, paths and arbitrary payload fields are rejected. References should
be opaque IDs or hashes, not filenames or personal descriptions. Explicit
producers record decisions at decision time; no retrospective model call is made.

The default job-store producer writes metadata-only job transition events to
`DATA_DIR/phase0/events.jsonl`. Job state commits first; an unavailable ledger
is logged and does not repeat an action. Consequently the JSONL file is an
audit aid rather than the transactional authority for job completion; it can
miss an event on an I/O failure or crash between the commit and append. Non-incognito chat turns and discovery events are also bridged through the debug trace writer. The bridge is best-effort and can miss events on queue overflow, I/O failure, or a crash; incognito turns are excluded. No journal is generated automatically.

Writers take a cross-process lock, append one bounded record and fsync. Readers
return valid records and identify malformed lines. Appends stop on a damaged
tail; preserve a copy before operator repair. On POSIX, directories/files are
created with restrictive mode bits; on Windows, privacy relies on inherited
user-directory ACLs and should be checked by the operator. Store DATA_DIR and
its lock files outside synchronized vault directories. Retain until the user
chooses to archive or remove it; export by copying the JSONL and recording its
date, with the same access controls. There is no automatic deletion.

Inspect without any model:

    venv\Scripts\python.exe -m src.agent_runtime.event_ledger --kind job.completed

Use `--path`, `--actor`, `--session-id` or `--job-id` for a different file or
filter. A malformed record makes the command return a nonzero status while
still reporting earlier valid events.

## Durable jobs

`src.agent_runtime.durable_jobs.JobStore` uses additive `phase0_jobs` and
`phase0_job_artifacts` tables in the existing local SQLite application database
when constructed without a path. Construction is explicit; startup and the
existing scheduler do not instantiate it. The backend currently requires SQLite.
The idempotency key is unique per owner; a reused key with different inputs is
rejected. Claim uses a transaction, a random lease token and an expiry. Only the
current unexpired token may checkpoint, yield, fail, renew or complete. Reclaim
after expiry changes the token and attempt, fencing late workers. Cancellation
invalidates the lease. A retry is explicit and never replays a tool action.

Completion inserts a bounded structured artifact and its receipt in the same
SQLite transaction as the completed state. A repeated completion from the same
lease returns the same receipt only for the same payload. A crash before commit
leaves neither artifact nor receipt; after commit both exist. This guarantee is
limited to the database artifact. External files, network actions and vault
writes cannot be made exactly once by this primitive and must use the existing
authorization/approval path plus a separately designed idempotent effect
protocol. Leases assume one local database and a reasonably consistent host
clock. No claim of mid-generation preemption or cross-device database sharing
is made.

## Operations and rollback

On zum, the existing launcher is `python launcher.py` in the repository; the
app listens on `127.0.0.1:7011`. Check authenticated `/api/ready` for
`checks.tool_index` and `/api/health` for liveness. Semantic index degradation
does not grant tools and catalog discovery remains available. The indexer
rejects vault-contained, Syncthing-managed and Git-contained cache directories;
its SQLite database and WAL/SHM stay beside that external cache.

To disable Phase 0 job activity, do not construct `JobStore` or call its methods;
no worker is enabled by this change. The ledger reader is read-only. Preserve
any job database and ledger before rollback. The remote file backup made during
installation is under the user's `.mabel-deploy-backups` directory. Restore
`src/agent_runtime/tool_catalog.py` from that backup and remove the newly added
Phase 0 modules/tests only after preserving any generated state. Restart the
existing launcher to load restored code. Do not delete the application database.
