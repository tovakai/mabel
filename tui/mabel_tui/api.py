"""Talking to Mabel over HTTP with an API token: sessions, the chat stream, her day, the music proxy."""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx


class MabelError(RuntimeError):
    pass


class Mabel:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token
        self.http = httpx.AsyncClient(base_url=self.url, headers={"Authorization": f"Bearer {token}"},
                                      timeout=httpx.Timeout(30.0, read=600.0))

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, **params) -> dict | list:
        resp = await self.http.get(path, params=params or None)
        if resp.status_code >= 300:
            raise MabelError(f"{path}: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    # ── chat ────────────────────────────────────────────────────────────

    async def sessions(self) -> list[dict]:
        data = await self._get("/api/sessions")
        items = data if isinstance(data, list) else data.get("sessions", [])
        live = [s for s in items if not s.get("archived")]
        return sorted(live, key=lambda s: s.get("last_message_at") or s.get("updated_at") or "", reverse=True)

    async def history(self, session_id: str) -> list[dict]:
        data = await self._get(f"/api/history/{session_id}")
        return [m for m in (data.get("history") or []) if m.get("role") in ("user", "assistant")]

    async def new_session(self, name: str, like: dict | None = None) -> dict:
        """A new chat on the same model as ``like`` (tokens must name a registered endpoint)."""
        like = like or (await self.sessions() or [{}])[0]
        form = {"name": name, "endpoint_id": like.get("endpoint_id") or "",
                "endpoint_url": like.get("endpoint_url") or "", "model": like.get("model") or ""}
        resp = await self.http.post("/api/session", data=form)
        if resp.status_code >= 300:
            raise MabelError(f"new session: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    async def stream(self, session_id: str, message: str, *, approval: tuple[str, str] | None = None
                     ) -> AsyncIterator[dict]:
        """The chat stream as events: {"delta"}, {"type": ...}, {"type": "error"}. Ends after [DONE]."""
        form = {"message": message, "session": session_id, "mode": "chat"}
        if approval:
            form.update(message="", tool_approval_id=approval[0], tool_approval_decision=approval[1])
        async with self.http.stream("POST", "/api/chat_stream", data=form) as resp:
            if resp.status_code >= 300:
                body = (await resp.aread()).decode(errors="replace")
                yield {"type": "error", "error": f"{resp.status_code}: {body[:300]}"}
                return
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:].strip()
                if payload == "[DONE]":
                    return
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    if "error" in event and "type" not in event:
                        event["type"] = "error"
                    yield event

    # ── her day ─────────────────────────────────────────────────────────

    async def life(self) -> dict:
        return await self._get("/api/tui/life")

    async def dream_image(self) -> bytes | None:
        resp = await self.http.get("/api/tui/dream-image")
        return resp.content if resp.status_code == 200 else None

    # ── music (Mabel's Navidrome proxy) ─────────────────────────────────

    async def music(self, method: str, **params) -> dict:
        data = await self._get(f"/api/music/rest/{method}", **params)
        return data if isinstance(data, dict) else {}

    async def music_write(self, method: str, **params) -> None:
        await self.http.post(f"/api/music/rest/{method}", json=params)

    async def cover(self, cover_id: str, size: int = 300) -> bytes | None:
        resp = await self.http.get(f"/api/music/cover/{cover_id}", params={"size": size})
        return resp.content if resp.status_code == 200 else None

    def stream_url(self, song_id: str) -> str:
        return f"{self.url}/api/music/stream/{song_id}"
