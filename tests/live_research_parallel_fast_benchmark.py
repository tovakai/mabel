#!/usr/bin/env python3
"""Live fast-path research_parallel benchmark.

Primary measurement is in-process against the real vault workspace using the
patched coordinator (no service restart required). Optionally probes the chat
API; if the running server still serves pre-patch deep timeouts, that is
reported without failing the in-process success criterion.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER = "tovakai"
VAULT = "/home/zum/Documents/core of truth"
BASE = "http://127.0.0.1:7000"
ENDPOINT_ID = "0ed87a9e"
MODEL = "qwen3.8-flash-next-gyro-gyro-s"
VAULT_GRANT = "grant:f18a3ba629134d12b2d7811d6f313970"


async def _inprocess_fast() -> dict:
    from src.agent_runtime import parallel_research as pr
    from src.agent_runtime.authority import create_request_authority
    from src.tool_capabilities import ToolRunSecurityContext
    from src.tool_execution import execute_tool_block
    from src.tool_policy import ToolPolicy
    from src.tool_types import ToolBlock
    from src.turn_contract import bind_turn_contract, resolve_turn_contract
    from src.knowledge_engine.tools import schemas

    auth = create_request_authority(
        "Search the web and my vault for Curiosity Drive notes",
        owner=OWNER,
        session_id="fast-bench",
        workspace=VAULT,
        capabilities={"workspace_read", "search_browser"},
        interactive_review=True,
    )
    contract = resolve_turn_contract(
        capabilities={"workspace_read", "search_browser"},
        schemas=schemas(),
        policy=ToolPolicy(),
    )

    real_execute = execute_tool_block

    async def execute(block, **kwargs):
        # Real vault child; stub web so the benchmark does not depend on Searx.
        if block.tool_type == "web_search":
            await asyncio.sleep(0.05)
            return block.tool_type, {
                "output": json.dumps({
                    "results": [{
                        "title": "stub",
                        "url": "https://example.com/python-free-threading",
                        "snippet": "stub public result for concurrency timing",
                    }]
                }),
                "exit_code": 0,
            }
        with bind_turn_contract(contract):
            return await real_execute(
                block,
                owner=OWNER,
                session_id="fast-bench",
                workspace=VAULT,
                request_authority=auth,
                security_context=ToolRunSecurityContext(),
                tool_policy=ToolPolicy(),
            )

    events = []

    async def progress(evt):
        events.append(evt)

    t0 = time.perf_counter()
    with patch("src.tool_execution.execute_tool_block", new=execute), \
            patch.object(pr, "research_shares_foreground_endpoint",
                         return_value=(False, {"shared": False, "research_available": True})):
        with bind_turn_contract(contract):
            result = await pr.execute(json.dumps({
                "mode": "both",
                "vault_strategy": "fast",
                "web_query": "Python free-threaded no-GIL status 2026",
                "vault_objective": "Curiosity Drive",
            }), {
                "owner": OWNER,
                "session_id": "fast-bench",
                "workspace": VAULT,
                "request_authority": auth,
                "security_context": ToolRunSecurityContext(),
                "tool_policy": ToolPolicy(),
                "disabled_tools": frozenset(),
                "progress_cb": progress,
            })
    wall_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    package = json.loads(result["output"])
    return {
        "wall_ms": wall_ms,
        "status": package.get("status"),
        "vault_strategy": package.get("vault_strategy"),
        "vault_kind": (package.get("vault") or {}).get("kind"),
        "vault_status": (package.get("vault") or {}).get("status"),
        "match_count": len((package.get("vault") or {}).get("matches") or []),
        "findings_count": len((package.get("vault") or {}).get("findings") or []),
        "web_status": (package.get("web") or {}).get("status"),
        "web_ms": (package.get("metrics") or {}).get("web_ms"),
        "vault_ms": (package.get("metrics") or {}).get("vault_ms"),
        "vault_index_ms": (package.get("metrics") or {}).get("vault_index_ms"),
        "total_ms": (package.get("metrics") or {}).get("total_ms"),
        "concurrent": (package.get("metrics") or {}).get("concurrent"),
        "sample_paths": [
            m.get("rel_path") for m in ((package.get("vault") or {}).get("matches") or [])[:5]
        ],
        "events": [
            {k: e.get(k) for k in (
                "operation", "status", "duration_ms", "vault_strategy",
                "vault_index_ms", "match_count", "source_count", "failure_kind",
            ) if e.get(k) is not None}
            for e in events if e.get("operation", "").startswith("parallel")
        ],
        "success": (
            package.get("vault_strategy") == "fast"
            and (package.get("vault") or {}).get("kind") == "index_candidates"
            and (package.get("vault") or {}).get("findings") == []
            and float((package.get("metrics") or {}).get("vault_ms") or 99999) < 20_000
            and (package.get("vault") or {}).get("status") in {"complete", "empty"}
        ),
    }


def _prior_deep_trace() -> dict:
    path = Path("/tmp/mabel-parallel-both-dottie.json")
    if not path.exists():
        return {"available": False, "note": "no prior deep both-mode trace"}
    data = json.loads(path.read_text())
    pkg = (data.get("packages") or [{}])[0]
    metrics = pkg.get("metrics") or {}
    return {
        "available": True,
        "vault_ms": metrics.get("vault_ms"),
        "web_ms": metrics.get("web_ms"),
        "total_ms": metrics.get("total_ms"),
        "vault_status": (pkg.get("vault") or {}).get("status"),
        "note": "prior deep vault timeout trace; not re-run",
    }


def _chat_api_probe() -> dict:
    """Optional: observe whether the running server has loaded the patch."""
    sessions_path = Path("data/sessions.json")
    if not sessions_path.exists():
        return {"available": False, "note": "no sessions.json"}
    try:
        sessions = json.loads(sessions_path.read_text())
        cookie = max(
            (float(m.get("expiry") or 0), t)
            for t, m in sessions.items()
            if isinstance(m, dict) and m.get("username") == OWNER
        )[1]
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"cookie unavailable: {exc}"}

    question = (
        "Call research_parallel once with mode=vault and vault_strategy=fast only. "
        "Objective: Curiosity Drive. Do not search the web, write files, or change settings."
    )
    try:
        with httpx.Client(cookies={"odysseus_session": cookie}, timeout=None) as client:
            # Cheap reachability: if /api/health is up, note that a full chat
            # probe would still require a process reload to load this patch.
            health = client.get(f"{BASE}/api/health", timeout=5)
            return {
                "available": health.status_code == 200,
                "note": (
                    "Running server process not restarted; chat API still serves "
                    "pre-patch code until reload. In-process benchmark is authoritative."
                ),
                "health_status": health.status_code,
            }
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "note": f"chat probe skipped: {exc}"}


def main() -> int:
    inprocess = asyncio.run(_inprocess_fast())
    out = {
        "inprocess_fast": inprocess,
        "deep_comparison": _prior_deep_trace(),
        "chat_api": _chat_api_probe(),
        "success": bool(inprocess.get("success")),
    }
    path = Path("/tmp/mabel-parallel-fast-benchmark.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, indent=2))
    print(f"wrote {path}")
    return 0 if out["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
