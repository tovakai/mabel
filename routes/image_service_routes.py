"""Pictures for other local apps (src/image_service.py) — /api/services/image/*.

Callers authenticate with an API token carrying the ``images:generate`` scope
(``Authorization: Bearer ody_…``); a logged-in browser session works too.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src import image_service
from src.auth_helpers import require_api_token_scope

SCOPE = "images:generate"


class ImageJob(BaseModel):
    prompt: str = Field(..., max_length=image_service.MAX_PROMPT)
    service: str = "image"
    width: int | None = None
    height: int | None = None
    transparent: bool = False
    client: str = ""
    ref: str = ""
    now: bool = False                 # also start the batch straight away ("process queue now")


def _auth(request: Request) -> str:
    owner = require_api_token_scope(request, SCOPE)
    if not owner:
        raise HTTPException(401, "Not authenticated")
    return owner


def setup_image_service_routes() -> APIRouter:
    router = APIRouter(prefix="/api/services/image")

    @router.post("", status_code=202)
    async def queue(body: ImageJob, request: Request) -> dict:
        _auth(request)
        try:
            out = image_service.enqueue(body.prompt, service=body.service, width=body.width, height=body.height,
                                        transparent=body.transparent, client=body.client, ref=body.ref)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except image_service.Busy as exc:
            raise HTTPException(429, str(exc))
        if body.now:
            try:
                image_service.start_now()
                out["started"] = True
            except image_service.Busy as exc:
                out["started"] = False
                out["not_started"] = str(exc)
        return out

    @router.get("")
    async def overview(request: Request) -> dict:
        _auth(request)
        return image_service.overview()

    @router.post("/process")
    async def process_now(request: Request) -> dict:
        _auth(request)
        try:
            return image_service.start_now()
        except image_service.Busy as exc:
            raise HTTPException(409, str(exc))

    @router.get("/{job}")
    async def job_status(job: str, request: Request) -> dict:
        _auth(request)
        found = image_service.status(job)
        if not found:
            raise HTTPException(404, "no such job (finished jobs are kept for a day)")
        return found

    @router.get("/{job}/result")
    async def job_result(job: str, request: Request):
        _auth(request)
        path = image_service.result_path(job)
        if not path:
            raise HTTPException(404, "no picture for this job (not done yet, failed, or older than a day)")
        return FileResponse(path, media_type="image/png")

    return router
