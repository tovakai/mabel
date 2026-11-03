#!/usr/bin/env python3
"""Synthetic latency benchmark: sequential web+vault vs research_parallel.

This uses fake backends with fixed sleeps. It measures coordinator overlap only
and must not be cited as a production speedup. Wall-clock, model-request count,
tool time, and approximate context cost are reported for the fake workload.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

from src.agent_runtime import parallel_research as pr


WEB_SLEEP = 0.12
VAULT_SLEEP = 0.18
APPROX_CTX_TOKENS_PER_BRANCH = 400


class _Auth:
    owner = "bench"
    session_id = "bench"
    workspace = "/tmp"
    denied = frozenset()
    block_all = False

    def restricted(self, operation):
        return False

    def permits(self, operation):
        return operation.tool in {"web_search", "dispatch_dottie"}


async def _fake_child(block, **kwargs):
    if block.tool_type == "web_search":
        await asyncio.sleep(WEB_SLEEP)
        return block.tool_type, {
            "output": json.dumps({"results": [{"title": "t", "url": "https://ex", "snippet": "s"}]}),
            "exit_code": 0,
        }
    await asyncio.sleep(VAULT_SLEEP)
    return block.tool_type, {
        "output": json.dumps({"status": "complete", "findings": [{
            "claim": "c", "confidence": "source_supported",
            "evidence": [{"note_id": "n", "rel_path": "a.md", "start_line": 1, "end_line": 1, "quote": "c"}],
        }]}),
        "exit_code": 0,
    }


async def sequential():
    from src.tool_types import ToolBlock
    t0 = time.monotonic()
    model_requests = 0  # fake children do no LLM calls
    await _fake_child(ToolBlock("web_search", json.dumps({"query": "q"})))
    await _fake_child(ToolBlock("dispatch_dottie", json.dumps({"research_objective": "v"})))
    wall = time.monotonic() - t0
    return {
        "mode": "sequential",
        "wall_ms": round(wall * 1000, 1),
        "model_requests": model_requests,
        "tool_time_ms": round((WEB_SLEEP + VAULT_SLEEP) * 1000, 1),
        "approx_context_tokens": APPROX_CTX_TOKENS_PER_BRANCH * 2,
    }


async def _fake_run_child(tool, arguments, ctx, *, timeout_s):
    from src.tool_types import ToolBlock
    _, result = await _fake_child(ToolBlock(tool, json.dumps(arguments)))
    return result


async def parallel():
    ctx = {
        "owner": "bench",
        "session_id": "bench",
        "workspace": "/tmp",
        "request_authority": _Auth(),
        "disabled_tools": frozenset(),
        "tool_policy": None,
    }
    t0 = time.monotonic()
    with patch.object(pr, "_run_child", new=_fake_run_child), \
            patch.object(pr, "research_shares_foreground_endpoint", return_value=(False, {})):
        result = await pr.execute(json.dumps({
            "mode": "both", "web_query": "q", "vault_objective": "v",
        }), ctx)
    wall = time.monotonic() - t0
    package = json.loads(result["output"])
    return {
        "mode": "research_parallel",
        "wall_ms": round(wall * 1000, 1),
        "model_requests": 0,
        "tool_time_ms": round(
            max(package["metrics"]["web_ms"], package["metrics"]["vault_ms"]), 1
        ),
        "approx_context_tokens": APPROX_CTX_TOKENS_PER_BRANCH * 2,
        "concurrent": package["metrics"]["concurrent"],
        "reported_web_ms": package["metrics"]["web_ms"],
        "reported_vault_ms": package["metrics"]["vault_ms"],
        "reported_total_ms": package["metrics"]["total_ms"],
    }


async def main():
    seq = await sequential()
    par = await parallel()
    speedup = seq["wall_ms"] / par["wall_ms"] if par["wall_ms"] else None
    report = {
        "note": "Synthetic fake-backend benchmark only; not a production measurement.",
        "sequential": seq,
        "parallel": par,
        "synthetic_wall_speedup": round(speedup, 2) if speedup else None,
        "expected_overlap_ms": round(min(WEB_SLEEP, VAULT_SLEEP) * 1000, 1),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
