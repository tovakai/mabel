# Workspace inspection and agent tool recovery

Implemented on `fix/reliable-agent-tool-runtime`, based on
`fix/batch-apply-authorization-and-workspace-diagnostics`. No session, settings,
database, or vault migration is required.

**Multi-device follow-on:** filesystem binding now also intersects
Settings → Devices → Allowed Directories with per-conversation Active
Directories. See [multi-device-filesystem.md](multi-device-filesystem.md).
Legacy single-workspace FormData does not mint grants; shell/Python use an
explicit primary local directory only.

## Findings and production path

The reported Windows/model sessions were not available as recordings, so their
exact historic context cannot be reconstructed. The wording-dependent admission
failure was reproduced with the production interpreter: “look around my movie
library”, “Explore Library/Movies”, and “check my movie collection for metadata
inconsistencies” received only `ask_user`/`update_plan`; “inspect the Markdown
files” received the whole `shell_files` family, including code execution and
writes. This is a deterministic classification problem, independent of model
parameters. Explicitly naming filesystem vocabulary happened to select a different
family. A selected workspace alone did not fix this.

The request path is:

1. `routes/chat_routes.py` resolves the session, workspace, user toggles, owner
   restrictions, and trusted request text. Its native-terminal and compact-model
   runtime decisions are separate from resource admission.
2. `requested_capabilities()` selects a family; `selected_tools_for_request()`
   can narrow explicit operations. `interpret_request()` filters history to
   trusted user rows; model proposals and tool events cannot establish authority.
3. `request_authority_for_http()` / `create_request_authority()` seal grants,
   owner/session/workspace identity, and resource roots before schema selection.
   Internal tool HTTP requests do not become fresh user requests.
4. `resolve_turn_contract()` intersects relevant tools with schemas and policy.
   The compact route additionally intersects the preview inventory with that
   contract. Neither schema retrieval nor advertisement grants execution.
5. `stream_agent_loop()` preserves the bound authority through model rounds and
   model fallback; `stream_preview()` implements the alternate compact transport.
   Native calls pass through `function_call_to_tool_block()`; textual calls use
   the shared tool tags and parser.
6. `execute_tool_block()` normalizes the exact operation, checks request binding,
   grants, policy, contract, backend/resource identity, exact approval, and the
   external-context security gate. Native search operations bind a sealed search
   root. Child authority is intersected with its parent.
7. The dispatcher calls the registered native handler or a contained process.
   `containment.unavailable_tool_result()` now explicitly marks non-execution.
8. Both loops use the same bounded recovery state. A failed execution capability
   can lead to already authorized native readers; a denial is never converted
   into fallback permission. Existing completion/evidence and loop budgets still
   apply.

Previously, containment refusal produced error text and a containment object,
but no stable failure kind. Generic loop breakers bounded repeated failures,
while much of the active recovery logic focused on writes/artifacts and web
sources. A read-only model could finish immediately after a shell failure with
no runtime-owned check for viable native alternatives.

The existing batch tool was a preview/apply mutation tool; its validator requires
an edit specification. It was not a read-only boolean metadata inventory.

## Changes

- `src/turn_contract.py`: introduces `workspace_read`, with six native tools.
  Inspection of a bound filesystem collection uses this family without code or
  write grants. Exact, single-tool user instructions retain their narrow
  contracts. Media, remote-source, and write workflows retain existing routing.
  Trusted inspection continuations preserve prohibitions, including when history
  already contains the current “Continue” row.
- `src/agent_runtime/authority.py`: composes caller policy with user prohibitions;
  retains inspection restrictions across continuations. A same-ID intersection
  that loses the child's resource roots is treated as inherited, preventing
  exact approval from renewing a replaced root. The existing replaced-root
  regression exposed this security issue during validation.
- `src/tool_policy.py`: tool-name prohibitions such as “without Bash or Python”
  are enforced in policy rather than left to model compliance.
- `src/agent_runtime/tool_recovery.py`: normalizes structured failure information
  and maintains two bounded replanning opportunities per invocation. Failed
  capabilities are suppressed for the rest of that invocation. Only explicit
  pre-execution unavailability, missing dependencies, and unavailable containment
  qualify. Denials and uncertain execution do not trigger automatic substitution.
- `src/tool_execution.py`, `src/containment.py`: normalize all dispatcher exits,
  including pre-execution denials, preserving legacy `failure_kind` fields while
  adding a `failure` object with category, execution certainty, and recovery
  guidance. Malformed arguments retain the legacy fail-closed admission result
  and additionally identify `invalid_arguments`.
- `src/agent_loop.py`, `src/clean_agent_preview.py`, `routes/chat_routes.py`:
  preserve the native read surface across transports, suppress unavailable tools,
  and replan if a model stops after a recoverable capability failure. The compact
  runtime accepts the explicit read family without enabling the native-terminal
  write/execute surface.
- `src/agent_tools/vault_batch_tools.py`: adds a separate `vault_audit` handler,
  reusing bounded Markdown enumeration and workspace admission. Enumeration is
  capped before sorting. YAML is composed into nodes without constructing
  arbitrary Python objects; only literal boolean values count as true/false.
- `src/agent_runtime/resource_binding.py`, `effect_adapters.py`: bind the audit's
  directory and descendants through the native resource adapter and classify its
  evidence as read-only. A complete glob audit does not claim full observation
  of every possible resource under a directory.
- Tool registry, tags, schema, index, security lists, and capability metadata are
  updated together in `src/agent_tools/__init__.py`, `tool_types.py`,
  `tool_schemas.py`, `tool_index.py`, `tool_security.py`, and `tool_capabilities.py`.
  `requirements.txt` declares PyYAML explicitly.
- `tests/test_workspace_inspection_runtime.py`: prompt equivalence, model-independent
  native schemas, policy and resource denials, continuations, bulk exact counts,
  no mutations, bounded recovery, report pagination, and real agent/compact
  sequences using mock provider responses and production dispatch.

## Audit contract and limits

Example native arguments:

```json
{"path":"Library/Movies","glob":"**/*.md","property":"owned"}
```

The audit returns discovered/inspected totals and separate true, false, missing,
invalid, and uninspected counts. Invalid YAML counts as inspected but invalid;
I/O/encoding/size failures count as uninspected. Missing or false ownership is
not inherently an error. Duplicate top-level keys, merge keys, strings, numeric
values, lists, and nonmapping frontmatter require review instead of coercion.

Bounds: 1,000 candidates, 2 MiB per file, 32 MiB per invocation, 64 KiB of
frontmatter per file. Symlinks and hardlinks are refused. Opened file identity
is checked before reading. Oversized inventories fail explicitly instead of
claiming complete coverage. Audits perform no writes.

Model-facing detail pages have a bounded size and `next_offset`. Repeat with
`report_offset` until it is null to list all paths. Every page retains full audit
counts; report pagination is distinct from inspection coverage. Pages now use the bounded receipt snapshot described in the final-synthesis
follow-up below. A fresh audit is not a transactional snapshot of concurrent
external edits. Full structured results remain in the
tool result; model text avoids duplicating that payload or silently truncating it.

## Diagnostics

The existing `turn_contract` event includes semantic capabilities, advertised
and unavailable tools, authorized tool names, and denied tool names. Existing
`tool_routing_audit` events retain retrieval/selection/advertisement stages.
`tool_output` includes normalized failure metadata; `tool_recovery` reports the
bounded attempt number, unavailable tools, and authorized alternatives.
Existing completion/evidence events retain the final runtime decision. No raw
history, credentials, scripts, or resource-root dumps were added to diagnostics.

