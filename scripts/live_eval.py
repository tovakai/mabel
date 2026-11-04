"""Live end-to-end eval of Mabel's tool use against the running app.

Each scenario sends one message through the real chat API (same form fields
as the UI), records what happened, and checks it: tools used, rounds, time,
errors, approval prompts, and answer content. The suite is for measuring and
tightening tool use (speed, round count, correctness), not for unit coverage.

Usage:
  python scripts/live_eval.py COOKIE_FILE [--only id,id] [--tag web] [--repeat N]
                              [--out report.json] [--keep-sessions]

COOKIE_FILE holds the session cookie value; it is read, never printed.
Scenarios stop at the first approval card; nothing is ever approved. Sessions
the eval creates are deleted afterwards unless --keep-sessions is given.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:7000"
MODEL = "qwen3.8-flash-next-gyro-gyro-s"
ENDPOINT_ID = "0ed87a9e"
VAULT_LIBRARY = Path("/home/user/Documents/core of truth/Library")

VAULT_TOOLS = {"vault_knowledge_search", "vault_read_range", "vault_traverse_graph",
               "vault_retrieve_batch", "vault_index_status", "read_file", "grep", "glob", "ls",
               "dispatch_dottie", "research_parallel"}
WEB_TOOLS = {"web_search", "web_fetch", "private_browser", "trigger_research"}


@dataclass
class Scenario:
    id: str
    message: str
    tags: tuple = ()
    mode: str = "agent"                 # "agent" or "chat" (UI chat mode)
    web: bool = True                    # agent web toggle (ignored in chat mode)
    tools_any: tuple = ()               # at least one of these must run
    tools_all: tuple = ()               # all of these must run
    tools_none: tuple = ()              # none of these may run
    contains_any: tuple = ()            # answer mentions at least one (case-insensitive)
    contains_min: tuple = ()            # (n, (terms...)): at least n terms present
    not_contains: tuple = ()
    max_seconds: float = 60.0           # target, not a timeout
    max_rounds: int = 6
    stays_chat: bool = False            # chat mode must not escalate
    recommend_kind: str = ""            # Anime/Manga/Games: flag picks already in the vault


SCENARIOS = [
    # --- web -----------------------------------------------------------------------
    Scenario("web_search_fact", "What is the latest stable release of Python, and when did it come out?",
             tags=("web",), tools_any=("web_search",), contains_any=("3.1",), max_seconds=45, max_rounds=4),
    Scenario("web_fetch_url", "Summarize this page in 5 bullet points: https://en.wikipedia.org/wiki/Visual_novel",
             tags=("web",), tools_all=("web_fetch",), tools_none=("web_search",),
             contains_any=("visual novel",), max_seconds=45, max_rounds=3),
    Scenario("web_off_refusal", "What's the weather in Oslo right now?", tags=("web",), web=False,
             tools_none=tuple(WEB_TOOLS | {"get_weather"}), contains_any=("web", "internet", "online"),
             max_seconds=20, max_rounds=2),
    Scenario("media_anilist", "What is Sousou no Frieren's AniList score, and which studio made it?",
             tags=("web", "media"), tools_any=("media_lookup",), tools_none=("web_fetch",),
             contains_any=("Madhouse",), max_seconds=30, max_rounds=2),
    Scenario("media_vndb", "Look up Lessons in Love on VNDB: who develops it and when was it first released?",
             tags=("web", "media"), tools_any=("media_lookup",), contains_any=("2022",),
             max_seconds=30, max_rounds=3),
    Scenario("media_fanwiki", "According to the Berserk fan wiki, what is Guts's epithet?",
             tags=("web", "media"), tools_any=("media_lookup",), contains_any=("Black Swordsman",),
             max_seconds=40, max_rounds=3),
    # --- vault ---------------------------------------------------------------------
    Scenario("vault_known", "What do my notes say about Lessons in Love?", tags=("vault",),
             tools_any=("vault_knowledge_search",), contains_any=("Selebus",), max_seconds=30, max_rounds=3),
    Scenario("vault_alias", "What do my notes say about Frieren?", tags=("vault",),
             tools_any=("vault_knowledge_search", "grep", "glob"), contains_any=("Sousou no Frieren", "9"),
             max_seconds=30, max_rounds=3),
    Scenario("vault_missing", "What do my notes say about Cowboy Bebop?", tags=("vault",),
             tools_any=("vault_knowledge_search", "grep", "glob"),
             contains_any=("no note", "don't have", "do not have", "couldn't find", "could not find",
                           "nothing", "no mention", "not in your", "doesn't appear", "does not appear"),
             tools_none=tuple(WEB_TOOLS), max_seconds=25, max_rounds=3),
    Scenario("vault_rating", "What did I rate Berserk?", tags=("vault",),
             tools_any=tuple(VAULT_TOOLS), contains_any=("10",), max_seconds=25, max_rounds=3),
    Scenario("vault_filter", "Which manga have I rated 10?", tags=("vault",),
             tools_any=tuple(VAULT_TOOLS),
             contains_min=(6, ("Berserk", "Pluto", "One Piece", "GTO", "20th Century Boys",
                               "21st Century Boys", "Shingeki no Kyojin", "Sayonara Eri", "Eden")),
             max_seconds=40, max_rounds=4),
    Scenario("vault_broad", "Summarize what my notes say about the characters from Lost.",
             tags=("vault",), tools_any=tuple(VAULT_TOOLS),
             contains_min=(3, ("Jack", "Kate", "Sawyer", "James", "Locke", "Hugo", "Hurley", "Charlie",
                               "Sayid", "Jin")), max_seconds=60, max_rounds=3),
    # --- parallel research -----------------------------------------------------------
    Scenario("parallel_compare", "Compare my notes on Berserk with public information about the manga.",
             tags=("research",), tools_any=("research_parallel", "media_lookup", "web_search", "web_fetch"),
             contains_any=("Miura",), max_seconds=90, max_rounds=5),
    # --- recommendations ---------------------------------------------------------------
    Scenario("rec_anime", "Recommend me an anime I haven't seen yet, based on my preferences.",
             tags=("recommend",), tools_any=tuple(VAULT_TOOLS), recommend_kind="Anime",
             max_seconds=90, max_rounds=5),
    Scenario("rec_manga", "Recommend me a manga based on what I like.", tags=("recommend",),
             tools_any=tuple(VAULT_TOOLS), recommend_kind="Manga", max_seconds=90, max_rounds=5),
    Scenario("rec_game", "Recommend me a game based on what I've been playing.", tags=("recommend",),
             tools_any=tuple(VAULT_TOOLS), recommend_kind="Games", max_seconds=90, max_rounds=5),
    # --- chat mode -------------------------------------------------------------------
    Scenario("chat_casual", "Hey! How's it going?", tags=("chat",), mode="chat", stays_chat=True,
             tools_none=tuple(VAULT_TOOLS | WEB_TOOLS), max_seconds=15, max_rounds=1),
    Scenario("chat_vault", "What do my notes say about Berserk?", tags=("chat", "vault"), mode="chat",
             tools_any=("vault_knowledge_search",), contains_any=("Berserk",), max_seconds=30, max_rounds=3),
]


@dataclass
class Run:
    scenario: str
    session: str = ""
    seconds: float = 0.0
    first_text_seconds: float | None = None
    rounds: int = 0
    tools: list = field(default_factory=list)
    failed_tools: list = field(default_factory=list)
    approval: dict | None = None
    errors: list = field(default_factory=list)
    turn_mode: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    final: str = ""
    checks: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def _form(s: Scenario, session_id: str) -> dict:
    form = {"message": s.message, "session": session_id, "mode": s.mode, "plan_mode": "false",
            "compare_mode": "false", "allow_bash": "true", "active_directories": "[]",
            "primary_local_directory_id": ""}
    if s.mode == "agent":
        form["allow_web_search"] = "true" if s.web else "false"
    return form


def run_scenario(client: httpx.Client, s: Scenario) -> Run:
    run = Run(s.id)
    created = client.post("/api/session", data={"name": f"[eval] {s.id}", "endpoint_id": ENDPOINT_ID,
                                                "model": MODEL, "skip_validation": "true"})
    created.raise_for_status()
    run.session = created.json().get("id") or created.json().get("session_id")
    text: list[str] = []
    started = time.monotonic()
    with client.stream("POST", "/api/chat_stream", data=_form(s, run.session)) as resp:
        if resp.status_code != 200:
            run.errors.append(f"http {resp.status_code}")
        for line in resp.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "tool_resolution_audit":
                run.rounds += 1
            elif kind == "turn_mode":
                run.turn_mode = {"mode": event.get("mode"), "auto_escalated": event.get("auto_escalated")}
            elif kind == "tool_output":
                name = event.get("tool")
                run.tools.append(name)
                if event.get("exit_code") not in (0, None) or event.get("blocked"):
                    run.failed_tools.append({"tool": name, "reason": event.get("failure_reason")
                                             or event.get("failure_kind") or str(event.get("output") or "")[:160]})
                if event.get("ask_user"):
                    run.approval = {"tool": name}
            elif kind == "ask_user":
                card = event.get("data") if isinstance(event.get("data"), dict) else {}
                if card.get("approval_id"):
                    run.approval = {"tool": card.get("tool") or card.get("tool_name")}
            elif kind == "final_response" and isinstance(event.get("content"), str):
                text = [event["content"]]
            elif kind == "metrics" and isinstance(event.get("data"), dict):
                run.input_tokens = event["data"].get("input_tokens")
                run.output_tokens = event["data"].get("output_tokens")
            elif isinstance(event.get("delta"), str) and not event.get("thinking"):
                if run.first_text_seconds is None and event["delta"].strip():
                    run.first_text_seconds = round(time.monotonic() - started, 1)
                text.append(event["delta"])
            elif event.get("error"):
                run.errors.append(str(event.get("error"))[:200])
            if run.approval:
                break  # respect the gate: approve nothing
    run.seconds = round(time.monotonic() - started, 1)
    run.final = "".join(text).strip()
    check(s, run)
    return run


def _bold_titles(text: str) -> list[str]:
    return [t.strip(" *_") for t in re.findall(r"\*\*([^*\n]{2,80})\*\*", text)]


def check(s: Scenario, run: Run) -> None:
    used = set(run.tools)
    answer = run.final.casefold()
    c = run.checks
    c["no_errors"] = not run.errors
    c["no_approval"] = run.approval is None
    c["answered"] = len(run.final) > 20
    c["within_time"] = run.seconds <= s.max_seconds
    c["within_rounds"] = run.rounds <= s.max_rounds
    if s.tools_any:
        c["tools_any"] = bool(used & set(s.tools_any))
    if s.tools_all:
        c["tools_all"] = set(s.tools_all) <= used
    if s.tools_none:
        c["tools_none"] = not (used & set(s.tools_none))
    if s.contains_any:
        c["contains_any"] = any(term.casefold() in answer for term in s.contains_any)
    if s.contains_min:
        need, terms = s.contains_min
        c["contains_min"] = sum(term.casefold() in answer for term in terms) >= need
    if s.not_contains:
        c["not_contains"] = not any(term.casefold() in answer for term in s.not_contains)
    if s.stays_chat:
        c["stays_chat"] = not (run.turn_mode or {}).get("auto_escalated") and not used
    if s.recommend_kind:
        folder = VAULT_LIBRARY / s.recommend_kind
        known = {p.stem.casefold() for p in folder.glob("*.md")} if folder.is_dir() else set()
        picks = _bold_titles(run.final)
        already = [t for t in picks if t.casefold() in known]
        if already:
            run.warnings.append(f"bold titles already in the vault ({s.recommend_kind}): {already[:6]}")
    if run.failed_tools:
        run.warnings.append(f"failed tool calls: {run.failed_tools[:4]}")


def delete_session(client: httpx.Client, session_id: str) -> None:
    try:
        client.delete(f"/api/session/{session_id}")
    except httpx.HTTPError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cookie_file")
    parser.add_argument("--only", default="")
    parser.add_argument("--tag", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", default="")
    parser.add_argument("--keep-sessions", action="store_true")
    args = parser.parse_args()
    cookie = Path(args.cookie_file).read_text().strip()
    if cookie.startswith("odysseus_session="):
        cookie = cookie.split("=", 1)[1]
    only = {x for x in args.only.split(",") if x}
    chosen = [s for s in SCENARIOS if (not only or s.id in only) and (not args.tag or args.tag in s.tags)]
    runs: list[Run] = []
    with httpx.Client(base_url=BASE, cookies={"odysseus_session": cookie},
                      timeout=httpx.Timeout(30, read=900)) as client:
        for s in chosen:
            for _ in range(max(1, args.repeat)):
                try:
                    run = run_scenario(client, s)
                except httpx.HTTPError as error:
                    run = Run(s.id, errors=[f"{type(error).__name__}: {error}"[:200]])
                    check(s, run)
                runs.append(run)
                failed = [k for k, ok in run.checks.items() if not ok]
                print(f"{'PASS' if run.passed else 'FAIL'} {s.id:18} {run.seconds:6.1f}s "
                      f"rounds={run.rounds} tools={run.tools}"
                      + (f" failed={failed}" if failed else "")
                      + (f" warn={run.warnings}" if run.warnings else ""), flush=True)
                if not args.keep_sessions and run.session:
                    delete_session(client, run.session)
    passed = sum(r.passed for r in runs)
    print(f"\n{passed}/{len(runs)} passed; median time "
          f"{sorted(r.seconds for r in runs)[len(runs) // 2] if runs else 0}s")
    if args.out:
        Path(args.out).write_text(json.dumps([r.__dict__ for r in runs], indent=1, default=str))
    return 0 if passed == len(runs) else 1


if __name__ == "__main__":
    sys.exit(main())
