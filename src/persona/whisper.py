"""Phase 2: one Curiosity Whisper, a continuing thought Mabel may bring up.

Manually seeded for now (origin ``user_seeded``); later Dottie suggests and
Mabel selects (``self_generated``). It is served only in social turns, as an
optional thought she can mention if the conversation naturally connects, and
it cools off after it has come up so it cannot take over every chat.

Stored locally in ``data/persona/whispers.json`` (private; never committed).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .modules import _AUTHORITY_CONFLICT

TOPIC_LIMIT = 80
THOUGHT_LIMIT = 400
REFERENCE_LIMIT = 300
DEFAULT_DAYS = 14
COOLDOWN_HOURS = 12        # after it comes up, stay quiet this long
HISTORY_LIMIT = 20
ORIGINS = ("user_seeded", "self_generated")
_STOP = {"the", "and", "for", "with", "about", "into", "from", "that", "this", "how", "what", "why", "its"}
_LOCK = threading.Lock()


class WhisperError(ValueError):
    pass


def _clean(text: str, limit: int, label: str) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        raise WhisperError(f"{label} is required")
    if len(value) > limit:
        raise WhisperError(f"{label} is over {limit} characters")
    if "```" in value or _AUTHORITY_CONFLICT.search(value):
        raise WhisperError(f"{label} conflicts with runtime policy")
    return value


def _keywords(topic: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", topic.lower()) if len(w) >= 4 and w not in _STOP]


def mentions(topic: str, text: str) -> bool:
    """True when ``text`` names the topic: the whole phrase, or most of its distinctive words."""
    haystack = " ".join(str(text or "").lower().split())
    if not haystack:
        return False
    if topic.lower() in haystack:
        return True
    words = _keywords(topic)
    if not words:
        return False
    hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", haystack))
    return hits >= max(1, (len(words) + 1) // 2)


class WhisperStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("history"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"active": None, "history": []}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".whispers-", suffix=".json")
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def active(self, now: datetime) -> dict | None:
        """The current unexpired whisper (expired ones are retired on read)."""
        now = now.astimezone(timezone.utc)
        with _LOCK:
            data = self._read()
            whisper = data.get("active")
            if whisper and whisper.get("expires_at", "") <= now.isoformat():
                self._retire(data, "expired", now)
                self._write(data)
                return None
            return whisper

    def servable(self, now: datetime) -> dict | None:
        """The active whisper unless it came up within the cooldown."""
        whisper = self.active(now)
        if not whisper:
            return None
        last = whisper.get("last_discussed_at")
        if last:
            try:
                if now.astimezone(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=COOLDOWN_HOURS):
                    return None
            except ValueError:
                pass
        return whisper

    def seed(self, topic: str, thought: str, *, now: datetime, reference: str = "",
             days: float = DEFAULT_DAYS, origin: str = "user_seeded") -> dict:
        if origin not in ORIGINS:
            raise WhisperError("unknown origin")
        if not 0 < float(days) <= 90:
            raise WhisperError("days must be between 0 and 90")
        now = now.astimezone(timezone.utc)
        whisper = {
            "id": f"w{secrets.token_hex(4)}",
            "topic": _clean(topic, TOPIC_LIMIT, "topic"),
            "thought": _clean(thought, THOUGHT_LIMIT, "thought"),
            "reference": _clean(reference, REFERENCE_LIMIT, "reference") if str(reference or "").strip() else "",
            "origin": origin,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=float(days))).isoformat(),
            "last_discussed_at": None,
            "times_discussed": 0,
        }
        with _LOCK:
            data = self._read()
            if data.get("active"):
                self._retire(data, "replaced", now)
            data["active"] = whisper
            self._write(data)
        return whisper

    def retire(self, now: datetime, reason: str = "retired") -> None:
        with _LOCK:
            data = self._read()
            if data.get("active"):
                self._retire(data, reason, now.astimezone(timezone.utc))
                self._write(data)

    def note_turn(self, text: str, now: datetime) -> bool:
        """Mark the whisper discussed when this turn's text names its topic."""
        now = now.astimezone(timezone.utc)
        with _LOCK:
            data = self._read()
            whisper = data.get("active")
            if not whisper or not mentions(whisper["topic"], text):
                return False
            whisper["last_discussed_at"] = now.isoformat()
            whisper["times_discussed"] = int(whisper.get("times_discussed") or 0) + 1
            self._write(data)
            return True

    @staticmethod
    def _retire(data: dict, reason: str, now: datetime) -> None:
        old = dict(data["active"], retired_at=now.isoformat(), retired_reason=reason)
        data["history"] = (data.get("history") or [])[-(HISTORY_LIMIT - 1):] + [old]
        data["active"] = None