## Validation and local use

Run from the checkout with its existing virtual environment:

```bash
cd /home/tovakai/mabel
.venv/bin/pip install 'PyYAML>=6.0.2'
.venv/bin/python -m pytest -q tests/test_workspace_inspection_runtime.py
.venv/bin/python -m pytest -q tests/test_request_authority.py tests/test_turn_contract.py tests/test_resource_identity.py tests/test_batch_preview_continuation.py tests/test_runtime_resource_integration.py tests/test_containment_contract.py tests/test_native_execution_containment.py tests/test_containment_enforcement.py
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7011
```

Use a disposable workspace, select Agent mode and a native-tool-capable model,
then request a movie metadata audit in natural language. Check coverage counts
and follow detail pagination when producing a full list. Repeat with a different
model while keeping the workspace and tool settings the same. Existing explicit
model tool-transport preferences still apply; model size does not confer grants.

Linux containment remains enforcing. Windows still cannot execute a shell when
its required filesystem/process containment is unavailable; native filesystem
inspection does not depend on shell containment. Windows behavior is simulated
in tests here; actual Windows and live Gyro-S/Dottie inference require separate
validation. Unknown or ambiguous natural-language requests remain conservative;
this is deterministic interpretation, not universal natural-language understanding.

Two broader tests fail identically using temporary copies of the original branch's
runtime/interpreter/authority modules: the compact tool-sample inventory omits the
already-existing `vault_batch_edit`, and a policy test expects the word “forbade”
while the dispatcher returns “forbidden”. These assertion failures are not hidden
by changing authorization or approval behavior.

Final combined result: **2,055 passed, 2 deselected, 2 warnings** in 67.71 seconds.
The two deselections are precisely the baseline failures described above. The
full application suite was not run. Compilation of changed runtime modules and
`git diff --check` passed.

Exact combined regression command:

```bash
.venv/bin/python -m pytest -q \
  tests/test_workspace_inspection_runtime.py \
  tests/test_request_authority.py tests/test_turn_contract.py \
  tests/test_vault_batch_tools.py tests/test_containment_contract.py \
  tests/test_resource_identity.py tests/test_batch_preview_continuation.py \
  tests/test_runtime_resource_integration.py tests/test_effect_resource_bindings.py \
  tests/test_wave3_diagnostics.py tests/test_clean_agent_preview.py \
  tests/test_tool_policy.py tests/test_agent_turn_contract_boundaries.py \
  tests/test_turn_contract_integration.py tests/test_product_turn_contract_route.py \
  tests/test_chat_route_tool_policy.py tests/test_native_execution_containment.py \
  tests/test_containment_enforcement.py \
  -k 'not test_every_compactly_offered_preview_tool_has_valid_policy_permitted_call and not test_executor_policy_backstop_blocks_tools' \
  --disable-warnings --tb=short --maxfail=4
```

## Follow-up: property audit completion integrity

The original tests proved that a model could invoke the audit tool and receive
its result. They did not prove that a model which skipped that tool could not
fabricate a report. The new adversarial production-path reproduction ran a
Qwen-style `ls` call followed by the false “121 Markdown files, zero owned, all
missing” answer. Both ordinary and compact runtimes originally returned
`status=unverified` with **`can_complete=true`**. The presentation gate checked
artifact/test execution claims, not property-audit coverage; numeric property
summaries therefore passed unchanged. Coincidentally correct counts were also
unsupported, regardless of their numerical accuracy.

`vault_audit` was registered, authorized, advertised, executed, and returned to
the model in both tested paths when selected. A separate routing conflict was
also reproduced: “which notes” inside a filesystem collection request could
seal a personal `manage_notes` list operation, erasing the native read inventory
from its contract. Typed workspace property audits now take precedence over
that incidental personal-record interpretation. Explicit personal-tool requests
retain their existing route.

Listings had two independent observation limits: an entry cap and the dispatcher
text cap. Long movie titles can cut a listing before the entry cap. Compact
transport adds its own observation budget. Neither filenames nor file sizes
establish frontmatter values. The exact historical Dottie prompt, advertised
payload, and full trace were not supplied, so the precise origin of its number
121 cannot be established here. No historical backup-vault answer is considered
verified merely because its counts looked plausible.

### Implementation

- `src/agent_runtime/structured_audits.py` defines a scoped boolean-property
  obligation and validates native report structure, unique file inventory,
  category partitions, coverage, and matching directory/glob/property. Only the
  native producer can record the current run's attestation. Model prose, prior
  answers, listings, tool-output-shaped JSON, and forged completion events do
  not attest to an audit.
- `src/agent_runtime/completion.py` extends the existing presentation gate.
  Unsupported numerical narratives are replaced by a deterministic report from
  validated native facts. Missing or partial coverage sets `can_complete=false`.
  If the model stopped after listing, the gate can perform one authorized native
  read through the production dispatcher, without another model invocation.
  It does not retry a failed audit or grant capabilities. Unresolved scope or
  unavailable permissions produce a truthful incomplete report and follow-up.
- `src/turn_contract.py` and `src/agent_runtime/authority.py` consistently select
  workspace readers for property-count requests. A resolvable audit selects
  `vault_audit` as required when that tool is offered; absent schemas and policy
  denials remain effective. Personal Notes exact-read inference cannot replace
  an audit of filesystem notes.
- `src/agent_loop.py` orders the dedicated audit schema first for audit tasks.
  `src/clean_agent_preview.py` uses the same completion gate, including directly
  invoked compact audits, while nested transport shares its parent's evidence.
- `src/agent_tools/vault_batch_tools.py` publishes validated native evidence and
  adds versioned coverage, separate malformed/unreadable categories and errors.
  `src/agent_runtime/tool_recovery.py` records audit failures without converting
  success-shaped transport output into native attestation.
- `src/agent_tools/filesystem_tools.py`, `src/tool_execution.py`, and both model
  transports expose listing metadata ahead of truncatable text: entries returned,
  truncation, zero files/property values inspected, and no property coverage.
  A byte-truncated listing does not claim an exact returned entry count.
- The saved compact trace removes discarded assistant reports. Late native audit
  calls and results are retained in both metrics/tool events and follow-up tool
  protocol, followed by the grounded terminal report.
- `src/tool_schemas.py` describes the structured audit result. New regressions
  live in `tests/test_structured_audit_completion.py`.

### Report semantics

`schema_version=1`, `complete`, `discovered`, `inspected`, `property_counts`,
`errors`, and bounded/paginated `entries` are machine-readable. Full producer
results also retain the complete file classification inventory.

The six `property_counts` categories are disjoint:

| Category | Meaning |
| --- | --- |
| `true` / `false` | Literal YAML boolean values for the requested key |
| `missing` | Readable note without that key; optional reference metadata, not an error |
| `invalid` | Key exists with a nonboolean value, including quoted booleans and lists |
| `malformed` | Frontmatter cannot be reliably interpreted, including syntax errors, duplicate keys, nonmapping roots and merge keys |
| `unreadable` | File could not be read/decoded/admitted within the audit limits; property value unknown |

Their sum equals `discovered`; `inspected = discovered - unreadable`. Inspection
means the note was read and classified; malformed frontmatter is an explicit
classification, not a missing property. `complete` means the whole selection was
inspected, not that every property value was valid. Partial results retain all
known counts but cannot verify a full-collection report.

