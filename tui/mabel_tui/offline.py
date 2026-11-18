"""Offline music: songs downloaded to this machine, played from disk when they're here.

Files live in ~/Music/mabel-os-offline/<artist>/<album>/<track> - <title>.<ext> with an index
(index.json) of the song metadata (so the library browses with no network at all) and of the
collections you downloaded (an album, a playlist, a single song). Covers are saved beside them.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("MABEL_OFFLINE_DIR") or Path.home() / "Music" / "mabel-os-offline")


def _safe(text: str, limit: int = 80) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(text or "unknown")).strip(" .")[:limit] or "unknown"


class Offline:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.index_path = root / "index.json"
        self.songs: dict[str, dict] = {}         # song id → {"song": meta, "path": rel, "size": n}
        self.collections: dict[str, dict] = {}   # key → {"kind", "name", "artist", "song_ids", "cover"}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.songs = {k: v for k, v in (data.get("songs") or {}).items() if (self.root / v["path"]).exists()}
        self.collections = data.get("collections") or {}

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"songs": self.songs, "collections": self.collections}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.index_path)

    def has(self, song_id: str) -> bool:
        return song_id in self.songs

    def local(self, song_id: str) -> str | None:
        entry = self.songs.get(song_id)
        return str(self.root / entry["path"]) if entry else None

    def path_for(self, song: dict, transcoded: bool) -> Path:
        ext = (song.get("transcodedSuffix") if transcoded else None) or song.get("suffix") or "mp3"
        track = f"{int(song['track']):02d} - " if str(song.get("track") or "").isdigit() else ""
        return Path(_safe(song.get("artist"))) / _safe(song.get("album")) / f"{track}{_safe(song.get('title'), 120)}.{ext}"

    async def download(self, source, songs: list[dict], *, key: str, kind: str, name: str, artist: str = "",
                       cover_id: str | None = None, max_bitrate: int = 0, progress=None) -> tuple[int, int]:
        """Fetch what isn't here yet. Returns (downloaded, already here)."""
        got = had = 0
        old = self.collections.get(key, {})
        # Recorded first, with every song it should have: a broken download shows up half-done, and O again finishes it.
        self.collections[key] = {"kind": kind, "name": name, "artist": artist, "cover": old.get("cover"),
                                 "song_ids": list(dict.fromkeys((old.get("song_ids") or []) + [s["id"] for s in songs]))}
        self.save()
        for song in songs:                                    # covers too, per album, so the deck shows them offline
            if song.get("coverArt") and not self.cover_file(song["coverArt"]):
                await self._save_cover(source, song["coverArt"])
        if cover_id and not self.cover_file(cover_id):
            await self._save_cover(source, cover_id)
        self.collections[key]["cover"] = cover_id or old.get("cover")
        for n, song in enumerate(songs, 1):
            if progress:
                progress(n, len(songs), song)
            if self.has(song["id"]):
                had += 1
                continue
            rel = self.path_for(song, transcoded=bool(max_bitrate))
            size = await source.download(song["id"], self.root / rel, max_bitrate)
            rel = self._true_suffix(rel)                     # transcoded: name it after what arrived
            self.songs[song["id"]] = {"song": song, "path": str(rel), "size": size}
            got += 1
            self.save()
        self.save()
        return got, had

    MAGIC = ((b"fLaC", "flac"), (b"OggS", "opus"), (b"ID3", "mp3"), (b"\xff\xfb", "mp3"), (b"\xff\xf3", "mp3"),
             (b"RIFF", "wav"))

    def _true_suffix(self, rel: Path) -> Path:
        path = self.root / rel
        head = path.open("rb").read(64)
        ext = next((e for magic, e in self.MAGIC if head.startswith(magic)), None)
        if head[4:8] == b"ftyp":
            ext = "m4a"
        if ext == "opus" and b"OpusHead" not in head:
            ext = "ogg"
        if not ext or path.suffix == f".{ext}":
            return rel
        new = rel.with_suffix(f".{ext}")
        path.replace(self.root / new)
        return new

    async def _save_cover(self, source, cover_id: str) -> None:
        try:
            picture = await source.cover(cover_id, 400)
        except Exception:
            return
        if picture:
            path = self.root / "covers" / f"{_safe(cover_id, 60)}.jpg"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(picture)

    def complete(self, key: str) -> tuple[int, int]:
        ids = (self.collections.get(key) or {}).get("song_ids") or []
        return sum(1 for i in ids if i in self.songs), len(ids)

    def cover_file(self, cover_id: str | None) -> Path | None:
        if not cover_id:
            return None
        p = self.root / "covers" / f"{_safe(cover_id, 60)}.jpg"
        return p if p.exists() else None

    def collection_songs(self, key: str) -> list[dict]:
        ids = (self.collections.get(key) or {}).get("song_ids") or []
        return [self.songs[i]["song"] for i in ids if i in self.songs]

    def remove(self, key: str) -> int:
        """Forget a collection; delete the files no other collection still uses."""
        gone = self.collections.pop(key, None)
        if not gone:
            return 0
        still = {i for c in self.collections.values() for i in c.get("song_ids") or []}
        removed = 0
        for song_id in gone.get("song_ids") or []:
            if song_id in still or song_id not in self.songs:
                continue
            path = self.root / self.songs.pop(song_id)["path"]
            path.unlink(missing_ok=True)
            removed += 1
            for parent in (path.parent, path.parent.parent):          # tidy empty album/artist folders
                try:
                    parent.rmdir()
                except OSError:
                    break
        self.save()
        return removed

    def size_mb(self) -> float:
        return sum(v.get("size") or 0 for v in self.songs.values()) / 1e6
