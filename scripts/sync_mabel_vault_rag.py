#!/usr/bin/env python3
"""Mabel-vault RAG: dry-run scan or immediate incremental sync.

Examples:
    python scripts/sync_mabel_vault_rag.py --check
    python scripts/sync_mabel_vault_rag.py --sync

Loads local .env without overriding an already configured environment.
--check never contacts ChromaDB, downloads models, or modifies vault files.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Direct script execution does not automatically add the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Count eligible notes; do not index")
    group.add_argument("--sync", action="store_true", help="Run one incremental Chroma sync")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    except ImportError:
        pass

    from src.mabel_vault_rag import (
        _read_snapshot, configured_owners, resolve_vault_root, sync_configured_vault,
    )

    try:
        root = resolve_vault_root()
        owners = configured_owners()
        if args.check:
            notes = _read_snapshot(root)
            counts = {}
            for p in notes:
                section = p.split("/", 1)[0]
                counts[section] = counts.get(section, 0) + 1
            print(f"Vault: {root}")
            print(f"Markdown notes: {len(notes)}")
            print(f"Configured RAG owners: {', '.join(owners) if owners else '(none)'}")
            for section, count in sorted(counts.items()):
                print(f"  {section}: {count}")
            print("No writes performed.")
            return 0
        result = sync_configured_vault()
        if result.get("disabled"):
            print("Mabel vault RAG is disabled. Set MABEL_VAULT_RAG_ENABLED=true.")
            return 2
        print("Mabel vault RAG sync:", result)
        return 1 if result.get("failed") else 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Mabel vault RAG: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
