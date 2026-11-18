"""Where AUDIO gets its music: Mabel's proxy (the PC) or Navidrome directly (the deck).

Navidrome runs on the lifebook, not on the PC, so with its own login the deck keeps its music when
Mabel (or the whole PC) is down. Both sources offer the same calls: music, music_write, cover,
stream_url, download. Configure the direct one in config.toml:

    [navidrome]
    url = "http://lifebook.tail4312a5.ts.net:4533"
    user = "mabel"
    password = "…"
"""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from urllib.parse import urlencode

import httpx

API_VERSION, CLIENT = "1.16.1", "mabel-os"


class Navidrome:
    def __init__(self, url: str, user: str, password: str):
        self.url, self.user, self.password = url.rstrip("/"), user, password
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, read=600.0))
        self.label = "navidrome"

    def _auth(self) -> dict:
        salt = secrets.token_hex(8)
        return {"u": self.user, "t": hashlib.md5((self.password + salt).encode()).hexdigest(), "s": salt,
                "v": API_VERSION, "c": CLIENT, "f": "json"}

    def _params(self, params: dict) -> list[tuple[str, str]]:
        pairs = list(self._auth().items())
        for key, value in params.items():
            for v in (value if isinstance(value, list) else [value]):
                if v is not None:
                    pairs.append((key, str(v).lower() if isinstance(v, bool) else str(v)))
        return pairs

    async def music(self, method: str, **params) -> dict:
        resp = await self.http.get(f"{self.url}/rest/{method}", params=self._params(params))
        data = resp.json().get("subsonic-response", {})
        if data.get("status") != "ok":
            raise RuntimeError((data.get("error") or {}).get("message") or f"Navidrome said {resp.status_code}")
        return data

    async def music_write(self, method: str, **params) -> None:
        await self.music(method, **params)

    async def cover(self, cover_id: str, size: int = 300) -> bytes | None:
        resp = await self.http.get(f"{self.url}/rest/getCoverArt", params=self._params({"id": cover_id, "size": size}))
        return resp.content if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image") else None

    def stream_url(self, song_id: str) -> str:
        # Auth rides in the URL (Subsonic); mpv gets it over its IPC socket, never on a command line.
        return f"{self.url}/rest/stream?{urlencode(self._params({'id': song_id}))}"

    async def download(self, song_id: str, path: Path, max_bitrate: int = 0) -> int:
        params = {"id": song_id, **({"maxBitRate": max_bitrate} if max_bitrate else {})}
        return await _save(self.http, f"{self.url}/rest/stream", self._params(params), {}, path)

    async def close(self) -> None:
        await self.http.aclose()


class MabelMusic:
    """Mabel's music proxy (/api/music/*), with the TUI's API token."""

    def __init__(self, mabel):
        self.mabel, self.label = mabel, "mabel"

    async def music(self, method: str, **params) -> dict:
        return await self.mabel.music(method, **params)

    async def music_write(self, method: str, **params) -> None:
        await self.mabel.music_write(method, **params)

    async def cover(self, cover_id: str, size: int = 300) -> bytes | None:
        return await self.mabel.cover(cover_id, size)

    def stream_url(self, song_id: str) -> str:
        return self.mabel.stream_url(song_id)

    async def download(self, song_id: str, path: Path, max_bitrate: int = 0) -> int:
        params = {"maxBitRate": max_bitrate} if max_bitrate else {}
        return await _save(self.mabel.http, self.mabel.stream_url(song_id), params, {}, path)

    async def close(self) -> None:
        pass


async def _save(http: httpx.AsyncClient, url: str, params, headers: dict, path: Path) -> int:
    """Stream a song to ``path`` (via a .part file, so a broken download never looks finished)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(path.suffix + ".part")
    size = 0
    async with http.stream("GET", url, params=params, headers=headers) as resp:
        if resp.status_code >= 300:
            raise RuntimeError(f"download failed: {resp.status_code}")
        with part.open("wb") as fh:
            async for chunk in resp.aiter_bytes(1 << 16):
                fh.write(chunk)
                size += len(chunk)
    part.replace(path)
    return size
