"""Persona sidebar timeline: what Mabel has been up to, newest first.

Nothing new is recorded for this; it merges what already exists:
- curiosity sessions (what she chose and why, what she wrote, her verdicts on
  Dottie's suggestions) from ``data/curiosity/sessions``;
- heartbeat wakes, yields and finishes from the Phase 0 ledger;
- decisions on Dottie's inbox (by her or by you) and Dottie's loop runs;
- mood changes (affect residue) and whisper changes.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LEDGER_TAIL = 4000
LIMIT = 150
LOOP_NAMES = {"archivist": "Archivist", "scout": "Scout", "dreamer": "Dreamer", "sentinel": "Sentinel"}


def _when(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _item(at, kind, title, detail="", ref=None) -> dict | None:
    dt = _when(at)
    return {"at": dt.isoformat(), "kind": kind, "title": title, "detail": detail, "ref": ref} if dt else None


def _ledger(data_dir: Path) -> list[dict]:
    path = data_dir / "phase0" / "events.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-LEDGER_TAIL:]
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _sessions(data_dir: Path, since: datetime) -> list[dict]:
    items = []
    for path in sorted((data_dir / "curiosity" / "sessions").glob("cs-*.json")):
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        steps = state.get("steps") or {}
        choice, write = steps.get("choice") or {}, steps.get("write") or {}
        if choice and (_when(choice.get("chosen_at")) or since) >= since:
            label = {"open_question": "one of her own open questions", "dottie": "something Dottie noticed",
                     "own_question": "her own new question", "surprise": "a random pick",
                     "spawned": "a link from an earlier article"}.get(choice.get("kind"), "a link in your notes")
            items.append(_item(choice.get("chosen_at"), "research_choice", f"Chose to research {choice.get('subject')}",
                               f"From {label}. {choice.get('question', '')} Why: {choice.get('why', '')}", state["id"]))
            verdicts = [d for d in choice.get("dottie_decisions") or [] if d.get("decision") in ("reject", "keep")]
            if verdicts:
                text = "; ".join(f"{'rejected' if d['decision'] == 'reject' else 'kept'} “{d['title']}”"
                                 + (f" ({d['reason']})" if d.get("reason") else "") for d in verdicts)
                items.append(_item(choice.get("chosen_at"), "inbox_review", "Went through Dottie's suggestions", text))
        if write and (_when(state.get("updated_at")) or since) >= since:
            whisper = {"set": " and made it her whisper", "replaced_automatic": " and made it her whisper"}.get(
                write.get("whisper_outcome"), "")
            items.append(_item(state.get("updated_at"), "article", f"Wrote “{Path(write['article']).stem}”",
                               f"{write.get('status', '')}, {len(write.get('verified') or [])}/{len(write.get('cited') or [])} "
                               f"cited sources verified{whisper}.", state["id"]))
    return [i for i in items if i]


def build(*, data_dir: Path, days: int = 7, limit: int = LIMIT, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    items: list[dict | None] = list(_sessions(data_dir, since))

    for event in _ledger(data_dir):
        at, kind = event.get("occurred_at"), event.get("kind")
        if not (_when(at) and _when(at) >= since):
            continue
        if kind == "job.claimed":
            idle = str(event.get("rationale_code") or "").removeprefix("idle_").removesuffix("m")
            verb = "Woke up on her own to finish a session" if event.get("decision") == "resumed" else "Woke up on her own"
            items.append(_item(at, "wake", verb, f"You'd been away {idle} min and the GPU was free.", event.get("job_id")))
        elif kind == "job.yielded":
            items.append(_item(at, "yield", "Paused her research",
                               str(event.get("rationale_code") or "").replace("_", " ") + "; she'll finish it later.",
                               event.get("job_id")))
        elif kind == "job.completed":
            items.append(_item(at, "done", "Finished her session and went back to sleep", "", event.get("job_id")))
        elif kind == "job.failed":
            items.append(_item(at, "failed", "A research session failed",
                               str(event.get("rationale_code") or "").replace("_", " "), event.get("job_id")))

    try:
        from src.subconscious.inbox import Inbox
        inbox = Inbox(data_dir / "subconscious" / "inbox.sqlite")
        everything = [i for s in ("new", "accepted", "rejected", "done", "expired") for i in inbox.list(status=s, limit=200)]
    except Exception:
        everything = []
    runs: dict[tuple, list] = defaultdict(list)
    for entry in everything:
        created = _when(entry["created_at"])
        if created and created >= since:
            runs[(entry["loop"], created.replace(minute=created.minute // 10 * 10, second=0, microsecond=0))].append(entry)
        decided = _when(entry.get("decided_at"))
        if decided and decided >= since and entry.get("decided_by") == "user" and entry["status"] in ("accepted", "rejected"):
            items.append(_item(entry["decided_at"], "inbox_user",
                               f"You {'kept' if entry['status'] == 'accepted' else 'rejected'} “{entry['title'][:80]}”",
                               entry.get("decision_reason") or ""))
    for (loop, bucket), entries in runs.items():
        kinds = defaultdict(int)
        for e in entries:
            kinds[e["kind"].replace("_", " ")] += 1
        summary = ", ".join(f"{n} {k}{'s' if n > 1 else ''}" for k, n in kinds.items())
        items.append(_item(min(e["created_at"] for e in entries), "dottie",
                           f"Dottie's {LOOP_NAMES.get(loop, loop)} found {len(entries)} thing{'s' if len(entries) > 1 else ''}",
                           summary))

    try:
        affect = json.loads((data_dir / "persona" / "affect.json").read_text())
        for r in affect.get("residue") or []:
            source = {"time_of_day": "time of day", "conversation_warmup": "warmed up by chatting",
                      "conversation_lively": "a lively conversation", "background_busy": "busy background work",
                      "manual": "pinned by you"}.get(r.get("source"), r.get("source", ""))
            if (_when(r.get("at")) or since) >= since:
                items.append(_item(r.get("at"), "mood", f"Mood: {r.get('mood')}", source))
    except (OSError, ValueError):
        pass

    try:
        whispers = json.loads((data_dir / "persona" / "whispers.json").read_text())
        for w in ([whispers.get("active")] if whispers.get("active") else []) + (whispers.get("history") or []):
            origin = "seeded by you" if w.get("origin") == "user_seeded" else f"her own, from {w.get('source', 'somewhere')}"
            items.append(_item(w.get("created_at"), "whisper", f"New curiosity whisper ({origin})", w.get("thought", "")))
    except (OSError, ValueError, AttributeError):
        pass

    try:
        outreach = json.loads((data_dir / "initiative" / "outreach.json").read_text())
        for sent in outreach.get("sent") or []:
            how = "on your phone" if sent.get("status") in ("sent", "answered") else "in a chat (the phone notification failed)"
            items.append(_item(sent.get("at"), "outreach", f"Messaged you {how} about {sent.get('subject')}",
                               f"“{sent.get('message', '')}” Why: {sent.get('why', '')}", sent.get("session_id")))
            if sent.get("answered_at"):
                items.append(_item(sent["answered_at"], "outreach_reply", f"You replied about {sent.get('subject')}", ""))
        for review in outreach.get("reviews") or []:
            if review.get("decision") == "silence":
                items.append(_item(review.get("at"), "silence", "Thought about messaging you, and chose not to",
                                   review.get("why", "")))
    except (OSError, ValueError):
        pass

    items = [i for i in items if i and _when(i["at"]) >= since]
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]
