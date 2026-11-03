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
