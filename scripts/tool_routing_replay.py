#!/usr/bin/env python3
"""Replay representative prompts through the real tool-routing pipeline.

Runs the production chat route -> turn contract -> agent loop -> per-candidate
request factory in-process, against a *scratch copy* of the data directory, and
captures the exact tool schemas that would be sent to the model. Nothing is
written to the real data directory and no tool is executed.

  --live   additionally forwards each captured request to the configured model
           (default: the Strata Gyro-S endpoint) through the app's own transport,
           records the model's first tool call, and measures the exact schema
           token cost as prompt_tokens(with tools) - prompt_tokens(without tools).
           Tool calls are recorded and discarded, never executed.

Output: a JSON report (--out) and a console table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent

# Each case: what the user actually needed.
#   need: list of acceptable tool sets; the turn is "routable" if any set is fully advertised.
#         [] means no tool is needed (conversational).
#   first: acceptable first tool calls for --live scoring ("none" = answer directly).
CASES = [
    # conversational
    {"id": "conv-emotional", "kind": "conversational", "need": [],
     "first": ["none"], "message": "I just finished chapter 40 of Frieren and honestly I'm a bit emotional about it"},
    {"id": "conv-thanks", "kind": "conversational", "need": [], "first": ["none"],
     "message": "thanks, that actually helped a lot!"},
    {"id": "conv-opinion", "kind": "conversational", "need": [], "first": ["none"],
     "message": "what do you think makes a magic system feel satisfying in a story?"},
    {"id": "conv-explain", "kind": "conversational", "need": [], "first": ["none"],
     "message": "can you explain the difference between TCP and UDP in simple terms?"},
    # vault
    {"id": "vault-topic", "kind": "vault",
     "need": [["vault_knowledge_search"], ["research_parallel"], ["dispatch_dottie"]],
     "first": ["vault_knowledge_search", "research_parallel", "dispatch_dottie", "grep"],
     "message": "what do my notes say about quantization experiments?"},
    {"id": "vault-find-summarize", "kind": "vault",
     "need": [["vault_knowledge_search", "read_file"], ["grep", "read_file"], ["research_parallel"]],
     "first": ["vault_knowledge_search", "grep", "glob", "research_parallel"],
     "message": "find my note about Berserk and summarize what I wrote"},
    {"id": "vault-rewrite", "kind": "vault",
     "need": [["read_file", "edit_file"], ["read_file", "write_file"]],
     "first": ["read_file", "ls", "glob", "grep"],
     "message": "rewrite my isekai genre page in the vault so the recommendations are grouped by tone"},
    {"id": "vault-count", "kind": "vault",
     "need": [["grep"], ["vault_audit"], ["vault_knowledge_search"]],
     "first": ["grep", "vault_audit", "vault_knowledge_search", "glob"],
     "message": "how many of my vault notes are tagged #manga?"},
    # web
    {"id": "web-synopsis", "kind": "web", "need": [["web_search"]], "first": ["web_search"],
     "message": "what is the anime Dandadan about?"},
    {"id": "web-weather", "kind": "web", "need": [["get_weather"], ["web_search"]],
     "first": ["get_weather", "web_search"],
     "message": "what's the weather going to be in Stockholm tomorrow?"},
    {"id": "web-url", "kind": "web", "need": [["web_fetch"]], "first": ["web_fetch"],
     # Explicit web-off now also overrides the pasted-URL fetch exemption.
     "need_noweb": "refuse",
     "message": "read https://github.com/ggml-org/llama.cpp/releases and summarize the newest release"},
    {"id": "web-news", "kind": "web", "need": [["web_search"]], "first": ["web_search"],
     "message": "any recent news about Qwen model releases this week?"},
    # combined vault + web
    {"id": "both-compare", "kind": "combined",
     "need": [["research_parallel"], ["vault_knowledge_search", "web_search"]],
     "first": ["research_parallel", "vault_knowledge_search", "web_search"],
     "message": "compare what my notes say about Chainsaw Man part 2 with what people online think of it"},
    {"id": "both-update-check", "kind": "combined",
     "need": [["research_parallel"], ["vault_knowledge_search", "web_search"]],
     "first": ["research_parallel", "vault_knowledge_search", "web_search"],
     "message": "check my vault notes on llama.cpp and tell me if there's a newer release online than what I wrote down"},
    {"id": "both-wiki-match", "kind": "combined",
     "need": [["research_parallel"], ["vault_knowledge_search", "web_search"], ["vault_knowledge_search", "web_fetch"]],
     "first": ["research_parallel", "vault_knowledge_search", "web_search"],
     "message": "do my notes about the One Piece Egghead arc match what the wiki says?"},
    {"id": "both-research", "kind": "combined",
     "need": [["research_parallel"], ["trigger_research"]],
     "first": ["research_parallel", "trigger_research"],
     # Explicit web-off denies Deep Research (web-only); no web tool may be offered.
     "need_noweb": [],
     "message": "research how well 4-bit quantized MoE models hold up, using both my notes and public benchmarks"},
    # added after the first evaluation
    {"id": "vault-obsidian", "kind": "vault",
     "need": [["vault_knowledge_search"], ["research_parallel"], ["grep", "read_file"]],
     "first": ["vault_knowledge_search", "research_parallel", "grep", "dispatch_dottie"],
     "message": "what did I write in my obsidian vault about Vinland Saga?"},
    {"id": "notes-create", "kind": "notes_app", "need": [["manage_notes"]], "first": ["manage_notes"],
     "message": "add a note: buy oat milk and coffee filters"},
    {"id": "notes-create-named", "kind": "notes_app", "need": [["manage_notes"]], "first": ["manage_notes"],
     "message": "create a note called packing list with passport, charger and headphones"},
    {"id": "task-hash", "kind": "compute", "need": [["python"]], "first": ["python", "bash"],
     "message": "what is the SHA-256 hash of the exact string hello world?"},
    {"id": "task-arith", "kind": "compute", "need": [], "first": ["none", "python"],
     "message": "what's 17% of 2,340 euros?"},
    {"id": "task-days", "kind": "compute", "need": [], "first": ["none", "python", "bash", "discover_tools"],
     "message": "how many days are there between 14 March 1987 and 3 November 2026?"},
    {"id": "task-uuid", "kind": "compute", "need": [], "first": ["none", "python", "bash", "discover_tools"],
     "message": "generate a random UUID v4 for me"},
    {"id": "task-shell", "kind": "compute", "need": [["bash"]], "first": ["bash"],
     "message": "run df -h and tell me how much disk space is free on my home partition"},
]

# With the web toggle off: web-only requests should be refused (no tools is correct);
# combined requests must keep their vault half.
NOWEB_NEED = {
    "web": "refuse",
    "combined": [["vault_knowledge_search"], ["research_parallel"], ["grep", "read_file"], ["dispatch_dottie"]],
}

MODEL = "qwen3.8-flash-next-gyro-gyro-s"
ENDPOINT_ID = "0ed87a9e"
ENDPOINT_URL = "http://127.0.0.1:8080/v1"
OWNER = "tovakai"


_SECRET_FILES = {".app_key"}


def _copy_data(src: Path, dst: Path, *, include_app_key: bool = False) -> None:
    """Owner-only scratch copy. The app key is copied only for --live, and is
    deleted again as soon as the endpoint headers are resolved."""
    def ignore(_dir, names):
        return {n for n in names if n.startswith("debug_console.sqlite3") or n.endswith(("-wal", "-shm"))
                or n in {"logs", "effects", "generated_images", "uploads", "personal_uploads", "tts_cache", "emoji_cache"}
                or (n in _SECRET_FILES and not include_app_key)}
    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(src, dst, ignore=ignore)
    os.chmod(dst, 0o700)
    for db in ("app.db", "scheduled_emails.db"):
        if (src / db).exists():
            (dst / db).unlink(missing_ok=True)
            with sqlite3.connect(f"file:{src / db}?mode=ro", uri=True) as source, sqlite3.connect(dst / db) as target:
                source.backup(target)


def _remove_data(data_dir: Path) -> None:
    """Delete the scratch copy after stopping writers that could recreate it."""
    try:  # the debug store's daemon writer lazily mkdirs its parent on flush
        from src.agent_runtime import debug_trace
        if debug_trace._store is not None:
            debug_trace._store.close()
            debug_trace._store = None
    except Exception:
        pass
    for _ in range(3):
        shutil.rmtree(data_dir, ignore_errors=True)
        if not data_dir.exists():
            return
        time.sleep(0.5)
    print(f"WARNING: scratch data copy could not be removed: {data_dir}", file=sys.stderr)


def _setup(data_dir: Path):
    os.environ["ODYSSEUS_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(ROOT))
    import pytest  # noqa: F401  (MonkeyPatch)
    from routes import chat_routes
    from src import agent_loop
    return chat_routes, agent_loop


class _Request:
    def __init__(self, message, *, web: bool):
        self.headers = {}
        # The owner is an admin in auth.json; mirror the real auth manager.
        from core.auth import ADMIN_PRIVILEGES
        self.app = SimpleNamespace(state=SimpleNamespace(
            auth_manager=SimpleNamespace(get_privileges=lambda user: dict(ADMIN_PRIVILEGES))))
        self.state = SimpleNamespace(current_user=OWNER)
        # Same fields the web UI sends (static/js/chat.js, activeDirectories.js)
        # for a session with no explicitly activated directories.
        self._form = {
            "message": message, "session": "routing-replay", "mode": "agent", "plan_mode": "false",
            "compare_mode": "false", "no_documents": "true", "no_memory": "true",
            "allow_web_search": "true" if web else "false", "allow_bash": "true",
            "active_directories": "[]", "primary_local_directory_id": "",
            "selected_model": MODEL, "selected_endpoint_id": ENDPOINT_ID,
        }

    async def form(self):
        return self._form


def _live_headers():
    from core.database import SessionLocal, ModelEndpoint
    from src.endpoint_resolver import build_headers, resolve_endpoint_runtime
    db = SessionLocal()
    try:
        row = db.query(ModelEndpoint).filter(ModelEndpoint.id == ENDPOINT_ID).first()
        base, key = resolve_endpoint_runtime(row, owner=OWNER)
        return build_headers(key, base)
    finally:
        db.close()


def _parse_chunk(chunk):
    if not isinstance(chunk, str) or not chunk.startswith("data: ") or chunk.strip() == "data: [DONE]":
        return None
    try:
        return json.loads(chunk[6:])
    except ValueError:
        return None


async def _prompt_tokens(url, headers, model, messages, tools):
    """Provider-counted prompt tokens for one request (max_tokens=1, non-streaming)."""
    import httpx
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    body = {"model": model, "messages": messages, "max_tokens": 1, "stream": False}
    if tools:
        body["tools"] = tools
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(endpoint, json=body, headers=dict(headers or {}))
        usage = (response.json() or {}).get("usage") or {}
        return usage.get("prompt_tokens") or usage.get("input_tokens")
    except Exception:
        return None


async def _run_case(case, *, web, live, chat_routes, agent_loop, mp, headers, original_stream):
    from src.agent_runtime import routing_trace
    message = case["message"]
    captured = {"calls": []}

    async def capture(candidates, messages, **kwargs):
        captured.setdefault("routing_ms", round((time.monotonic() - started_at) * 1000, 1))
        factory = kwargs.get("candidate_request_factory")
        url, model, cand_headers = candidates[0]
        request = {"messages": messages, "kwargs": {"tools": kwargs.get("tools")}}
        if factory is not None:
            try:
                request = factory(0, url, model, cand_headers) or {}
                if hasattr(request, "__await__"):
                    request = await request
            except Exception as error:  # surface the real factory failure for diagnosis
                captured.setdefault("factory_errors", []).append(f"{type(error).__name__}: {str(error)[:300]}")
                raise
        tools = (request.get("kwargs") or {}).get("tools") or []
        call = {"path": "agent_round" if factory is not None else "direct_no_tools",
                "model": model, "tools": sorted((t.get("function") or {}).get("name", "") for t in tools),
                "message_count": len(request.get("messages") or messages)}
        if live and not captured["calls"]:
            # 1) The model's real choice, through the app's own transport (same request bytes).
            first_tools, text, errors = [], [], []
            started = time.monotonic()
            async for chunk in original_stream(candidates[:1], messages, **kwargs):
                if chunk.startswith("event: error"):
                    errors.append("stream_error")
                data = _parse_chunk(chunk.split("\n", 1)[-1] if chunk.startswith("event:") else chunk)
                if not isinstance(data, dict):
                    continue
                if data.get("type") == "tool_calls":
                    first_tools += [(c.get("function") or {}).get("name") or c.get("name") for c in data.get("calls") or []]
                elif data.get("type") == "diagnostic_event" and data.get("operation") == "model_request" \
                        and data.get("status") == "completed":
                    call["live_prompt_tokens"] = data.get("input_tokens")
                elif data.get("error"):
                    errors.append(str(data.get("error"))[:120])
                elif isinstance(data.get("delta"), str) and not data.get("thinking"):
                    text.append(data["delta"])
            call["live_first_tools"] = first_tools
            call["live_answered_directly"] = bool("".join(text).strip()) and not first_tools
            call["live_text"] = "".join(text).strip()[:400]
            call["live_errors"] = errors
            call["live_seconds"] = round(time.monotonic() - started, 1)
            # 2) Exact schema cost: identical messages with vs without tools, max_tokens=1,
            #    sent directly so the transport's failure tracking is not involved.
            if tools:
                with_tools = await _prompt_tokens(url, cand_headers, model, request.get("messages") or messages, tools)
                without = await _prompt_tokens(url, cand_headers, model, request.get("messages") or messages, None)
                if with_tools and without:
                    call["exact_schema_tokens"] = with_tools - without
                    call["exact_prompt_tokens_with_tools"] = with_tools
        captured["calls"].append(call)
        yield 'data: {"delta": "(routing replay: model call captured)"}\n\n'
        yield "data: [DONE]\n\n"

    session = SimpleNamespace(endpoint_url=ENDPOINT_URL, model=MODEL, headers=dict(headers or {}),
                              name="routing replay", history=[], add_message=lambda m: None,
                              endpoint_id=ENDPOINT_ID, active_directory_ids=[],
                              primary_local_directory_id=None, cwd=None)
    session_manager = SimpleNamespace(get_session=lambda sid: session, save_sessions=lambda: None)
    context = SimpleNamespace(
        user=OWNER, messages=[{"role": "user", "content": message}],
        route_messages=[{"role": "user", "content": message}],
        preprocessed=SimpleNamespace(attachment_meta=[]), auto_opened_docs=[], rag_sources=[],
        web_sources=[], used_memories=[], uploaded_files=[], uprefs={}, was_compacted=False,
        context_trimmed=False, context_length=32768, context_messages_before_trim=1,
        context_messages_after_trim=1, context_tokens_before_trim=10, context_tokens_after_trim=10,
        preset=SimpleNamespace(temperature=0.6, max_tokens=4096, character_name=None),
    )

    async def fake_context(*args, **kwargs):
        return context

    with mp.context() as p:
        for name, value in {
            "coerce_message_and_session": lambda *a, **k: (message, "routing-replay"),
            "_verify_session_owner": lambda *a, **k: None,
            "effective_user": lambda request: OWNER,
            "_clear_orphaned_session_endpoint": lambda *a, **k: False,
            "_recover_empty_session_model": lambda *a, **k: False,
            "_enforce_chat_privileges": lambda *a, **k: None,
            "resolve_session_auth": lambda *a, **k: None,
            "get_session_mode": lambda sid: "agent",
            "set_session_mode": lambda *a, **k: None,
            "build_chat_context": fake_context,
            "_is_image_generation_session": lambda *a, **k: False,
            "save_assistant_response": lambda *a, **k: None,
            "run_post_response_tasks": lambda *a, **k: None,
            "accumulate_token_usage": lambda *a, **k: None,
        }.items():
            p.setattr(chat_routes, name, value)
        p.setattr(agent_loop, "stream_llm_with_fallback", capture)
        from src.chat_processor import ChatProcessor
        processor = SimpleNamespace(_requests_personal_overview=ChatProcessor._requests_personal_overview)
        router = chat_routes.setup_chat_routes(session_manager, SimpleNamespace(), processor,
                                               SimpleNamespace(), SimpleNamespace(), SimpleNamespace())
        endpoint = next(r.endpoint for r in router.routes if r.path == "/api/chat_stream")
        started_at = time.monotonic()
        response = await endpoint(_Request(message, web=web))
        trace = routing_trace.current()
        visible = []
        async for chunk in response.body_iterator:
            for line in str(chunk if isinstance(chunk, str) else chunk.decode("utf-8", "ignore")).splitlines():
                data = _parse_chunk(line + "\n")
                if isinstance(data, dict) and isinstance(data.get("delta"), str) and not data.get("thinking"):
                    visible.append(data["delta"])
                elif isinstance(data, dict) and data.get("type") == "final_response" and isinstance(data.get("content"), str):
                    visible = [data["content"]]
        captured["visible"] = "".join(visible)[:300]
    try:  # flush the debug store's writer thread so every replayed turn is persisted
        from src.agent_runtime.debug_trace import get_store
        get_store().queue.join()
    except Exception:
        pass
    summary = trace.summary() if trace is not None else {}
    summary["_routing_ms"] = captured.get("routing_ms")
    return summary, captured["calls"], captured.get("visible", "")


_WEB_EGRESS_FOR_REPLAY = {"web_search", "web_fetch", "get_weather", "private_browser", "youtube_tool",
                          "search_hf_models", "trigger_research"}


def _score(case, summary, calls, *, web=True, visible=""):
    advertised = set()
    for r in summary.get("rounds") or ():
        advertised |= set(r.get("tools") or ())
    if not advertised:
        for c in calls:
            advertised |= set(c.get("tools") or ())
    need = case["need"] if web else case.get("need_noweb", NOWEB_NEED.get(case["kind"], case["need"]))
    refused = not calls
    if need == "refuse":
        satisfied, missing = refused, ([] if refused else ["<expected refusal>"])
    else:
        satisfied = (not need) or any(set(option) <= advertised for option in need)
        missing = [] if satisfied else sorted(min((set(o) - advertised for o in need), key=len))
    tools = summary.get("tools") or {}
    why = {name: (tools.get(name) or {}).get("why", "not_registered") for name in missing if not name.startswith("<")}
    first = calls[0] if calls else {}
    live = None
    if "live_first_tools" in first:
        got = first["live_first_tools"][:1] or (["none"] if first.get("live_answered_directly") else ["empty"])
        acceptable = case["first"] if web else (
            ["vault_knowledge_search", "research_parallel", "grep", "dispatch_dottie", "glob", "read_file"]
            if case["kind"] == "combined" else case["first"])
        live = {"first": got[0], "ok": got[0] in acceptable,
                "prompt_tokens": first.get("live_prompt_tokens"),
                "exact_schema_tokens": first.get("exact_schema_tokens"),
                "seconds": first.get("live_seconds"), "text": first.get("live_text"),
                "errors": first.get("live_errors")}
    return {
        "satisfied": satisfied, "missing": missing, "missing_why": why,
        "advertised": sorted(advertised), "advertised_count": len(advertised),
        "families": (summary.get("contract") or {}).get("families"),
        "seal": (summary.get("contract") or {}).get("request_seal"),
        "schema_tokens_estimate": (summary.get("tokens") or {}).get("first_round_schema_estimate"),
        "context_limit": (summary.get("tokens") or {}).get("context_limit"),
        "path": [c.get("path") for c in calls],
        "research_parallel": tools.get("research_parallel"),
        "refused": refused, "routing_ms": summary.get("_routing_ms"),
        # Explicit web-off must never advertise a web-capable tool.
        "web_leak": [] if web else sorted(advertised & _WEB_EGRESS_FOR_REPLAY),
        "live": live,
    }


def run_isolated(args):
    """One process per case and toggle, so a failed model reply cannot leave
    transport state behind that affects later cases. Same measurement method."""
    import subprocess
    scratch = Path(args.scratch or tempfile.mkdtemp(prefix="routing-replay-"))
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
    toggles = (True,) if args.web_on_only else (False,) if args.web_off_only else (True, False)
    results = []
    for web in toggles:
        for case in cases:
            sub = scratch / f"iso_{case['id']}_{'web' if web else 'noweb'}"
            cmd = [sys.executable, __file__, "--only", case["id"], "--scratch", str(sub),
                   "--out", str(sub / "r.json"), "--web-on-only" if web else "--web-off-only"]
            if args.live:
                cmd.append("--live")
            if args.legacy_core:
                cmd.append("--legacy-core")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            line = next((l for l in proc.stdout.splitlines() if l.startswith(("web ", "noweb "))), "")
            print(line or f"{'web' if web else 'noweb'} {case['id']}: run failed (exit {proc.returncode})", flush=True)
            try:
                results += json.loads((sub / "r.json").read_text())["results"]
            except (OSError, ValueError, KeyError):
                pass
            shutil.rmtree(sub, ignore_errors=True)
    out = Path(args.out) if args.out else scratch / "routing_replay_report.json"
    out.write_text(json.dumps({"model": MODEL, "endpoint_id": ENDPOINT_ID, "cases": CASES, "results": results}, indent=2))
    print(f"\nreport: {out}")


async def main_async(args):
    scratch = Path(args.scratch or tempfile.mkdtemp(prefix="routing-replay-"))
    data_dir = scratch / "replay_data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    _copy_data(ROOT / "data", data_dir, include_app_key=args.live)
    import atexit
    if not args.keep_data:
        atexit.register(_remove_data, data_dir)
    try:
        chat_routes, agent_loop = _setup(data_dir)
        if args.legacy_core:  # evaluation-only A/B: restore the pre-Stage-2 conversational core
            from src import turn_contract
            turn_contract.CONVERSATIONAL_CORE_TOOLS = turn_contract.CONTRACT_CORE_TOOLS
        import pytest
        mp = pytest.MonkeyPatch()
        original_stream = agent_loop.stream_llm_with_fallback
        headers = _live_headers() if args.live else {}
        for name in _SECRET_FILES:  # headers are in memory now; drop the key file
            (data_dir / name).unlink(missing_ok=True)
        results = []
        cases = [c for c in CASES if not args.only or c["id"] in args.only.split(",")]
        toggles = (True,) if args.web_on_only else (False,) if args.web_off_only else (True, False)
        for web in toggles:
            for case in cases:
                live = args.live
                summary, calls, visible = await _run_case(case, web=web, live=live, chat_routes=chat_routes,
                                                 agent_loop=agent_loop, mp=mp, headers=headers,
                                                 original_stream=original_stream)
                row = {"id": case["id"], "kind": case["kind"], "web_toggle": web,
                       **_score(case, summary, calls, web=web, visible=visible),
                       "profile": summary.get("profile"), "trace_errors": summary.get("trace_errors"),
                       "visible_reply": visible if not calls else None}
                results.append(row)
                live_txt = ""
                if row["live"]:
                    lv = row["live"]
                    live_txt = f"  gyro-s first={lv['first']} {'OK' if lv['ok'] else 'WRONG'} exact_schema={lv['exact_schema_tokens']}"
                print(f"{'web' if web else 'noweb':<5} {case['id']:<22} {'OK  ' if row['satisfied'] else 'MISS'} "
                      f"n={row['advertised_count']:>2} ~{row['schema_tokens_estimate']}tok route={row['routing_ms']}ms "
                      f"ws={(row['profile'] or {}).get('workspace_bound')}/{(row['profile'] or {}).get('vault_scope')} "
                      f"fam={row['families']} {'LEAK=' + str(row['web_leak']) + ' ' if row['web_leak'] else ''}"
                      f"seal={row['seal']} missing={row['missing_why'] or ''}{live_txt}", flush=True)
        report = {"model": MODEL, "endpoint_id": ENDPOINT_ID, "cases": CASES, "results": results}
        out = Path(args.out) if args.out else scratch / "routing_replay_report.json"
        out.write_text(json.dumps(report, indent=2))
        print(f"\nreport: {out}")
    finally:
        if not args.keep_data:
            _remove_data(data_dir)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--web-on-only", action="store_true")
    ap.add_argument("--web-off-only", action="store_true")
    ap.add_argument("--isolate", action="store_true",
                    help="run each case/toggle in its own process (recommended with --live)")
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--scratch", help="directory for the scratch data copy and report")
    ap.add_argument("--out")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("--legacy-core", action="store_true",
                    help="evaluation only: offer the old conversational core (bash/python/vault_batch_edit)")
    args = ap.parse_args(argv)
    if args.isolate:
        run_isolated(args)
    else:
        asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