For compatibility, legacy `counts.invalid` remains `invalid + malformed`, and
`counts.uninspected` aliases `unreadable`. Errors contain invalid/malformed/read
issues, never optional missing or false values. Filename-detail pagination is
separate from audit coverage. No file writes or settings/session migrations are
introduced.

### Follow-up validation

The broad run passed **2,212 tests**, with the same two documented baseline
assertions deselected (111.83 seconds). After final inventory-consistency and
direct-compact checks, the audit-specific suite passed **45 tests** (36.15
seconds). Compilation and `git diff --check` passed. The full application suite
was not run.

```bash
cd /home/tovakai/mabel
.venv/bin/python -m pytest -q tests/test_structured_audit_completion.py
.venv/bin/python -m pytest -q tests/test_workspace_inspection_runtime.py tests/test_completion_boundary.py tests/test_code_nav_tools.py tests/test_request_authority.py tests/test_resource_identity.py
```

Tests use 208-note disposable collections, mixed values, unreadable notes,
long-title byte-truncated listings, capped globs, denied/unadvertised capabilities,
native tool failures, incorrect scope/property/glob, contradictory summaries,
coincidentally correct unsupported counts, forged success-shaped results, direct
compact calls, and saved follow-up traces. Model-selected native audits and the
bounded native recovery both produce grounded reports through real dispatch.
There is no additional model call for deterministic report recovery.

These are mock-provider production-runtime tests, not live Dottie/Gyro-S
inference. A genuine live model-driven audit has **not** been run in this session.
No real vault was read or modified for validation. The scoped contract supports
one boolean property at a time; ambiguous multi-property requests ask for a
specific property. It does not attempt to verify arbitrary numerical prose.
Audits remain bounded and are not transactional snapshots of concurrent external
vault edits.

## Follow-up: final synthesis after successful audits

A completed audit is not sufficient if the user receives an irrelevant listing.
The 173-note regression is reproduced with mock Qwen-native calls through both
production runtimes: 173 discovered/inspected, 159 true, 0 false, 14 missing, no
invalid/malformed/unreadable values, and zero audit writes. Tests exercise a raw
listing, empty response, contradictory totals, and a second detail request at
`report_offset=104`.

The exact live prompt, raw model frames and persisted session were not supplied;
therefore the original listing cannot be conclusively attributed to a particular
model frame or UI replay. Source inspection shows that `chat.js` renders tool
output in a separate expandable tool area, and `_AgentRenderState` treats final
responses as replacement snapshots. The following backend defects were confirmed:

- Completion inference could miss a property audit even after a successfully
  authorized native audit. That left model prose outside the audit-specific gate.
  A reproduction uses “Explore ... and examine owned: true ...”, which previously
  authorized filesystem readers but did not establish a synthesis obligation.
- `Library/Movies:` was parsed as a directory whose name ended with a colon.
  Sentence punctuation now terminates that directory selector.
- Each tool observation could contain roughly 6 KB of filename details, repeatedly
  competing with aggregates in a small model's context. Pagination rescanned and
  reparsed the files rather than serving the initial audit's snapshot.
- The earlier audit gate always replaced prose with a fixed report. It did not
  distinguish a useful natural summary from an empty, unrelated or incomplete one.

### Current behavior

1. The native producer publishes a run-owned receipt observation. A successful
   scoped native operation can tighten completion requirements even if initial
   wording inference missed the audit. This supplies no execution permission:
   the operation must already have passed normal request, policy and resource
   admission. Transport JSON cannot manufacture this evidence.
2. `vault_audit` returns compact aggregates by default, with at most three bounded
   sample entries and an opaque `audit_id`. Full records remain available in the
   receipt and explicit detail pages. Normal detail pages use a 1,500-character
   entry budget, retain aggregate counts, and expose their next offset.
3. A bounded server-memory receipt cache reuses the original snapshot for page
   requests. No Markdown enumeration, file reading or YAML parsing occurs on a
   cache hit. It retains at most 16 receipts/16 MiB for ten minutes. Every page
   still passes the ordinary dispatcher and must match owner, session, workspace,
   selection and sealed directory identity. A receipt cannot grant access.
   Cross-request snapshot reuse requires an explicit receipt and a bound owner
   and session; stale, changed-scope or unavailable receipts fail explicitly.
4. The scoped synthesis validator checks the requested directory, discovery and
   inspection totals, true/false/missing counts, coverage, actual issues, and
   filename references when requested. It rejects raw listings, empty answers,
   unsupported/contradictory counts and missing findings. It validates only the
   structured audit operation; it does not police arbitrary numerical prose.
   Existing generic guards against invented test/execution claims remain active
   for both the original and regenerated prose.
5. A useful validated natural answer is retained. Otherwise there is one bounded,
   tool-free regeneration request to the same selected model, using only the
   original request and compact verified evidence. It excludes the paginated
   context tail and is limited to 768 output tokens and twenty seconds. If the
   replacement cannot be validated, a readable deterministic receipt report wins.
   Partial or unsupported audits retain their incomplete-report behavior.
6. Aggregate-only requests do not receive a directory dump. Filename requests get
   real matching references, a clearly bounded sample and the paginated receipt.
   Missing/false optional properties remain valid reference-library metadata.
7. The final event explicitly replaces the whole turn. Saved round text and the
   compact session trace contain the accepted or deterministic report, preserving
   native call/result protocol. An earlier listing draft cannot become the saved
   final answer. `audit_synthesis` diagnostics identify the initial problem,
   regeneration attempt, chosen source and initial answer hash/length without
   saving a giant context dump. Additional generation is counted separately.

The schema/index/agent instructions and `GetWorkspaceTool` output now state that
code execution requires enforced containment and is unavailable if the required
isolation cannot be established. The stale “not sandboxed” claim was removed;
containment enforcement itself was not changed.

This supersedes the earlier rescanning-pagination description: pagination now
uses an immutable snapshot, not a fresh audit of concurrent file edits. Start a
new audit without an old receipt to obtain fresh counts. The default observation
is now aggregate evidence; clients wanting filename pages must explicitly supply
`report_offset` (starting at zero), and `audit_id` for a later request.

### Files and verification

- `src/agent_runtime/audit_receipts.py`: bounded, resource-bound snapshot storage.
- `src/agent_runtime/structured_audits.py`: native observations, directory parsing,
  scoped answer validation, compact synthesis evidence and readable fallback.
- `src/agent_runtime/completion.py`: one bounded regeneration, native-receipt
  completion adoption, canonical final event, diagnostics and saved trace.
- `src/agent_tools/vault_batch_tools.py`: compact observations and cached detail
  pagination; full native report remains available without file mutations.
- `src/agent_tools/filesystem_tools.py`, `src/agent_loop.py`, `src/tool_index.py`,
  `src/tool_schemas.py`: accurate containment wording and updated tool contract.
- `tests/test_audit_final_synthesis.py`: 173-note regressions across ordinary and
  compact paths, including missed intent, filename requests and cache isolation.
- `tests/test_structured_audit_completion.py`: mock provider supports pagination
  and the explicit synthesis request.
- `tests/turnRendering.test.mjs`: Chromium verifies that the canonical audit report
  replaces listing drafts while retaining the separate raw tool evidence.

