"""Inspect staged episodic candidates (no LLM, no automatic promotion)."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from src.constants import DATA_DIR
from src.agent_runtime.event_ledger import Ledger
from .candidates import CandidateQueue
from .outbox import flush_candidate_events
from .candidates import CapturePolicy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="Exact Mabel storage owner")
    parser.add_argument(
        "--state-dir", type=Path,
        default=Path(DATA_DIR) / "episodic_candidates",
        help="Local, unsynced SQLite staging directory",
    )
    parser.add_argument("--limit", type=int, default=30)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="Inspect candidates, read-only after initialization")
    listing.add_argument(
        "--state", choices=["pending", "deferred", "rejected", "quarantined"],
        default="pending",
    )
    sub.add_parser("events", help="Inspect undelivered transition envelopes")
    sub.add_parser("retention-status", help="Show deletion-tombstone and pending-note counts")
    reconcile = sub.add_parser(
        "reconcile-deletions",
        help="Explicitly scrub candidates whose canonical sessions are missing",
    )
    reconcile.add_argument("--confirm-scrub", action="store_true")
    reconcile.add_argument("--after-session-id")
    cleanup = sub.add_parser(
        "cleanup-deleted-episodes",
        help="Explicitly remove only verified, owner-scoped episode files",
    )
    cleanup.add_argument("--vault-root", type=Path, required=True)
    cleanup.add_argument("--protected-root", type=Path, action="append", required=True)
    cleanup.add_argument("--confirm-remove-files", action="store_true")
    cleanup.add_argument("--session-id")
    reviewer = sub.add_parser(
        "review", help="One explicitly consented Dottie inference; advice only",
    )
    reviewer.add_argument("--candidate-id", required=True)
    reviewer.add_argument(
        "--send-source-to-dottie", action="store_true",
        help="Explicitly allow this one excerpt to be sent to configured Research / Dottie",
    )
    publishing = sub.add_parser(
        "publish-events",
        help="Explicitly replay the outbox into the existing Phase 00 ledger",
    )
    publishing.add_argument("--ledger-path", type=Path, default=Path(DATA_DIR) / "phase0" / "events.jsonl")
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be between 1 and 100")
    queue = CandidateQueue(args.state_dir, owner=args.owner)
    if args.command == "list":
        output = {"owner": queue.owner, "candidates": queue.list(state=args.state, limit=args.limit)}
    elif args.command == "events":
        output = {"owner": queue.owner, "pending_events": queue.pending_events(limit=args.limit)}
    elif args.command == "retention-status":
        with queue._connect() as db:
            tombstones = db.execute(
                "SELECT count(*) FROM deleted_sessions WHERE owner=?", (queue.owner,),
            ).fetchone()[0]
            pending = db.execute(
                "SELECT count(*) FROM episode_deletion_queue WHERE owner=?", (queue.owner,),
            ).fetchone()[0]
        output = {"owner": queue.owner, "deleted_sessions": tombstones,
                  "pending_episode_files": pending}
    elif args.command == "reconcile-deletions":
        if not args.confirm_scrub:
            parser.error("reconcile-deletions requires --confirm-scrub")
        from core.database import SessionLocal
        from .retention import reconcile_missing_sessions
        with SessionLocal() as db:
            output = reconcile_missing_sessions(
                queue, db=db, limit=args.limit,
                after_session_id=args.after_session_id,
            )
    elif args.command == "cleanup-deleted-episodes":
        if not args.confirm_remove_files:
            parser.error("cleanup-deleted-episodes requires --confirm-remove-files")
        from .retention import complete_episode_deletions
        output = complete_episode_deletions(
            queue, vault_root=args.vault_root,
            protected_roots=tuple(args.protected_root),
            session_id=args.session_id, limit=args.limit,
        )
    elif args.command == "review":
        if not args.send_source_to_dottie:
            parser.error("review requires --send-source-to-dottie")
        from core.database import SessionLocal
        from routes.prefs_routes import _load_for_user
        from .dottie_review import review_candidate_with_dottie

        enabled = os.getenv("MABEL_EPISODIC_CAPTURE_ENABLED", "").strip().lower() in {
            "1", "true", "yes",
        }
        if not enabled:
            parser.error("review requires the explicit MABEL_EPISODIC_CAPTURE_ENABLED pilot")
        prefs = _load_for_user(queue.owner)
        policy = CapturePolicy(
            episodic_enabled=enabled,
            memory_enabled=bool(prefs.get("memory_enabled", True)),
        )
        with SessionLocal() as db:
            output = asyncio.run(review_candidate_with_dottie(
                queue, args.candidate_id, db=db, policy=policy,
                send_source_to_dottie=True,
            ))
        # CLI output contains only advisory metadata, never source excerpts.
        output = {key: output[key] for key in (
            "id", "candidate_id", "reviewer", "recommendation",
            "confidence", "rationale_code", "created", "already_reviewed",
        )}
    else:
        receipt = flush_candidate_events(queue, Ledger(args.ledger_path), limit=args.limit)
        output = {
            "owner": queue.owner, "attempted": receipt.attempted,
            "acknowledged": receipt.acknowledged,
            "pending_next_page": receipt.pending_next_page,
        }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
