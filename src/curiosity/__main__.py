"""Manual Curiosity Drive (Phase 3).

    python -m src.curiosity audit             # unresolved-link audit (read-only)
    python -m src.curiosity candidates        # what she could pick right now
    python -m src.curiosity session           # one full session; Mabel chooses
    python -m src.curiosity session --resume ID
    python -m src.curiosity backlog           # open questions and spawned links
"""
from __future__ import annotations

import argparse
import asyncio
import json

from .backlog import Backlog
from .candidates import audit
from .session import Paths, Session


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m src.curiosity")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("audit")
    cand = sub.add_parser("candidates")
    cand.add_argument("--seed", type=int)
    run = sub.add_parser("session")
    run.add_argument("--resume")
    run.add_argument("--pick", type=int, help="restrict the choice to one list item (testing)")
    run.add_argument("--seed", type=int)
    sub.add_parser("backlog")
    args = parser.parse_args()
    paths = Paths()
    if args.command == "audit":
        print(json.dumps(audit(paths.index), indent=2))
    elif args.command == "candidates":
        session = Session(paths, session_id="preview")
        session.file = paths.data / "preview.json"   # never resumed
        session.state["steps"] = {}
        for i, c in enumerate(session.build_candidates(seed=args.seed), 1):
            print(f"{i:2d}. [{c['kind']}] {c.get('question') or c['name']}  score={c.get('score', 0)}  "
                  f"{'; '.join(c.get('reasons') or [])}")
    elif args.command == "session":
        session = Session(paths, session_id=args.resume)
        print(f"session {session.id}")
        result = asyncio.run(session.run(forced=args.pick, seed=args.seed))
        print(json.dumps({"choice": session.done("choice"), "result": result}, indent=2, ensure_ascii=False,
                         default=str))
    elif args.command == "backlog":
        print(json.dumps(Backlog(paths.data / "backlog.json").load(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
