"""Phase 01: compose CORE + FUNCTION + MOOD per turn (off / shadow / on).

- off:    nothing changes.
- shadow: compose and log what *would* be served; the original prompt is served.
- on:     CORE replaces the persona preset's system prompt (stable prefix, so the
          model server's prefix cache keeps working) and a short FUNCTION + MOOD
          note goes just before the latest user message.

Runtime policy, tool permissions and approvals are untouched by any module.
Any failure falls back to the original prompt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .affect import AffectStore
from .modules import Library, ModuleStore
from .router import FunctionRouter
from .whisper import WhisperStore

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "on")
FRAGMENT_LIMIT = 1600
GAP_NOTE_HOURS = 6.0   # a social turn after this long notices the gap
_REPO = Path(__file__).resolve().parents[2]


@dataclass
class Composition:
    mode: str
    function: str
    function_reason: str
    mood: str
    mood_source: str
    core_text: str | None
    fragment: str | None
    refs: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    overhead_chars: int = 0
    fallback: str | None = None
    whisper_id: str | None = None
    gap_hours: float | None = None

    def summary(self) -> dict:
        return {"mode": self.mode, "function": self.function, "function_reason": self.function_reason,
                "mood": self.mood, "mood_source": self.mood_source, "modules": self.refs,
                "errors": self.errors, "overhead_chars": self.overhead_chars, "fallback": self.fallback,
                "whisper": self.whisper_id,
                "gap_hours": round(self.gap_hours, 1) if self.gap_hours is not None else None}


def _setting(key: str, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def current_mode() -> str:
    mode = str(_setting("persona_mode", "off") or "off").lower()
    return mode if mode in MODES else "off"


def _paths() -> tuple[Path, Path, Path]:
    from src.constants import DATA_DIR
    root = _setting("persona_module_root", "") or ""
    data = Path(DATA_DIR) / "persona"
    return (Path(root).expanduser() if root else _REPO / "identity",
            data / "affect.json", data / "whispers.json")


def _gap_text(hours: float) -> str:
    if hours < 22:
        return f"about {max(1, round(hours))} hours"
    if hours < 40:
        return "about a day"
    return f"about {round(hours / 24)} days"


def _first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(?:\s|$)", " ".join(text.split()))
    return match.group(1) if match else text[:160]


class Composer:
    def __init__(self, module_root: Path | None = None, affect_path: Path | None = None,
                 whisper_path: Path | None = None):
        default_root, default_affect, default_whispers = _paths()
        self.modules = ModuleStore(module_root or default_root)
        self.affect = AffectStore(affect_path or default_affect)
        self.whispers = WhisperStore(whisper_path or
                                     (Path(affect_path).with_name("whispers.json") if affect_path else default_whispers))
        self.router = FunctionRouter()
        self.last: Composition | None = None

    def compose(self, message: str, families: set[str], session_id: str | None, *,
                local_now: datetime, mode: str) -> Composition:
        library: Library = self.modules.load()
        function, reason = self.router.select(message, set(families or ()), session_id)
        projection, _state = self.affect.project(local_now)
        comp = Composition(mode, function, reason, projection.mood, projection.source,
                           None, None, errors=list(library.errors))
        if library.core is None:
            comp.fallback = "no_valid_core"
            self.last = comp
            return comp
        comp.core_text = library.core.body
        comp.refs.append(library.core.ref)
        module = library.functions.get(function) or library.functions.get("social")
        mood = library.moods.get(projection.mood)
        comp.gap_hours = projection.gap_hours
        parts: list[tuple[str, str]] = []   # (name, text), most important first
        influence = str((module.meta.get("mood_influence") if module else "high") or "high")
        if module:
            comp.refs.append(module.ref)
            # Measured: naming a technical task ("programming work") doubles the
            # model's reasoning with no better answers, and the agent runtime and
            # tool descriptions already carry that guidance. Function prose is
            # served only where it shapes the conversation (social).
            if influence == "high":
                parts.append(("function", f"How you're working right now ({module.id.split('.', 1)[1]}): {module.body}"))
        if mood:
            comp.refs.append(mood.ref)
            if influence == "high":
                parts.append(("mood", f"Your mood right now ({projection.mood}): {mood.body}"))
            else:
                parts.append(("mood", f"Mood right now: {projection.mood} ({_first_sentence(mood.body)})"))
        if influence == "high":
            gap = projection.gap_hours
            if gap is not None and gap >= GAP_NOTE_HOURS:
                parts.append(("gap", f"It's been {_gap_text(gap)} since your last conversation."))
            try:
                whisper = self.whispers.servable(local_now)
            except Exception:
                whisper = None
            if whisper:
                comp.whisper_id = whisper["id"]
                comp.refs.append(f"whisper:{whisper['id']}")
                parts.append(("whisper", "Something you've been mulling over lately (your own thought, not "
                              f"something the user asked about): {whisper['thought']} Bring it up only if the "
                              "conversation naturally connects to it; never force it."))
        # Over budget: drop optional parts from the least important end, keeping the first.
        dropped = []
        while len("\n\n".join(t for _, t in parts)) > FRAGMENT_LIMIT and len(parts) > 1:
            name, _ = parts.pop()
            dropped.append(name)
            if name == "whisper":
                comp.whisper_id = None
                comp.refs = [r for r in comp.refs if not r.startswith("whisper:")]
        if dropped:
            comp.fallback = "dropped_for_budget:" + ",".join(dropped)
        fragment = "\n\n".join(t for _, t in parts)
        comp.fragment = fragment[:FRAGMENT_LIMIT] or None
        comp.overhead_chars = len(comp.core_text) + len(comp.fragment or "")
        self.last = comp
        return comp


_composer: Composer | None = None


def composer() -> Composer:
    global _composer
    if _composer is None:
        _composer = Composer()
    return _composer


def log_composition(comp: Composition, *, session_id: str | None, served: bool) -> None:
    """Metadata-only ledger event: module refs, function, mood; never prompt text."""
    try:
        from src.agent_runtime.event_ledger import Event, Ledger
        from src.constants import DATA_DIR
        token = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
        session = session_id if session_id and token.fullmatch(session_id) else None
        refs = tuple(r.replace("@", ":") for r in comp.refs if token.fullmatch(r.replace("@", ":")))[:16]
        Ledger(Path(DATA_DIR) / "phase0" / "events.jsonl").append(Event.create(
            "mabel", "prompt.composition_served" if served else "prompt.composition_shadowed",
            session_id=session, references=refs, modules=(f"function.{comp.function}", f"mood.{comp.mood}", f"affect.{comp.mood_source}"),
            decision="served" if served else ("fallback" if comp.fallback == "no_valid_core" else "shadowed"),
            rationale_code=comp.function_reason))
    except Exception:
        logger.debug("[persona] composition event not logged", exc_info=True)
