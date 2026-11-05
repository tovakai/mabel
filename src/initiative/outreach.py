"""Phase 6: Mabel occasionally starts a conversation because she has something to bring up.

Pipeline (each step recorded in the ledger: candidate -> review -> send/hold/drop -> response):

1. candidates: her recent verified research (its open questions and her take),
   her own whisper, and conversation patterns Dottie noticed that she kept;
   a rule-based filter drops anything already shared or already talked about.
2. gates (deterministic): on and not paused, at most MAX_PER_DAY a day and
   MIN_GAP apart, quiet for COOLDOWN after an unanswered message, her model
   free (no chat, no research), and a phone to send to.
3. review: Mabel decides whether anything is worth sharing and writes it, or
   chooses silence (a good outcome, not a failure). Nothing is tuned for replies.
4. send: a new chat holds her message with the research attached as context,
   and a phone notification links straight to it.
5. response: replying in that chat marks the outreach answered.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_PER_DAY = 2
MIN_GAP = timedelta(hours=3)
COOLDOWN = timedelta(hours=48)          # after an unanswered message
REVIEW_GAP = timedelta(hours=12)        # re-review an unchanged candidate set at most this often
ARTICLE_WINDOW = timedelta(days=3)
PATTERN_WINDOW = timedelta(days=7)
MESSAGE_LIMIT = 500
MAX_CANDIDATES = 6
_REPO = Path(__file__).resolve().parents[2]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def _when(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# --- store ---------------------------------------------------------------------------------

class Store:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict):
                data.setdefault("sent", [])
                data.setdefault("reviews", [])
                return data
        except (OSError, ValueError):
            pass
        return {"sent": [], "reviews": []}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data["sent"] = data["sent"][-200:]
        data["reviews"] = data["reviews"][-200:]
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".outreach-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)


def default_store() -> Store:
    from src.constants import DATA_DIR
    return Store(Path(DATA_DIR) / "initiative" / "outreach.json")


def _ledger(kind: str, ref: str, decision: str | None = None, code: str | None = None,
            references: tuple = ()) -> None:
    try:
        from src.agent_runtime.event_ledger import Event, Ledger
        from src.constants import DATA_DIR
        token = re.compile(r"[^A-Za-z0-9_.:-]+")
        refs = tuple(token.sub("_", r)[:128] for r in ((f"outreach:{ref}",) + tuple(references)))[:16]
        Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
            "mabel", kind, references=refs, decision=decision,
            rationale_code=token.sub("_", code)[:64] if code else None))
    except Exception:
        logger.debug("[outreach] ledger event not written", exc_info=True)


# --- candidates ----------------------------------------------------------------------------

def _distinctive(text: str) -> set[str]:
    from src.subconscious.stale import distinctive
    return distinctive(text)


def recently_talked_about(subject: str, *, owner: str, now: datetime, hours: int = 48) -> bool:
    """He already brought it up in chat: no need to message him about it."""
    try:
        from src.subconscious.dreamer import recent_messages
        words = _distinctive(subject)
        if not words:
            return False
        for m in recent_messages(owner, now=now, days=max(1, hours // 24)):
            if len(words & _distinctive(m["text"])) >= min(2, len(words)):
                return True
    except Exception:
        return False
    return False


def gather(*, owner: str, now: datetime | None = None, mabel_vault: Path = _REPO, data_dir: Path | None = None) -> list[dict]:
    now = now or _now()
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    shared = {s.get("key") for s in default_store_for(data_dir).load()["sent"]}
    out: list[dict] = []
    try:
        from src.curiosity.backlog import articles
        from src.curiosity.verify import _section
        for art in articles(mabel_vault / "knowledge" / "research"):
            created = _when(art.get("created"))
            if art.get("status") != "verified" or not created or now - created > ARTICLE_WINDOW:
                continue
            text = art["path"].read_text(encoding="utf-8")
            take = " ".join(_section(text, "What I make of it").split())[:700]
            out.append({"key": f"article:{art['path'].name}", "source": "research", "subject": art.get("subject"),
                        "title": art["path"].stem, "question": art.get("question"), "take": take,
                        "open_questions": (art.get("open_questions") or [])[:3],
                        "article": f"knowledge/research/{art['path'].name}", "at": art.get("created")})
    except Exception:
        logger.debug("[outreach] article candidates unavailable", exc_info=True)
    try:
        whispers = json.loads((data_dir / "persona" / "whispers.json").read_text())
        w = whispers.get("active")
        if w and w.get("origin") == "self_generated" and not any(c.get("article") == w.get("reference") for c in out):
            out.append({"key": f"whisper:{w['id']}", "source": "whisper", "subject": w.get("topic"),
                        "title": w.get("topic"), "take": w.get("thought"), "at": w.get("created_at")})
    except (OSError, ValueError, AttributeError):
        pass
    try:
        from src.subconscious.inbox import Inbox
        inbox = Inbox(data_dir / "subconscious" / "inbox.sqlite")
        for item in inbox.list(status="new", kind="pattern", limit=10):
            created = _when(item["created_at"])
            if created and now - created <= PATTERN_WINDOW and item["payload"].get("curiosity"):
                out.append({"key": f"pattern:{item['id']}", "source": "conversation", "subject": item["title"],
                            "title": item["title"], "take": item["payload"]["curiosity"],
                            "observation": item.get("summary"), "at": item["created_at"]})
    except Exception:
        pass
    try:                                                    # her account of his recent days
        import yaml
        folder = mabel_vault / "journal" / "anthon"
        for path in sorted(folder.glob("*.md"))[-2:]:
            text = path.read_text(encoding="utf-8")
            meta = yaml.safe_load(text.split("\n---\n", 1)[0].removeprefix("---\n")) if text.startswith("---\n") else {}
            written = _when((meta or {}).get("written"))
            if not written or now - written > timedelta(days=2):
                continue
            from src.curiosity.verify import _section
            for i, question in enumerate((meta or {}).get("curious") or []):
                out.append({"key": f"life:{path.stem}:{i}", "source": "life", "subject": question[:80],
                            "title": f"your day ({path.stem})", "take": question,
                            "observation": " ".join(_section(text, "What stood out to me").split())[:500],
                            "article": f"journal/anthon/{path.name}", "at": meta.get("written")})
    except Exception:
        logger.debug("[outreach] life candidates unavailable", exc_info=True)
    kept = []
    for c in out:
        if c["key"] in shared:
            continue                                        # already shared
        if recently_talked_about(str(c.get("subject") or ""), owner=owner, now=now):
            continue                                        # he already brought it up
        kept.append(c)
    kept.sort(key=lambda c: str(c.get("at") or ""), reverse=True)
    return kept[:MAX_CANDIDATES]


def default_store_for(data_dir: Path) -> Store:
    return Store(Path(data_dir) / "initiative" / "outreach.json")


# --- gates ---------------------------------------------------------------------------------

def gates(data: dict, *, now: datetime, enabled: bool, paused: bool, model_free: bool, has_phone: bool) -> list[str]:
    """Reasons not to reach out right now (empty = all clear)."""
    reasons = []
    if not enabled:
        reasons.append("reaching out is switched off")
    if paused:
        reasons.append("paused by you")
    if not has_phone:
        reasons.append("no phone to send to")
    if not model_free:
        reasons.append("her model is busy (a chat or research is running)")
    sent = [s for s in data["sent"] if s.get("status") in ("sent", "answered", "in_app_only")]
    local_day = now.astimezone().date()
    today = [s for s in sent if (_when(s["at"]) or now).astimezone().date() == local_day]
    if len(today) >= MAX_PER_DAY:
        reasons.append(f"already {len(today)} messages today")
    if sent:
        last = max(sent, key=lambda s: s["at"])
        since = now - (_when(last["at"]) or now)
        if since < MIN_GAP:
            reasons.append(f"last message {int(since.total_seconds() // 60)} min ago")
        if not last.get("answered_at") and since < COOLDOWN:
            reasons.append(f"waiting: her last message is unanswered ({int(since.total_seconds() // 3600)} h)")
    return reasons


def needs_review(data: dict, candidates: list[dict], *, now: datetime) -> bool:
    """Only ask her again when something new is on the table, or after REVIEW_GAP."""
    if not candidates:
        return False
    keys = {c["key"] for c in candidates}
    seen = set()
    last_at = None
    for r in data["reviews"]:
        seen.update(r.get("keys", []))
        last_at = max(last_at or r["at"], r["at"])
    if keys - seen:
        return True
    return last_at is None or now - (_when(last_at) or now) >= REVIEW_GAP


# --- review --------------------------------------------------------------------------------

def _describe(i: int, c: dict) -> str:
    bits = [f"{i}. [{c['source']}] {c.get('title')}"]
    if c.get("question"):
        bits.append(f"   You researched: {c['question']}")
    if c.get("take"):
        bits.append(f"   What stuck with you: {c['take'][:500]}")
    if c.get("observation"):
        bits.append(f"   Dottie noticed: {c['observation']}")
    for q in c.get("open_questions") or []:
        bits.append(f"   Left open: {q}")
    return "\n".join(bits)


async def review(candidates: list[dict], *, owner: str, ask=None) -> dict:
    """Mabel chooses one thing to message about, or nothing. Returns her decision."""
    listing = "\n\n".join(_describe(i, c) for i, c in enumerate(candidates, 1))
    prompt = (
        "You could send Anthon a message on his phone right now, just because you want to. He isn't "
        "expecting it. Here's what's been on your mind:\n\n" + listing + "\n\n"
        "Pick the one thing (if any) you'd genuinely like to share or ask him about, or decide not to "
        "message at all; staying quiet is a perfectly good choice when nothing feels worth interrupting him "
        "for. If you write, make it a text from you: one to three sentences, casual, in your own voice, "
        "about the thing itself (maybe a question for him). Don't apologise for texting, don't be needy, "
        "and don't ask him to reply.\n"
        'Reply with only JSON: {"send": true or false, "pick": <number>, "message": "...", "why": "one honest sentence"}')
    system = "You are Mabel."
    try:
        from src.persona import composer
        library = composer().modules.load()
        if library.core:
            system = library.core.body
    except Exception:
        pass
    reply = await (ask or _ask_mabel)([{"role": "system", "content": system}, {"role": "user", "content": prompt}], owner)
    from src.curiosity.session import _json_object
    data = _json_object(reply)
    why = " ".join(str(data.get("why") or "").split())[:300]
    if not data.get("send"):
        return {"send": False, "why": why or "nothing felt worth it"}
    try:
        pick = int(data.get("pick"))
    except (TypeError, ValueError):
        return {"send": False, "why": "no valid choice"}
    message = " ".join(str(data.get("message") or "").split())
    from src.persona.modules import _AUTHORITY_CONFLICT
    if not 1 <= pick <= len(candidates) or not message or len(message) > MESSAGE_LIMIT \
            or _AUTHORITY_CONFLICT.search(message):
        return {"send": False, "why": "the drafted message didn't pass the checks"}
    return {"send": True, "candidate": candidates[pick - 1], "message": message, "why": why}


async def _ask_mabel(messages: list[dict], owner: str) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=2500, temperature=0.8, timeout=180)


# --- send ----------------------------------------------------------------------------------

def notify_service() -> str | None:
    """The configured phone notify service, or the only mobile_app one Home Assistant offers."""
    configured = str(_setting("initiative_notify_service", "") or "").strip()
    if configured:
        return configured if re.fullmatch(r"[a-z0-9_]+", configured) else None
    ha = home_assistant()
    if not ha:
        return None
    try:
        import httpx
        services = httpx.get(f"{ha['base_url'].rstrip('/')}/api/services",
                             headers={"Authorization": f"Bearer {ha['api_key']}"}, timeout=5).json()
        names = sorted(n for d in services if d.get("domain") == "notify"
                       for n in d.get("services", {}) if n.startswith("mobile_app_"))
        return names[0] if len(names) == 1 else None
    except Exception:
        return None


def home_assistant() -> dict | None:
    try:
        from src.integrations import load_integrations
        return next((i for i in load_integrations() if i.get("enabled") and i.get("base_url") and i.get("api_key") and (
            i.get("preset") == "homeassistant"
            or re.sub(r"[^a-z]", "", str(i.get("name") or "").lower()) in ("homeassistant", "hassio", "hass"))), None)
    except Exception:
        return None


def _context_payload(outreach_id: str, c: dict, mabel_vault: Path) -> dict:
    """Research context attached to her message, so a reply continues with it."""
    report = ""
    if c.get("article"):
        try:
            text = (mabel_vault / c["article"]).read_text(encoding="utf-8")
            report = text.split("\n---\n", 1)[1] if text.startswith("---\n") else text
        except OSError:
            report = ""
    if not report:
        report = "\n".join(str(c.get(k) or "") for k in ("title", "observation", "take") if c.get(k))
    return {"job_id": outreach_id, "query": str(c.get("subject") or c.get("title") or ""),
            "report": report[:12000], "sources": [], "rounds": 0, "error": False}


def create_chat(owner: str, title: str, message: str, metadata: dict) -> str:
    from core.models import ChatMessage
    from src.ai_interaction import get_session_manager
    from src.endpoint_resolver import resolve_endpoint_by_id
    from src.model_roles import candidates as role_candidates
    sm = get_session_manager()
    endpoint_id = str(_setting("default_endpoint_id", "") or "")
    entry = role_candidates("default", owner)[0]
    url, model, headers = resolve_endpoint_by_id(endpoint_id, entry.model, owner=owner)
    session_id = str(uuid.uuid4())
    sm.create_session(session_id=session_id, name=title[:60], endpoint_url=url, model=model, owner=owner,
                      headers=headers or None, endpoint_id=endpoint_id or None)
    sm.add_message(session_id, ChatMessage("assistant", message, metadata={"model": model, **metadata}))
    return session_id


def send_notification(message: str, link: str, tag: str, post=None) -> bool:
    ha, service = home_assistant(), notify_service()
    if not ha or not service:
        return False
    try:
        if post is None:
            import httpx
            post = httpx.post
        response = post(f"{ha['base_url'].rstrip('/')}/api/services/notify/{service}",
                        headers={"Authorization": f"Bearer {ha['api_key']}"},
                        json={"title": "Mabel", "message": message,
                              "data": {"clickAction": link, "url": link, "tag": tag, "group": "mabel",
                                       "channel": "Mabel"}}, timeout=10)
        return 200 <= response.status_code < 300
    except Exception:
        logger.warning("[outreach] notification failed", exc_info=True)
        return False


def chat_link(session_id: str) -> str:
    base = str(_setting("initiative_public_url", "") or "").strip().rstrip("/") or "https://mabel.example.ts.net"
    return f"{base}/#{session_id}"


async def run_once(*, owner: str, now: datetime | None = None, force_review: bool = False, ask=None,
                   model_free: bool | None = None, store: Store | None = None, mabel_vault: Path = _REPO) -> dict:
    """One pass: gather -> gates -> (maybe) review -> send or stay quiet."""
    now = now or _now()
    store = store or default_store()
    data = store.load()
    if model_free is None:
        from src.curiosity import runner
        from src.heartbeat import signals
        model_free = not signals.any_chat_active() and not runner.status().get("running") and signals.strata_up()
    blocked = gates(data, now=now, enabled=bool(_setting("initiative_enabled", True)),
                    paused=bool(_setting("initiative_paused", False)), model_free=model_free,
                    has_phone=bool(home_assistant()))
    if blocked:
        return {"status": "gated", "reasons": blocked}
    candidates = gather(owner=owner, now=now, mabel_vault=mabel_vault, data_dir=store.path.parent.parent)
    if not candidates:
        return {"status": "nothing_to_share"}
    if not force_review and not needs_review(data, candidates, now=now):
        return {"status": "already_considered"}
    review_id = hashlib.sha256(f"{now.isoformat()}".encode()).hexdigest()[:10]
    _ledger("outreach.reviewed", review_id, references=tuple(c["key"] for c in candidates))
    decision = await review(candidates, owner=owner, ask=ask)
    record = {"at": now.isoformat(), "id": review_id, "keys": [c["key"] for c in candidates],
              "decision": "send" if decision["send"] else "silence", "why": decision["why"]}
    if not decision["send"]:
        data["reviews"].append(record)
        store.save(data)
        _ledger("outreach.dropped", review_id, decision="silence", code="chose_silence")
        return {"status": "silence", "why": decision["why"]}
    cand = decision["candidate"]
    outreach_id = f"out-{uuid.uuid4().hex[:10]}"
    title = f"💭 {cand.get('title') or cand.get('subject')}"
    session_id = create_chat(owner, title, decision["message"], {
        "outreach_id": outreach_id, "outreach_source": cand["source"],
        "background_tool_result": _context_payload(outreach_id, cand, mabel_vault)})
    link = chat_link(session_id)
    delivered = send_notification(decision["message"], link, outreach_id)
    record["outreach"] = outreach_id
    data["reviews"].append(record)
    data["sent"].append({"id": outreach_id, "at": now.isoformat(), "key": cand["key"], "source": cand["source"],
                         "subject": cand.get("title") or cand.get("subject"), "message": decision["message"],
                         "why": decision["why"], "session_id": session_id,
                         "status": "sent" if delivered else "in_app_only", "answered_at": None})
    store.save(data)
    _ledger("outreach.sent" if delivered else "outreach.held", outreach_id,
            decision="sent" if delivered else "in_app_only", code=cand["source"])
    logger.info("[outreach] Mabel reached out about %r (%s)", cand.get("title"),
                "phone notified" if delivered else "notification failed; chat only")
    return {"status": "sent" if delivered else "in_app_only", "outreach": outreach_id, "session_id": session_id,
            "message": decision["message"], "link": link}


def note_reply(session_id: str, store: Store | None = None) -> bool:
    """A user message in an outreach chat: mark it answered (once)."""
    store = store or default_store()
    data = store.load()
    for s in data["sent"]:
        if s.get("session_id") == session_id and not s.get("answered_at"):
            s["answered_at"] = _now().isoformat()
            s["status"] = "answered"
            store.save(data)
            _ledger("outreach.responded", s["id"], decision="answered", code=s.get("source"))
            return True
    return False


# --- background loop -----------------------------------------------------------------------

import asyncio  # noqa: E402

LOOP_START_DELAY_S = 300
LOOP_EVERY_S = 20 * 60
_task: asyncio.Task | None = None
last_result: dict = {}


async def loop(owner: str = "tovakai") -> None:
    await asyncio.sleep(LOOP_START_DELAY_S)
    while True:
        try:
            last_result.clear()
            last_result.update(await run_once(owner=owner), at=_now().isoformat())
        except Exception:
            logger.warning("[outreach] pass failed", exc_info=True)
        await asyncio.sleep(LOOP_EVERY_S)


def consider_now(owner: str = "tovakai") -> bool:
    """Ask her now (gates still apply). False if a pass is already running."""
    global _task
    if _task and not _task.done():
        return False

    async def once():
        last_result.clear()
        last_result.update(await run_once(owner=owner, force_review=True), at=_now().isoformat())
    _task = asyncio.get_running_loop().create_task(once())
    return True
