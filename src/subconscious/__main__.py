"""python -m src.subconscious run [archivist|scout|dreamer ...] | inbox"""
from __future__ import annotations

import asyncio
import json
import sys

from .inbox import default_inbox
from .loops import LOOPS, run


def main() -> None:
    args = sys.argv[1:] or ["inbox"]
    if args[0] == "run":
        print(json.dumps(asyncio.run(run(args[1:] or LOOPS)), indent=2, ensure_ascii=False, default=str))
    elif args[0] == "inbox":
        for item in default_inbox().list(limit=100):
            print(f"[{item['loop']}/{item['kind']}] p{item['priority']} c{item['confidence']} {item['title']}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
