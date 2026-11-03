#!/usr/bin/env python3
"""Explain tool routing for recorded turns, from the debug console database.

Examples:
  tool_routing_why.py --tool research_parallel            # latest turn
  tool_routing_why.py --tool research_parallel --last 10  # last 10 turns, one line each
  tool_routing_why.py --session <id> --turn <turn_id>     # full per-tool table for one turn
  tool_routing_why.py --excluded                          # latest turn, exclusions grouped by reason

Read-only. The database holds names, reason codes and counts only (see
src/agent_runtime/routing_trace.py); no prompts, arguments or outputs.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.agent_runtime.routing_trace import REASONS, STATES, explain  # noqa: E402


def _db_path(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    data = os.environ.get("ODYSSEUS_DATA_DIR") or str(ROOT / "data")
    return Path(data) / "debug_console.sqlite3"


def _rows(db: Path, *, owner, session, turn, last):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = ("SELECT owner, session_id, turn_id, timestamp, payload FROM trace_events "
               "WHERE json_extract(payload,'$.operation')='tool_routing'")
        args = []
        for col, value in (("owner", owner), ("session_id", session), ("turn_id", turn)):
            if value:
                sql += f" AND {col}=?"
                args.append(value)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(max(1, last))
        return [(o, s, t, ts, json.loads(p)["metadata"].get("routing") or {})
                for o, s, t, ts, p in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _one_line(summary: dict, tool: str) -> str:
    item = (summary.get("tools") or {}).get(tool)
    prof = summary.get("profile") or {}
    fam = ",".join((summary.get("contract") or {}).get("families") or []) or "-"
    tok = summary.get("tokens") or {}
    if item is None:
        verdict = "not_registered"
    elif item["s"].endswith("V"):
        verdict = "ADVERTISED via " + (item.get("via") or "?")
    else:
        verdict = "excluded: " + (item.get("why") or "?")
    return (f"{prof.get('model','?'):<34} fam={fam:<28} tools={len(((summary.get('rounds') or [{}])[0]).get('tools', []))!s:>3} "
            f"~{tok.get('first_round_schema_estimate')}tok  {tool}: {verdict}")


def _table(summary: dict):
    print("state  R=registered A=authorized D=discoverable S=selected V=advertised (?=n/a)")
    for name, item in sorted((summary.get("tools") or {}).items(), key=lambda kv: (kv[1]["s"], kv[0]), reverse=True):
        why = item.get("why") or ""
        via = item.get("via") or ""
        print(f"  {item['s']}  {name:<40} {('why=' + why) if why else ''}{('  via=' + via) if via else ''}")


def _grouped(summary: dict):
    groups = defaultdict(list)
    for name, item in (summary.get("tools") or {}).items():
        if item.get("why"):
            groups[item["why"]].append(name)
    for code, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        print(f"{code} ({len(names)}): {REASONS.get(code, '')}")
        print("    " + ", ".join(sorted(names)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db")
    ap.add_argument("--owner")
    ap.add_argument("--session")
    ap.add_argument("--turn")
    ap.add_argument("--last", type=int, default=1)
    ap.add_argument("--tool")
    ap.add_argument("--excluded", action="store_true", help="group exclusions by reason")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    db = _db_path(args.db)
    if not db.exists():
        print(f"no debug database at {db}", file=sys.stderr)
        return 2
    rows = _rows(db, owner=args.owner, session=args.session, turn=args.turn, last=args.last)
    if not rows:
        print("no tool_routing events recorded yet (events are written by turns run after this change)", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([{"owner": o, "session": s, "turn": t, "timestamp": ts, "routing": r}
                          for o, s, t, ts, r in rows], indent=2))
        return 0
    if args.tool and len(rows) > 1:
        for _, s, t, ts, summary in rows:
            print(f"{ts[:19]} {s[:12]} {_one_line(summary, args.tool)}")
        return 0
    for _, s, t, ts, summary in rows:
        print(f"== {ts} session={s} turn={t}")
        counts = summary.get("counts") or {}
        print("   " + "  ".join(f"{k}={counts.get(k)}" for k in STATES)
              + f"   schema~{(summary.get('tokens') or {}).get('first_round_schema_estimate')}tok"
              + f" of {(summary.get('tokens') or {}).get('context_limit')}")
        if args.tool:
            print("\n".join("   " + line for line in explain(summary, args.tool)))
        elif args.excluded:
            _grouped(summary)
        else:
            _table(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