Verification: **2,234 passed, 2 deselected, 2 warnings** in the broader Python run
(133.67 seconds). The two exclusions are the previously reproduced baseline
assertion failures. A final focused run passed **79 tests**, including the
Chromium rendering suite, synthesis regressions and existing completion-boundary
protections (24.37 seconds). Changed
runtime modules compile and `git diff --check` passes. The full application suite
and live Dottie inference were not run. All vault fixtures were disposable.

```bash
cd /home/tovakai/mabel
.venv/bin/python -m pytest -q tests/test_audit_final_synthesis.py tests/test_structured_audit_completion.py tests/test_workspace_inspection_runtime.py tests/test_completion_boundary.py
npm ci --ignore-scripts
node_modules/.bin/playwright install chromium
.venv/bin/python -m pytest -q tests/test_turn_rendering_js.py tests/test_audit_final_synthesis.py tests/test_completion_boundary.py
```

Restart the Mabel server before live validation so it loads the updated runtime.
No session/settings migration or real-vault change is required. Receipt snapshots
are bounded, temporary data; server restart invalidates them. Conservative answer
validation can fall back for an unusual but correct phrasing, while ordinary
non-audit conversations retain their existing behavior.

## Follow-up: canonical collection scope and receipt continuation

The production-dispatch reproductions identified three independent causes:

- `Path.glob` included `.stversions` when a model searched from the workspace
  with `**/Library/Movies/*.md`. The completion contract rejected the scope after
  execution, but the native executor did not enforce that same selection before
  scanning. Any failed audit attempt also suppressed the original missing-evidence
  fallback, so the agent could not correct the semantic mistake.
- Pagination bound omitted `path`/`glob` fields to workspace defaults before
  looking up a receipt. That disagreed with the receipt's sealed collection
  directory even for a valid identifier. Expired, unknown and mismatched receipts
  shared one generic error, and expiry cleanup erased the distinction entirely.
- A model could invent `audit_id: "initial"`; no server-side continuation default
  restored the real snapshot or guided the bounded correction.

The scoped audit state now resolves an explicitly named directory once, including
quoted directory names, and seals its object identity. Both executor and completion
contract check that same directory, relative matching pattern, property and
control-directory policy. A workspace-root search for a named subdirectory is
rejected **before reading notes**; plausible counts cannot substitute for the
requested selection. Directory replacement fails closed. Workspace resource
binding and exact approvals still run independently on every operation.

One shared discovery policy prunes conventional control/history directories before
traversal: `.stversions`, `.stfolder`, `.git`, `.hg`, `.svn`, `.obsidian`, `.trash`,
`.cache`, `__pycache__`, `node_modules`, `.venv`. Ordinary code-navigation discovery
uses the same exclusions. This is an exact-name policy, not a blanket hidden-name
filter: unusual names such as `.my-legitimate-notes` remain active content.
Trusted, explicit requests can enable `include_control_dirs`; a model setting that
flag cannot expand the requested audit. Resource policy continues to apply.
Recursive matching keeps `*` within one component and `**` recursive; traversal
errors fail the inventory rather than certifying silently skipped directories.

Receipt continuation restores **omitted** selectors from an accessible snapshot
before resource binding. Supplied selectors remain intact and must match. Owner,
session, workspace, original selection and sealed directory identity remain
mandatory. An omitted identifier can use a unique snapshot from the current
request, filtered by the canonical audit target. Page zero can start an audit;
later pages never silently scan a new collection. The snapshot's aggregate counts
remain invariant, and normal pagination neither rereads nor recounts files.

Actionable failure codes are `audit_receipt_invalid_id`, `audit_receipt_expired`,
`audit_receipt_missing`, `audit_receipt_scope_mismatch`, and
`audit_scope_mismatch`. Foreign identifiers are indistinguishable from missing
snapshots. Small, bounded expiry tombstones retain only principal metadata, never
file records. Existing ten-minute, sixteen-snapshot, sixteen-MiB bounds remain.
A server restart or cache eviction can still remove a snapshot; that is reported
as missing rather than expiration.

The completion boundary can make **one** corrected native operation for semantic
scope/argument/receipt mistakes. It uses the original canonical target and the
same production dispatcher, inherited authority, policy, security context and
contract. If a valid snapshot is already recorded, correction reuses its actual
identifier and requested offset. Missing/expired snapshots permit one fresh
correctly scoped audit. Resource, authority, contract, policy, approval and
uncertain-side-effect denials do not enable this correction. Failed correction
retains a truthful blocker; there is no unbounded retry loop. Diagnostics record
`correction_reason`, `correction_used` and the bounded `audit_recovery` event.

`tests/test_audit_scope_receipts.py` exercises ordinary and compact production
paths with 173 active notes (159 true, 14 missing), nested directories, a similarly
named collection elsewhere, and two Syncthing historical copies. It covers broad
scope correction, invented/expired/missing/mismatched receipts, omitted pagination
fields, snapshot reuse without rescanning, explicit history requests, general
quoted target names, and security denials. All fixtures are disposable; no real
vault, settings or session history is changed. No session migration is required.
Restart the server to load the repair; old in-memory receipts are transient.

```bash
cd /home/tovakai/mabel
.venv/bin/python -m pytest -q tests/test_audit_scope_receipts.py tests/test_audit_final_synthesis.py tests/test_structured_audit_completion.py tests/test_workspace_inspection_runtime.py tests/test_vault_batch_tools.py tests/test_code_nav_tools.py tests/test_completion_boundary.py
```

Files changed for this follow-up:

- `structured_audits.py` and `completion.py`: canonical target/identity, common
  selection check, one semantic correction and traceable correction state.
- `audit_receipts.py`, `resource_binding.py`, `tool_execution.py`: restore omitted
  selectors before binding, preserve supplied selectors and exact approvals,
  distinguish receipt failures while protecting foreign receipt existence.
- `workspace_discovery.py`, `vault_batch_tools.py`, `filesystem_tools.py`: shared
  control-directory exclusions, component-wise recursive matching, fail-closed
  inventory errors, and executor-side scope enforcement. Batch preview inventory
  shares the active-content exclusion policy; regenerate an older preview if it
  previously included control/history files.
- `tool_schemas.py`: describe canonical collection roots, history opt-in and real
  receipt identifiers. `test_structured_audit_completion.py` adds multi-call mock
  provider support and expects safe correction of semantic selection mistakes.
- `test_audit_scope_receipts.py`: production-dispatch and both-runtime coverage;
  this document records behavior and operational limitations.

Verification for this follow-up: the broader runtime/authorization/containment/
browser run passed **2,238 tests, 2 deselected, 2 warnings** (159.04 seconds).
The two exclusions remain the previously documented baseline assertions
`test_every_compactly_offered_preview_tool_has_valid_policy_permitted_call` and
`test_executor_policy_backstop_blocks_tools`. A preceding focused run passed
**204 tests**. Changed runtime modules compile and `git diff --check` passes.
The full application suite, live Dottie endpoint and native Windows execution were
not run; Windows capability differences are covered by the containment mocks in
the broader suite. A genuine live model-driven audit remains to be validated after
server restart; the production pipeline tests use Qwen-style mock tool calls.

The final scope/receipt run passed **42 tests, 1 warning** (33.49 seconds),
including the additional omitted-pagination and failed-correction cases added
after the broader run. No new test failures remain in the executed selections.

## Follow-up: completed-task evidence across conversational turns

### Reproduced causes

