# Mabel production cutover: episodic memory and web-loop fixes

This release candidate contains the Phase 00.5 episodic memory stack plus
web-fetch URL/extraction repairs, unavailable-browser hiding, and conservative
Qwen web-round cache/budget diagnostics.

**The release branch is substantially ahead of GitHub dev.** Do not merge
it blindly into dev, force-update a dirty production checkout, or copy it
into a new worktree that would pick up a separate empty data directory.

## Production preflight on Arch

    cd ~/Documents/mabel
    git status --short
    git branch --show-current
    git fetch origin release/mabel-episodic-web-20261103
    git log -1 --oneline FETCH_HEAD
    git merge-base --is-ancestor HEAD FETCH_HEAD && echo FAST_FORWARD_OK

Stop if status lists uncommitted files or the ancestry check fails.
Never use git reset --hard or a forced checkout to make this succeed.

Using your current production Python interpreter, inspect the data directory:

    .venv-arch/bin/python -c 'from src.constants import DATA_DIR; print(DATA_DIR)'

Choose the actual interpreter if the shown venv path does not exist.
Stop only the Mabel backend using your existing launcher or user service;
leave Strata and Dottie running. Snapshot the actual DATA_DIR while SQLite
writers are stopped into a directory with private permissions. Do not copy
active WAL/SHM files as if they were an atomic database backup.

Save the current source revision:

    git branch "backup/mabel-before-episodic-$(date +%Y%m%d-%H%M%S)" HEAD
    git merge --ff-only FETCH_HEAD

This is a source-code update only. Do not remove the production DATA_DIR,
re-enter stored model configurations, or change the proxy/port.

## Validate before restarting

Use your working pytest environment against the actual release checkout:

    python -m pytest -q --tb=short \
      tests/test_web_fetch_round_efficiency.py \
      tests/test_agent_web_round_cache_contract.py \
      tests/test_web_fetch_batch.py \
      tests/test_web_fetch_plaintext.py \
      tests/test_web_fetch_size_caps.py \
      tests/test_tool_schemas.py \
      tests/test_agent_latency_regression.py \
      tests/test_episodic_semantic_advisory.py \
      tests/test_episodic_evidence_associations.py \
      tests/test_episodic_readonly_recall.py \
      tests/test_episodic_message_invalidation.py \
      tests/test_episodic_session_retention.py \
      tests/test_episodic_dottie_review.py \
      tests/test_episodic_review_consolidation.py \
      tests/test_episodic_verified_promotion.py \
      tests/test_episodic_candidate_foundation.py \
      tests/test_episodic_phase0_delivery.py \
      tests/test_episodic_completed_turn_capture.py \
      tests/test_phase0_foundations.py \
      tests/test_session_actions_cleanup.py \
      tests/test_session_manager_cleanup.py

Start Mabel normally. With MABEL_EPISODIC_CAPTURE_ENABLED absent or false,
nothing is auto-captured. Confirm chat, current model endpoints and ordinary
tool execution work before enabling the pilot.

## Controlled one-conversation live smoke test

Enable MABEL_EPISODIC_CAPTURE_ENABLED=1 deliberately for the Mabel backend.
The session must be non-incognito with memory extraction enabled. Memory
injection must also be enabled for reviewer/recall inspection; neither
setting authorizes unattended Dottie use.

Use actual owner and absolute vault paths, not guesses:

    export OWNER='ACTUAL_MABEL_OWNER'
    export MABEL_VAULT='/ABSOLUTE/PATH/TO/MABEL-WRITABLE-VAULT'
    export SOURCE_VAULT='/ABSOLUTE/PATH/TO/CORE-OF-TRUTH'

After a single completed and persisted conversation:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 python -m src.episodic_memory \
      --owner "$OWNER" list

Select one pending candidate. The next step sends its private excerpt to
the already-configured owner-scoped Research/Dottie role only with explicit
consent:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 python -m src.episodic_memory \
      --owner "$OWNER" review --candidate-id CANDIDATE_ID \
      --send-source-to-dottie

The review is advisory. To promote this candidate by your explicit decision:

    MABEL_EPISODIC_CAPTURE_ENABLED=1 python -m src.episodic_memory \
      --owner "$OWNER" promote --candidate-id CANDIDATE_ID \
      --vault-root "$MABEL_VAULT" --protected-root "$SOURCE_VAULT" \
      --confirm-promote

Verify read-only retrieval (the response prints private excerpts locally):

    MABEL_EPISODIC_CAPTURE_ENABLED=1 python -m src.episodic_memory \
      --owner "$OWNER" recall 'words from the test conversation' \
      --vault-root "$MABEL_VAULT" --protected-root "$SOURCE_VAULT" \
      --read-source-excerpts

Neither association graph inspection nor semantic embedding suggestions
are required for this basic smoke test. No automatic promotion, recall
injection or reviewer scheduler is enabled by the release.

## Web-round performance verification

Test a public, link-heavy wiki index without extraction_goal, and a focused
prose page with extraction_goal. In logs compare:

- [agent-cache-audit]: routing schema/system-prefix digests
- [agent-cache-request]: actual model-bound schema/system-prefix digests
- [agent-web-budget]: Qwen3.8 continuation generation/thinking settings
- per-round model prefill, generation, context token and tool count metrics

Stable prefixes are necessary for KV cache reuse but do not prove the backend
had a cache hit. Benchmark matched runs and consult Strata's cache/slot
diagnostics before claiming a particular speedup. The browser page tool
is hidden because its current producer rejects page operations.

## Rollback

Stop the backend, retain newly created candidate SQLite/Markdown state for
manual review, and use the saved pre-release Git commit/branch to restore
the older code. Restore the data snapshot only if specifically required;
reverting a whole data directory also erases unrelated user activity after
the backup. Restart with the episodic pilot flag disabled. Do not delete
source vaults or reset local Git history.
