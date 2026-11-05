"""Memory sidebar → Episodes tab: review Phase 00.5 candidates and kept episodes.

Owner-scoped. Clicking "Keep" in the UI is the explicit human approval that the
verified promotion writer requires; nothing is promoted automatically. "Later"
and "Discard" are the queue's existing defer / reject decisions. The global
capture / recall switches are admin-only settings.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import get_current_user, require_user, storage_owner_for_request

logger = logging.getLogger(__name__)

_SUMMARY = re.compile(r"^User said: (?P<user>.*?)\nMabel replied: (?P<assistant>.*)$", re.S)
_SOURCE = re.compile(r"^chat:(?P<session>[^:]+):message:(?P<message>.+)$")


class EpisodicSettings(BaseModel):
    capture_enabled: Optional[bool] = None
    recall_enabled: Optional[bool] = None


_DAY = re.compile(r"^(?P<source>lifelog:day|letter:note):(?P<date>\d{4}-\d{2}-\d{2})(?:-\d+)?$")


def _item(row: dict) -> dict:
    day = _DAY.match(str(row.get("source_ref") or ""))
    if day:      # a moment from Mabel's account of his day
        return {"id": row["id"], "kind": row.get("kind"), "state": row.get("state"),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at"), "session_id": None,
                "source_label": (f"From her letters with Tova, {day.group('date')}"
                                 if day.group("source") == "letter:note" else f"From your day, {day.group('date')}"), "user_excerpt": str(row.get("summary") or ""),
                "assistant_excerpt": "", "interpretation": row.get("interpretation") or ""}
    match = _SUMMARY.match(str(row.get("summary") or ""))
    source = _SOURCE.match(str(row.get("source_ref") or ""))
    return {
        "id": row["id"],
        "kind": row.get("kind"),
        "state": row.get("state"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "session_id": source.group("session") if source else None,
        "user_excerpt": match.group("user") if match else str(row.get("summary") or "")[:500],
        "assistant_excerpt": match.group("assistant") if match else "",
        "interpretation": row.get("interpretation") or "",
    }


def setup_episodic_routes() -> APIRouter:
    router = APIRouter(prefix="/api/memory/episodic", tags=["memory"])

    def _queue(request: Request):
        from src.constants import DATA_DIR
        from src.episodic_memory import CandidateQueue
        owner = storage_owner_for_request(request)
        if not owner:
            raise HTTPException(401, "Sign in to manage episodic memory")
        return CandidateQueue(Path(DATA_DIR) / "episodic_candidates", owner=owner)

    def _is_admin(request: Request) -> bool:
        from src.tool_security import owner_is_admin_or_single_user
        return bool(owner_is_admin_or_single_user(get_current_user(request)))

    @router.get("")
    async def overview(request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from src.episodic_memory.chat_recall import capture_enabled, recall_enabled
        queue = _queue(request)
        return {
            "capture_enabled": capture_enabled(),
            "recall_enabled": recall_enabled(),
            "can_manage_settings": _is_admin(request),
            "pending": [_item(r) for r in queue.list(state="pending", limit=100)],
            "deferred": [_item(r) for r in queue.list(state="deferred", limit=100)],
            "episodes": [_item(r) for r in queue.list(state="promoted", limit=100)],
        }

    @router.post("/{candidate_id}/keep")
    async def keep(candidate_id: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        from core.database import SessionLocal
        from routes.prefs_routes import _load_for_user
        from src.episodic_memory import CandidateAdmissionError, CapturePolicy
        from src.episodic_memory.chat_recall import capture_enabled, vault_paths
        from src.episodic_memory.promotion import promote_verified
        queue = _queue(request)
        if not capture_enabled():
            raise HTTPException(409, "Episodic memory is turned off")
        prefs = _load_for_user(queue.owner)
        vault_root, protected = vault_paths()
        try:
            with SessionLocal() as db:
                saved = promote_verified(
                    queue, candidate_id, db=db, vault_root=vault_root, protected_roots=protected,
                    policy=CapturePolicy(episodic_enabled=True,
                                         memory_enabled=bool(prefs.get("memory_enabled", True))),
                    approved=True,  # the user's click is the explicit approval
                )
        except (CandidateAdmissionError, PermissionError) as error:
            raise HTTPException(409, str(error)) from None
        return {"id": saved["id"], "state": saved["state"], "changed": saved["changed"]}

    async def _decide(candidate_id: str, request: Request, outcome: str, reason: str) -> dict[str, Any]:
        from src.episodic_memory import CandidateAdmissionError
        queue = _queue(request)
        try:
            row = queue.decide(candidate_id, outcome=outcome, reason=reason)
        except CandidateAdmissionError as error:
            raise HTTPException(409, str(error)) from None
        return {"id": row["id"], "state": row["state"], "changed": row["changed"]}

    @router.post("/{candidate_id}/later")
    async def later(candidate_id: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        return await _decide(candidate_id, request, "deferred", "user_deferred")

    @router.post("/{candidate_id}/discard")
    async def discard(candidate_id: str, request: Request, _user: str = Depends(require_user)) -> dict[str, Any]:
        return await _decide(candidate_id, request, "rejected", "user_rejected")

    @router.put("/settings")
    async def update_settings(body: EpisodicSettings, request: Request,
                              _user: str = Depends(require_user)) -> dict[str, Any]:
        if not _is_admin(request):
            raise HTTPException(403, "Only an admin can change episodic memory settings")
        from src.settings import load_settings, save_settings
        current = dict(load_settings())
        if body.capture_enabled is not None:
            current["episodic_capture_enabled"] = bool(body.capture_enabled)
        if body.recall_enabled is not None:
            current["episodic_recall_enabled"] = bool(body.recall_enabled)
        save_settings(current)
        return {"capture_enabled": bool(current.get("episodic_capture_enabled", False)),
                "recall_enabled": bool(current.get("episodic_recall_enabled", False))}

    return router