The original request interpreter inherited workspace inspection only for literal
continuations such as “continue” or “try again”. A question such as “Can you tell
me which ones that I don't own?” therefore constructed a new authority without
`vault_audit`, despite the previous native snapshot still existing. The schema
selection likewise omitted the reader. Requiring a model to recover an opaque
receipt from conversational history then compounded the failure; the invented
identifier was never evidence or permission. A later provider failure left the
user without a result the server already possessed.

The new production-path reproduction first completed an audit, then made exactly
that follow-up through the ordinary and compact agents. Before the repair it
failed: the native call was dropped as unoffered and the model/provider response
became the only possible answer. Tests now run whole multi-turn conversations,
including a real registered HTTP route's authority and schema construction.

### State, language understanding and permission are separate

`src/agent_runtime/task_artifacts.py` is a reusable, server-owned registry of
completed task artifacts. Only native producer code publishes records. Each has
a generated trusted identifier, kind, title, structured result or snapshot
reference, principal (owner/session/workspace), lifetime, and optional sealed
filesystem resource. Chat text, saved assistant prose, model-generated identifiers
and imported message metadata cannot publish records. Small results can be
inline; larger producers should reference their existing snapshot storage. The
registry does not create a parallel execution or approval framework.

The native audit producer publishes a `markdown_property_audit` reference. The
registry itself has no frontmatter parsing or audit-specific inference. A test
also publishes a synthetic `build_verification` result and retrieves it through
the same production tool path. Inline, non-filesystem results also work in
sessions without a selected workspace; resource-backed results require one.
Other producers must explicitly publish verified results before they become
available; arbitrary historical assistant claims are not retroactively trusted.

`task_results` is the model-facing read operation. It accepts optional kind/title
query selectors, classification and offset, rather than opaque IDs, new paths,
or replacement results. The model interprets conversational references and can
request the latest relevant task or a unique matching title. The server selects
the real record, validates its principal and resource, and retrieves it through
the appropriate adapter. Missing or ambiguous matches fail explicitly. The chat
route offers this reader when trusted task state exists, and new request authority
permits saved-result reads without inheriting unrelated filesystem, shell or
write permissions. Schema advertisement itself still grants nothing. Normal
policy, backend admission, owner/session binding and inherited-authority
intersection remain active.

The audit adapter checks the original sealed directory against current workspace
roots and current source-tool denials before releasing snapshot data. It supports
filtered, bounded pages: `true`, `false`, `missing`, `invalid` (including malformed
frontmatter), `uninspected`, and `not_true` (false plus missing). Aggregate counts
always describe the whole collection; `matching` and offsets describe only the
selected classification. Invalid values remain a separate uncertainty category.

There is still a small deterministic shortcut for obvious audit questions
(counts, missing files, “those 14”, unmarked ownership, invalid files, next page).
It is an optimization and outage path, not the registry's retrieval or permission
model. Differently worded requests are interpreted by the LLM through
`task_results`; tests explicitly exercise that path. If the most recent completed
task has another kind, the shortcut leaves interpretation to the model. This
repair does not depend on expanding that phrase matcher to every conversational
variation.

### Freshness, recovery and final output

Audit snapshots retain bounded internal file and directory identity/size/mtime/
ctime metadata, excluded from model observations. A follow-up checks metadata
rather than rereading YAML or regenerating counts. Unchanged snapshots supply the
same canonical per-file evidence and identifier. New files, removed files and
ordinary edits invalidate current evidence. This is metadata change detection,
not a content rehash on every question. Normal explicit snapshot pagination
retains the existing immutable-snapshot behavior.

The obvious-question shortcut receives a fresh authority containing only the
exact canonical cached-read and fresh-audit operations, plus saved-result reads.
An expired, evicted or changed snapshot permits one fresh audit through the normal
dispatcher when that exact operation remains permitted. Resource replacement,
source policy denial, approval requirements or authority denial never trigger a
refresh. The general `task_results` operation itself cannot scan files: stale
results return a specific blocker unless a separately authorized read operation
can refresh them. It cannot turn model-chosen selectors into new filesystem
permission.

Successful deterministic answers include actual filenames, counts, complete
coverage and optional-property semantics: missing `owned` means **unmarked**, not
proof of physical non-ownership. They never contact the LLM. Model-driven saved
result retrieval preserves conversational interpretation. If its provider then
returns HTTP 502, another HTTP failure, a timeout or an empty answer, the
completion boundary renders the native retrieved result rather than losing it.
The exception recovery applies only after successful native retrieval and never
catches cancellation or unrelated programming errors. Provider failure before
retrieval does not create evidence. Saved canonical traces and tool-event
metadata retain the actual operations and readable final response.

### Files, limits and verification

- New `task_artifacts.py`: reusable bounded producer-owned session registry and
  native retrieval observations.
- New `agent_tools/task_result_tools.py`: model-driven scoped result retrieval,
  with an audit snapshot adapter and inline structured-result support.
- New `audit_followups.py`: narrowly scoped deterministic shortcut, exact read
  plans and readable reports.
- `audit_receipts.py`, `vault_batch_tools.py`: publish trusted result references,
  retain private freshness metadata and supply classification-filtered pages.
- `authority.py`, `turn_contract.py`, `chat_routes.py`: fresh narrow authorization
  and task-reader advertisement, independently of model-generated IDs.
- `completion.py`: deterministic follow-ups, saved native traces and evidence-backed
  recovery after provider failure.
- Tool registration, schemas, index, capability metadata and compact tool inventory
  include the new read-only operation.
- `tests/test_audit_followups.py`: complete conversations, model-driven retrieval,
  provider 502, general result kinds, pagination, expiry, changed files, replacement,
  foreign principals, policy denial, exact grants and the real HTTP route.

The registry is process-local and bounded to 64 artifacts/4 MiB, with a one-hour
reference lifetime and a 128 KiB inline-result limit. Native audit snapshots retain
their ten-minute and sixteen-receipt/sixteen-MiB bounds, including private freshness
metadata. Restart/worker changes can remove this temporary state; there is no new
disk persistence, migration or cross-worker cache replication. Missing state is
reported honestly. Server restart is required to load this repair; existing
settings and session history do not need migration. No real vault was used.

Broader validation: **2,277 passed, 2 deselected, 2 warnings** (198.50 seconds).
The exclusions are the same previously reproduced baseline tests named above.
The focused conversation/scope run passed **75 tests** (71.91 seconds). A final
focused run additionally covers inline results without a workspace. Full
application tests, live Dottie inference and native Windows execution were not run.

```bash
cd /home/tovakai/mabel
.venv/bin/python -m pytest -q tests/test_audit_followups.py tests/test_audit_scope_receipts.py
.venv/bin/python -m pytest -q tests/test_audit_final_synthesis.py tests/test_structured_audit_completion.py tests/test_workspace_inspection_runtime.py tests/test_completion_boundary.py
```

Final focused validation passed **77 tests, 1 warning** (70.83 seconds), including
non-filesystem inline artifacts. Changed modules compile and `git diff --check`
passes. The compact inventory fixture now includes a valid `task_results` sample;
its previously documented unrelated inventory baseline mismatch remains excluded
from the broader run.

## Authorization and human review repair — 2026-10-28

This section supersedes earlier descriptions of English intent as the source of
automatic permissions for interactive Agent turns. Existing audits, receipts,
completion/synthesis checks, bounded recovery and reusable task artifacts remain.
All pre-existing uncommitted work was preserved. No harness replacement, session
migration or real-vault test was performed.

