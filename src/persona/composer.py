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

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "on")
FRAGMENT_LIMIT = 1600
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

    def summary(self) -> dict:
        return {"mode": self.mode, "function": self.function, "function_reason": self.function_reason,
                "mood": self.mood, "mood_source": self.mood_source, "modules": self.refs,
                "errors": self.errors, "overhead_chars": self.overhead_chars, "fallback": self.fallback}


def _setting(key: str, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def current_mode() -> str:
    mode = str(_setting("persona_mode", "off") or "off").lower()
    return mode if mode in MODES else "off"


def _paths() -> tuple[Path, Path]:
    from src.constants import DATA_DIR
    root = _setting("persona_module_root", "") or ""
    return (Path(root).expanduser() if root else _REPO / "identity",
            Path(DATA_DIR) / "persona" / "affect.json")


def _first_sentence(text: str) -> str:
    match = re.match(r"(.+?[.!?])(?:\s|$)", " ".join(text.split()))
    return match.group(1) if match else text[:160]


class Composer:
    def __init__(self, module_root: Path | None = None, affect_path: Path | None = None):
        default_root, default_affect = _paths()
        self.modules = ModuleStore(module_root or default_root)
        self.affect = AffectStore(affect_path or default_affect)
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
        parts = []
        influence = str((module.meta.get("mood_influence") if module else "high") or "high")
        if module:
            comp.refs.append(module.ref)
            # Measured: naming a technical task ("programming work") doubles the
            # model's reasoning with no better answers, and the agent runtime and
            # tool descriptions already carry that guidance. Function prose is
            # served only where it shapes the conversation (social).
            if influence == "high":
                parts.append(f"How you're working right now ({module.id.split('.', 1)[1]}): {module.body}")
        if mood:
            comp.refs.append(mood.ref)
            if influence == "high":
                parts.append(f"Your mood right now ({projection.mood}): {mood.body}")
            else:
                parts.append(f"Mood right now: {projection.mood} ({_first_sentence(mood.body)})")
        fragment = "\n\n".join(parts)
        if len(fragment) > FRAGMENT_LIMIT and len(parts) > 1:
            fragment = parts[0]  # over budget: drop the mood, keep the function
            comp.fallback = "mood_dropped_for_budget"
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
            session_id=session, references=refs, modules=(f"function.{comp.function}", f"mood.{comp.mood}"),
            decision="served" if served else ("fallback" if comp.fallback == "no_valid_core" else "shadowed"),
            rationale_code=comp.function_reason))
    except Exception:
        logger.debug("[persona] composition event not logged", exc_info=True)
