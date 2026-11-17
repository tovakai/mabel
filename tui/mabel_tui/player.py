"""Local playback through mpv's JSON IPC. Songs stream from Mabel's music proxy with the TUI's token.

The token goes to mpv over the IPC socket (http-header-fields), never on its command line, so it
doesn't show up in the process list. The queue lives here; mpv's playlist mirrors it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable

from .config import CACHE


class Player:
    def __init__(self, stream_url: Callable[[str], str], token: str, volume: int = 70,
                 on_change: Callable[[], None] | None = None, on_scrobble: Callable[[str, bool], None] | None = None):
        self.stream_url, self.token, self.volume = stream_url, token, volume
        self.on_change, self.on_scrobble = on_change, on_scrobble
        self.queue: list[dict] = []
        self.index = -1
        self.paused = False
        self.position = 0.0
        self.duration = 0.0
        self.error = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._scrobbled: set[str] = set()
        self._socket = Path(os.environ.get("XDG_RUNTIME_DIR") or CACHE) / f"mabel-tui-mpv-{uuid.uuid4().hex[:6]}.sock"

    @staticmethod
    def available() -> bool:
        return shutil.which("mpv") is not None

    @property
    def current(self) -> dict | None:
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    # ── mpv ─────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        if self._writer:
            return True
        if not self.available():
            self.error = "mpv isn't installed (sudo pacman -S mpv)"
            return False
        self._socket.parent.mkdir(parents=True, exist_ok=True)
        self._proc = await asyncio.create_subprocess_exec(
            "mpv", "--idle=yes", "--no-video", "--no-terminal", "--audio-display=no", "--force-window=no",
            f"--input-ipc-server={self._socket}", f"--volume={self.volume}",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(50):
            if self._socket.exists():
                break
            await asyncio.sleep(0.1)
        try:
            reader, self._writer = await asyncio.open_unix_connection(str(self._socket))
        except OSError as exc:
            self.error = f"couldn't talk to mpv: {exc}"
            return False
        asyncio.create_task(self._read(reader))
        await self._cmd("set_property", "http-header-fields", [f"Authorization: Bearer {self.token}"])
        for i, prop in enumerate(("pause", "time-pos", "duration", "playlist-pos", "idle-active"), 1):
            await self._cmd("observe_property", i, prop)
        return True

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), 3)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._socket.unlink(missing_ok=True)

    async def _cmd(self, *args):
        if not self._writer:
            return None
        self._next_id += 1
        rid = self._next_id
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, 5)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(rid, None)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "request_id" in msg and msg["request_id"] in self._pending:
                fut = self._pending[msg["request_id"]]
                if not fut.done():
                    fut.set_result(msg.get("data"))
                continue
            if msg.get("event") == "property-change":
                self._property(msg.get("name"), msg.get("data"))
            elif msg.get("event") == "end-file" and msg.get("reason") == "error":
                self.error = "couldn't play that song"
                self._changed()
        self._writer = None

    def _property(self, name: str, value) -> None:
        if name == "pause":
            self.paused = bool(value)
        elif name == "time-pos":
            self.position = float(value or 0)
            self._maybe_scrobble()
        elif name == "duration":
            self.duration = float(value or 0)
        elif name == "playlist-pos":
            self.index = int(value) if value is not None else -1
            self.position = 0.0
            if self.current and self.on_scrobble:
                self.on_scrobble(self.current["id"], False)          # "now playing"
        elif name == "idle-active" and value:
            self.position = 0.0
        self._changed()

    def _maybe_scrobble(self) -> None:
        song = self.current
        if not song or not self.on_scrobble or song["id"] in self._scrobbled:
            return
        length = self.duration or float(song.get("duration") or 0)
        if length and (self.position >= length / 2 or self.position >= 240):
            self._scrobbled.add(song["id"])
            self.on_scrobble(song["id"], True)

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    # ── the queue ───────────────────────────────────────────────────────

    async def play(self, songs: list[dict], start: int = 0) -> None:
        if not songs or not await self.start():
            return
        self.queue, self.error = list(songs), ""
        self._scrobbled.clear()
        await self._cmd("playlist-clear")
        for i, song in enumerate(self.queue):
            await self._cmd("loadfile", self.stream_url(song["id"]), "replace" if i == 0 else "append")
        if start:
            await self._cmd("set_property", "playlist-pos", start)
        await self._cmd("set_property", "pause", False)

    async def enqueue(self, songs: list[dict]) -> None:
        if not self.queue:
            return await self.play(songs)
        for song in songs:
            self.queue.append(song)
            await self._cmd("loadfile", self.stream_url(song["id"]), "append")
        self._changed()

    async def play_next(self, songs: list[dict]) -> None:
        if not self.queue:
            return await self.play(songs)
        at = self.index + 1
        for offset, song in enumerate(songs):
            self.queue.insert(at + offset, song)
            await self._cmd("loadfile", self.stream_url(song["id"]), "append")
            await self._cmd("playlist-move", len(self.queue) - 1, at + offset)
        self._changed()

    async def jump(self, index: int) -> None:
        if 0 <= index < len(self.queue):
            await self._cmd("set_property", "playlist-pos", index)
            await self._cmd("set_property", "pause", False)

    async def toggle(self) -> None:
        await self._cmd("cycle", "pause")

    async def set_paused(self, paused: bool) -> None:
        await self._cmd("set_property", "pause", paused)

    async def next(self) -> None:
        await self._cmd("playlist-next")

    async def previous(self) -> None:
        if self.position > 5:
            await self._cmd("seek", 0, "absolute")
        else:
            await self._cmd("playlist-prev")

    async def seek(self, seconds: float) -> None:
        await self._cmd("seek", seconds, "relative")

    async def change_volume(self, delta: int) -> None:
        self.volume = max(0, min(130, self.volume + delta))
        await self._cmd("set_property", "volume", self.volume)
        self._changed()

    async def apply(self, command: dict) -> None:
        """A control_music command from Mabel (the same one the web player gets)."""
        op, songs = command.get("op"), command.get("songs") or []
        if op == "play":
            await self.play(songs)
        elif op == "queue":
            await self.enqueue(songs)
        elif op == "play_next":
            await self.play_next(songs)
        elif op == "pause":
            await self.set_paused(True)
        elif op == "resume":
            await self.set_paused(False)
        elif op == "next":
            await self.next()
        elif op == "previous":
            await self.previous()