### Upstream findings and comparison

Reviewed upstream dev at
[57a1859f081f5877373e4aaf96435eb3ed513793](https://github.com/odysseus-dev/odysseus/commit/57a1859f081f5877373e4aaf96435eb3ed513793)
against Mabel HEAD ee5dd814652dbb057d908341916cd9bd275b4c0f. Their merge base is
9d0d3720f4ca181d87bef94da826d730acc6d483. The issue reports, substantive comments
and linked proposals were examined before implementing this repair.

- [#6631](https://github.com/odysseus-dev/odysseus/issues/6631): schemas/retrieval
  described available tools, while authority was independently reconstructed from
  lexical capability families. Custom MCP names do not belong to those built-in
  families, and referential requests can select none. Enabled, advertised tools
  could consequently fail admission without review. The issue remained open.
  [Open PR #6887](https://github.com/odysseus-dev/odysseus/pull/6887) adds exact
  approval for missing automatic grants. Its gap-versus-denial distinction,
  digest-bound card flag, taint handling and execution-result continuation were
  reused with adaptations. It was not merged in the inspected dev; its once-only
  path does not fix compact dispatch, discovery or scoped consent by itself.
- [#6635](https://github.com/odysseus-dev/odysseus/issues/6635): pending approvals
  did not always seal process launch identity outside original authority.
  Resource validation correctly rejected execution; approving text cannot
  reconstruct the identity. Fixes afecd44d (#6683, October 13) and f8e043e8
  (#6705, October 23) already exist in Mabel HEAD. They seal ungranted Bash/Python
  launches/default workspaces and close skill-test streams in their original
  task. They were retained. Further failures remained: compact dispatch skipped
  the approved-action prelude, and generator ContextVars spanning yields could
  still be closed from another ASGI task.
- [#6672](https://github.com/odysseus-dev/odysseus/issues/6672): embedding retrieval,
  deterministic capability/contract selection and request authority are three
  independent gates. Translating retrieval input alone cannot fix missing
  permission or a later English-only selector. Comments also report English
  follow-up failures. The issue remained open. This repair adds no translation
  phrase tables: trusted UI scopes supply positive automatic permissions and the
  model can discover tools and request exact human review.

Also reviewed [#6837](https://github.com/odysseus-dev/odysseus/pull/6837),
[#6636](https://github.com/odysseus-dev/odysseus/pull/6636) and
[#5819](https://github.com/odysseus-dev/odysseus/pull/5819): a narrower web-toggle
proposal, MCP continuation routing, and a much larger run-mode/sandbox redesign.
None was substituted wholesale for Mabel's existing machinery.

Latest dev security fix #6357 / commit 57a1859f prevents agents from removing
administrator tool denials through manage_settings.enable_tool, app_api, or
internal-token policy endpoints. This bounded fix and its upstream regressions
were backported. Human administrator settings controls still work. An agent,
even with an admin owner or an approval, cannot re-enable disabled tools.

Mabel's preceding work already fixed workspace read capability, canonical audit
scope, native fallback, receipt pagination, unsupported audit reports and task
artifact retrieval. It had not fixed generic authority-gap review, the HTTP
route's conversion of retrieval omissions into hard denials, or compact approval
and denial continuation. Investigation also found that same-request intersection
tested lost filesystem roots but omitted lost backend, owned-record, process and
browser ceilings. A same-ID child could then be mistaken for a root while sealing
approval. Every sealed ceiling now participates in this restriction check.

### Separation of interpretation, availability and permission

The model interprets conversation and proposes actions. Initial retrieval remains
a context-size optimization. A request-local discover_tools catalog offers bounded
descriptions and up to six additional schemas per call, with sixteen discovered
tools maximum. It supports the existing semantic index and paginated enumeration
when retrieval misses. It cannot grant execution permission.

The catalog contains the server's policy-filtered registered inventory. Current
policy and authority are independently rechecked by the dispatcher. Compact
contracts retain this catalog when their initial trained inventory is smaller.
Server-derived editor views preserve request-local discovery. An unavailable
initial lexical family does not prevent an interactive model from investigating
authorized alternatives.

Positive automatic grants in interactive human Agent turns no longer depend on
English intent classification. A selected, validated workspace declares read-only
workspace capability; enabled Web scopes declare their existing read capability;
trusted task state offers saved-result retrieval. Lexical selection cannot discard
these declarations. Private operations, writes and process launches without an
automatic grant require exact human review. Explicit tool/user/owner/server
denials remain restrictions, never approval candidates.

Delegated tokens, internal tool requests, children, background runs and plan mode
do not gain interactive escalation. Programmatic callers not opting into review
retain existing explicit authority behavior. A provider/model change does not
change the interactive permission profile. Full-schema providers retain their
existing enabled inventory; routed providers can discover alternatives.

Completed results still use the reusable task_artifacts registry and task_results
reader described above. The model supplies semantic kind/query and classification,
not a fabricated receipt. The server resolves actual identifiers and validates
owner, session, workspace and source permission. The audit phrase shortcut remains
an optional deterministic/outage optimization; it is not expanded by this repair
and is not the general continuity architecture.

### Approval lifecycle

Both runtimes reuse the existing pending store, exact approvals, dispatcher and
resource sealers:

1. Validate current policy, principal, workspace and root/child provenance.
2. Seal normalized input and applicable backend, filesystem, owned-record,
   process/job and browser identities.
3. Validate readiness before showing a card, including required process
   containment. Unavailable containment/backend produces an actionable failure,
   not an ineffective card or unsandboxed execution.
4. Show complete input, effects, affected resources, environment and expiry.
   External endpoint display uses credential-free origins.
5. Consume the authenticated human decision atomically. Invalid choices do not
   consume pending actions; owner/thread mismatches cannot retire them.
6. Execute the reviewed first action once, then return its actual result to the
   model. Restore original task, workspace and tool relevance without adding
   permission. Compact continuations use the same approved-action prelude.
7. On denial, persist the control decision, notify the frontend and give the model
   a trusted rejection observation. The exact operation stays denied in that
   task; authorized alternatives can continue without the original prompt again.

Authority-gap cards offer Allow once, Allow this exact operation for this task,
Allow this exact operation for this chat, and Deny. Scoped consent covers only
identical normalized inputs, backend and sealed resource identities. Other inputs
or resources need review. Every use is revalidated; process launches receive fresh
one-use reservations under the original ceiling. New external taint requires
renewed informed consent.

Task consent requires the original request ID. Chat consent requires the original
server-resolved card and opaque store proof in that same session. Deleting the
card, changing owner/workspace/resources, current policy denial, inheritance or
expiry prevents reuse. Neither choice becomes blanket authority or the legacy
chat-wide taint bypass. Older external-context approval semantics remain
compatible and still require pre-existing request authority.

Pending actions and new narrow scope grants are process-local, bounded and expire
after ten minutes. Disconnect/reconnect within the same live worker preserves
them. Restart, worker switching or expiry requires new proposal/review; saved UI
cards cannot reconstruct executable authority. Restart durability is not claimed.

Context-bound streams use a demand-driven producer task. Entry, iteration and
cleanup remain in that task if ASGI consumer tasks change. There is no prefetch or
execution after an unrequested yield. Cancellation closes nested streams before
resetting contexts. Bindings do not leak into the consumer. This is a small stream
ownership adapter, not another agent framework.

### Reference architectures

Inspected Codex's [orchestrator](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/orchestrator.rs),
[approvals](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/approvals.rs)
and [app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md),
and Agents SDK [RunState](https://github.com/openai/openai-agents-python/blob/main/src/agents/run_state.py)
and [runner](https://github.com/openai/openai-agents-python/blob/main/src/agents/run.py).
The applicable patterns separate proposal, review and sandbox execution, preserve
reviewed identity, feed denials/failures back to the model, and resume interrupted
tasks with server-owned state. Mabel already has corresponding core objects.
Repairing their seams is less disruptive than replacing the harness. Neither
reference justifies model-created permissions or approval bypassing containment.

### Changed areas

| Files | Reason |
| --- | --- |
| authority.py, turn_contract.py, agent_runtime/tool_catalog.py | UI-scoped grants, separate dynamic discovery, complete child-ceiling comparison, concise diagnostics |
| tool_approvals.py, tool_approval_scopes.py, tool_execution.py | Readiness checks, once and exact task/chat consent, validated decisions, denial memory |
| agent_loop.py, clean_agent_preview.py, routes/chat_routes.py | Both runtimes' review, HTTP relevance/policy separation, approved execution and denial replanning |
| agent_runtime/owned_stream.py | Stable ContextVar ownership and cancellation without prefetch |
| Tool registration/schema/capability metadata | Native discovery and accurate enforcing shell descriptions |
| Chat/skills renderers and module version URLs | Once option, resources, environment/expiry and consistent cached modules |
| admin_tools.py, tools/system.py, MCP/model policy routes | Upstream administrator-denial protection |
| Authorization, resource, route, stream, denylist and approval tests | Production-path regressions and assertions matching the new semantics |

The ordinary/compact HTTP demonstration uses the registered /api/chat_stream
endpoint, configured mock MCP manager and real dispatcher. A German request
discovers the service schema; remote call count remains zero until the server's
approval is answered. Allow once resumes and executes exactly read(record="one")
once. A replay cannot consume the approval. Other tests cover six languages,
discovery without grants, denial/native fallback, scoped consent, current-policy
changes, unavailable containment, foreign principals, interruption and retained
audit evidence after provider HTTP 502.

### Arch laptop validation and compatibility

Restart Mabel with existing environment/settings after reviewing the diff, then
hard-refresh the WebUI for consistent module versions. Usual development commands:

    cd /home/tovakai/mabel
    .venv/bin/python -m pytest -q tests/test_interactive_authorization_runtime.py tests/test_authority_gap_approval.py tests/test_owned_agent_stream.py tests/test_manage_settings_denylist_hard.py tests/test_exact_approval_ungranted_process.py tests/test_tool_approval_task_scope.py tests/test_remote_resource_identity.py tests/test_api_token_tool_authority.py
    .venv/bin/python -m pytest -q tests/test_audit_followups.py tests/test_audit_scope_receipts.py tests/test_audit_final_synthesis.py tests/test_structured_audit_completion.py tests/test_workspace_inspection_runtime.py tests/test_native_execution_containment.py tests/test_containment_enforcement.py
    .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 7011

Use a disposable workspace. In Agent mode inspect it in English and another
language with identical toggles, then request a write. Review the complete card,
allow once and verify one execution. Repeat with Deny and a permitted native
alternative. Audit the disposable movie fixture and ask which entries are
unmarked; the receipt should supply filenames without a rescan. Repeat with
Dottie and a larger model without changing policy/workspace. Check that disabled
tools stay unavailable even when the agent requests enabling them.

There is no trusted-host unsandboxed fallback. Linux containment remains enforcing;
Windows process limitations are simulated and fail closed. Native reads do not
depend on shell containment. External MCP isolation belongs to the backend.
Existing resource adapters have different coverage: calendar binds its backend
and retains handler ownership checks, rather than using document revision binding.

Existing approve / approve_task wire values remain; approve_once is additive.
Legacy approval history/signatures remain supported. Gap cards are digest-bound
and cannot become legacy broad bypasses. Initial schemas can differ by surface,
while authority does not. Interactive private/mutating requests formerly granted
by English keywords can now require review. No user data, settings or provider
configuration is rewritten. Live model inference, native Windows, multi-worker
restart and the full application-wide suite are not claimed.

Suggested upstream contributions: adapted #6887 gap/denial continuation; compact
approval parity and stable stream ownership; and discovery/permission separation
with multilingual HTTP regressions. The administrator-denial backport already
belongs upstream and should not be resubmitted as original work. No upstream
messages or PRs were sent.

### Final validation for this repair

The final combined run passed **2,896 tests, 2 deselected, 2 warnings** in
245.40 seconds. It includes the production HTTP approval demonstration in both
runtimes, the administrator-denial backport, six-language discovery/review,
ContextVar ownership and cancellation, API/child isolation, native containment,
browser rendering, and all preserved structured-audit and follow-up suites.

The preceding focused authorization/route/editor check passed **540 tests,
1 deselected, 2 warnings** in 42.71 seconds. A failing-before-fix production-loop
test demonstrated that the legacy single-tool shortcut attempted execution before
the model and could finish at authority denial. Interactive review now bypasses
those preemptive execution paths and English intent replacement. A final
English/Swedish browser test also verifies that click permission cannot be added
by an English keyword when interactive permissions are otherwise identical.

The broad run excludes only the two previously reproduced baseline assertions:
test_every_compactly_offered_preview_tool_has_valid_policy_permitted_call
(the older compact sample table omits vault_batch_edit) and
test_executor_policy_backstop_blocks_tools (expected "forbade", actual "forbidden").
These are retained as documented exclusions, not security exceptions. No new
failure was excluded. Changed Python modules compile (51 files) and git diff
--check passes. The full application-wide suite and live Dottie/Gyro-S inference
were not run.

The exact final broad selection, without output redirection, is:

    .venv/bin/python -m pytest -q \
      tests/test_owned_agent_stream.py tests/test_interactive_authorization_runtime.py \
      tests/test_authority_gap_approval.py tests/test_manage_settings_denylist_hard.py \
      tests/test_review_regressions.py tests/test_tool_approvals.py \
      tests/test_tool_approval_task_scope.py tests/test_tool_approval_single_action_scope.py \
      tests/test_exact_approval_ungranted_process.py tests/test_skill_test_stream_closed.py \
      tests/test_request_authority.py tests/test_remote_resource_identity.py \
      tests/test_external_context_tool_gate.py tests/test_api_token_tool_authority.py \
      tests/test_audit_followups.py tests/test_audit_scope_receipts.py \
      tests/test_audit_final_synthesis.py tests/test_structured_audit_completion.py \
      tests/test_workspace_inspection_runtime.py tests/test_turn_contract.py \
      tests/test_vault_batch_tools.py tests/test_containment_contract.py \
      tests/test_resource_identity.py tests/test_batch_preview_continuation.py \
      tests/test_runtime_resource_integration.py tests/test_effect_resource_bindings.py \
      tests/test_wave3_diagnostics.py tests/test_clean_agent_preview.py tests/test_tool_policy.py \
      tests/test_agent_turn_contract_boundaries.py tests/test_turn_contract_integration.py \
      tests/test_product_turn_contract_route.py tests/test_chat_route_tool_policy.py \
      tests/test_native_execution_containment.py tests/test_containment_enforcement.py \
      tests/test_code_nav_tools.py tests/test_completion_boundary.py \
      tests/test_turn_rendering_js.py tests/test_foreground_model_routing.py \
      tests/test_editor_writing_action_routing.py \
      -k 'not test_every_compactly_offered_preview_tool_has_valid_policy_permitted_call and not test_executor_policy_backstop_blocks_tools' \
      --disable-warnings --tb=short --maxfail=8

To run just the end-to-end mock human-review demonstration:

    .venv/bin/python -m pytest -q tests/test_interactive_authorization_runtime.py -k registered_http

The two excluded baseline tests were also run explicitly on the final tree:
**2 failed, 1 warning** in 1.27 seconds, with exactly the missing vault_batch_edit
sample and "forbade"/"forbidden" mismatch described above. They were not silently
treated as passing. No approval, confinement or containment check was relaxed to
avoid them. All changes remain uncommitted on fix/reliable-agent-tool-runtime.

## Knowledge-engine integration follow-on (2026-10-28)

The Obsidian knowledge engine reuses the repaired request/resource dispatcher,
TurnContract, child agent loop and session task-artifact registry. It does not
introduce a parallel approval system. See [knowledge-engine.md](knowledge-engine.md)
for its index, model-role/fallback integration, tested commands, hardware limits
and recorded results. The affected integration selection passed 1,023 tests;
the final knowledge/model-role selection passed 81 and the Settings browser test
passed. The earlier policy-denial wording fixture was corrected without changing
execution policy. The earlier compact-preview sample-set limitation is separately
identified in that report. Existing audit and directory-boundary behavior remains
covered; no real vault or remote machine was used for validation.

## Tool discovery and post-tool context repair (2026-10-29)

This focused repair addresses two independent failures observed on the running
zum installation. Investigation used read-only SSH access to its application log,
saved message metadata, and configured loopback inference endpoint. Neither the
running application nor its conversations, settings, credentials, or vault were
changed. One synthetic oversized inference request reproduced the endpoint's raw
HTTP response; it contained no user history or tool calls.

### Root causes and evidence

`get_tool_index()` is allowed to return `None` while its optional Chroma-backed
semantic index warms or is in a failure cooldown. Chroma was unavailable at
`localhost:8100` in the live logs. `discover_tools` unconditionally called
`index.get_tools_for_query(...)`, causing the reported `NoneType` exception.
The old discovery call also removed the router's `ALWAYS_AVAILABLE` names from
ranked matches, even though those names include useful filesystem reads. Tool
schema selection and execution authority remain separate: discovery only exposes
schemas from the request's existing policy-filtered catalog.

The tool continuation was a separate model-context failure. At 01:52:24 local
time, the live log shows `discover_tools` failing and `get_workspace` succeeding,
then a second generation with 56 schemas (58,175 schema JSON characters). The
provider rejected that generation with: `request exceeds the context window:
prompt (32820 tokens) leaves no room to answer in the context (32768); requests
are never truncated`. Authenticated `/props`, `/slots`, and `/v1/models` calls
reported a 32,768-token serving window, while the older unauthenticated model
probe could not read it and the model-family fallback assumed 131,072. The
regular agent route did not pass its already available, credentialed serving
resolution into the agent loop; the compact route did. Request trimming also
underestimated the final per-candidate tool-schema cost, and the bounded 400
recovery parser did not understand the provider's `prompt (N tokens)` and
`context (N)` wording when `max_tokens` was zero.

The original raw HTTP response was not stored by Mabel, so its complete original
body cannot be reconstructed. The same endpoint, when sent synthetic oversized
text, returned the complete body:

    {"error":{"type":"invalid_request_error","message":"request exceeds the context window: prompt (40052 tokens) leaves no room to answer in the context (32768); requests are never truncated"}}

The production outgoing structure was checked in the routing logs and request
construction path, then exercised with an HTTPX mock transport: a chat-completion
request carries message history, current tool-call/result pairing, the advertised
schemas, and output-token parameters. The original full request body was not
persisted, so exact original message contents are not claimed.

### Repair and verification

- `src/agent_runtime/tool_catalog.py` now uses a ready semantic index when
  available, respects warm/cooldown states, and falls back to bounded lexical
  ranking of the existing request catalog when semantic retrieval is unavailable.
  It retains ranked native read tools and never adds execution grants.
- `src/tool_index.py` now registers `discover_tools` in the existing description
  index, restoring schema/description parity.
- `routes/chat_routes.py` passes the credentialed serving-context resolution to
  both regular and compact agent turns. `src/agent_loop.py` budgets the actual
  per-route wire request, including selected schemas and output space, while
  leaving saved history unchanged. `src/generation_budget.py` parses the observed
  400 shape so its existing bounded pre-content recovery can reduce the request.
- `tests/test_discovery_context_regression.py` covers missing/warming/degraded
  discovery, policy isolation, healthy semantic ranking, both runtime paths,
  the real request/retry generator with a mock HTTP 400, and immutable history.
  Existing context-route and model-routing tests were updated for the newly
  shared serving-resolution behavior.

The failure was reproduced before the fix by two focused regression failures:
the `NoneType` dereference and the unparsed context-window response. The final
focused/broader selection passed **536 tests, 2 warnings** in 158.37 seconds,
including approval, audit, discovery, context, model-routing, and workspace
regressions. `git diff --check` passed. No live Dottie/Gyro-S generation was run
after the local change. Chroma remains unavailable on zum; native catalog
discovery now degrades safely, but semantic ranking requires that service to
recover. At the time of that focused run, the running zum installation had not
yet received these local changes. Deployment is recorded below.

## Cross-device merge and zum deployment (2026-10-29)

The two source trees differed in 13 shared files before merging. Zum had
additional knowledge-engine fixes for lossless Windows filesystem IDs, bounded
long Markdown source lines, and model-role endpoint refresh in Settings. Those
changes and their tests were copied into this checkout and retained. The local
discovery/context repair and its tests were then deployed to zum with exact
prior-hash checks. The separate experimental `mabel-linker` source and synthetic
fixtures were mirrored here without integration changes. Machine-specific
Syncthing rules and existing backup files were left on zum.

The merged local selection passed **577 tests, 2 warnings**; the focused merged
selection passed **198 tests, 2 warnings**. The Settings browser test and all
eight linker tests passed. On Windows, the first selection passed 185 tests and
found one test-fixture error: Swedish text was written with the platform default
encoding and then read as UTF-8. The fixture now writes UTF-8 explicitly; the
full knowledge-index module then passed **30 tests, 1 warning** on zum. This
was a fixture correction, not a vault or index data migration.

The nine runtime/test/report files were installed with a preconditioned backup
at `C:\Users\ZUM\.mabel-deploy-backups\20261029T012820Z-bf6f3861`.
An initial restart script stopped the old launcher but mishandled the expected
empty-port response from PowerShell, so it exited before relaunching. A direct
SSH child launch served briefly but ended with the SSH session. The updated
launcher was then started with `Win32_Process.Create`; a separate SSH session
confirmed PID 41900 still listening on `127.0.0.1:7011` after disconnect.
HTTP `/` returned 200 after startup. No model-driven live conversation was
run, and no real vault note or saved conversation was changed.

This Windows launch keeps the server running independently of SSH. It did not
create an automatic startup task for the next reboot. Chroma on zum remains
offline, so semantic discovery will use the catalog fallback until it recovers.
