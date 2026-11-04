"""Phase 01: choose one FUNCTION module per turn, with conversational continuity.

Deterministic: the turn contract's capability families plus a few explicit
cues, no classifier model. Code blocks and quoted lines are data, so their
contents cannot switch the function (e.g. code containing [[wikilinks]]).
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from threading import Lock

FUNCTIONS = ("social", "programming", "vault", "research")
_FOLLOWUP_SECONDS = 2 * 3600
_SESSIONS_KEPT = 500

_SWITCH_TO_SOCIAL = re.compile(
    r"\b(?:let'?s|lets)\s+(?:just\s+)?(?:chat|talk)\b|\b(?:enough|done)\s+with\s+(?:the\s+)?(?:code|work|notes)\b"
    r"|\bjust\s+(?:wanna|want\s+to)\s+(?:chat|talk)\b", re.I)
_PROGRAMMING = re.compile(
    r"```|\btraceback\b|\bstack\s*trace\b|\bexception\b|\berror:|\bsyntax\s*error\b|\bsegfault\b"
    r"|\b(?:python|javascript|typescript|rust|golang|java|c\+\+|bash|regex|sql|api|endpoint|function|"
    r"compile|compiler|unit\s+tests?|pytest|git|commit|refactor|debug|bug|script)\b", re.I)
_VAULT = re.compile(r"\b(?:obsidian|vault|frontmatter|wikilinks?|my\s+notes?|the\s+note|notes?\s+(?:on|about))\b", re.I)
_RESEARCH = re.compile(r"\b(?:research|sources?|look\s+up|search\s+(?:for|the\s+web)|compare\s+(?:these|the)|papers?)\b", re.I)
_SHORT_FOLLOWUP = 12  # words


def _strip_data(text: str) -> str:
    text = re.sub(r"```.*?```", " CODEBLOCK ", text or "", flags=re.S)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


class FunctionRouter:
    def __init__(self):
        self._lock = Lock()
        self._active: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def select(self, message: str, families: set[str], session_id: str | None,
               *, now: float | None = None) -> tuple[str, str]:
        """(function, reason) for this turn."""
        now = time.time() if now is None else now
        raw = str(message or "")
        text = _strip_data(raw)
        previous = self.previous(session_id, now=now)
        if _SWITCH_TO_SOCIAL.search(text):
            choice = ("social", "explicit_switch")
        elif "```" in raw or "shell_files" in families or _PROGRAMMING.search(text):
            choice = ("programming", "code_cue" if "shell_files" not in families else "contract_family")
        elif families & {"workspace_read", "notes", "documents"} or _VAULT.search(text):
            choice = ("vault", "contract_family" if families else "vault_cue")
        elif families & {"search_browser", "research"} or _RESEARCH.search(text):
            choice = ("research", "contract_family" if families else "research_cue")
        elif previous and previous != "social" and len(text.split()) <= _SHORT_FOLLOWUP:
            choice = (previous, "followup_continuation")
        else:
            choice = ("social", "default")
        if session_id:
            with self._lock:
                self._active[session_id] = (choice[0], now)
                self._active.move_to_end(session_id)
                while len(self._active) > _SESSIONS_KEPT:
                    self._active.popitem(last=False)
        return choice

    def previous(self, session_id: str | None, *, now: float) -> str | None:
        if not session_id:
            return None
        with self._lock:
            entry = self._active.get(session_id)
        if not entry or now - entry[1] > _FOLLOWUP_SECONDS:
            return None
        return entry[0]
