"""Offline music: downloads, the index, half-done downloads, removal, playing from disk."""
import asyncio
from pathlib import Path

from mabel_tui.offline import Offline


class FakeSource:
    def __init__(self, fail_on=None):
        self.fail_on, self.fetched = fail_on, []

    async def download(self, song_id, path: Path, max_bitrate=0):
        if song_id == self.fail_on:
            raise RuntimeError("network gone")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fLaC" + b"x" * 6)
        self.fetched.append(song_id)
        return 10

    async def cover(self, cover_id, size=300):
        return b"\xff\xd8jpeg"


def song(i, album="Album"):
    return {"id": f"s{i}", "title": f"Song/{i}", "artist": "Artist", "album": album, "track": i, "suffix": "flac",
            "coverArt": f"al-{album}"}


def test_download_index_and_local_paths(tmp_path):
    off = Offline(tmp_path)
    songs = [song(1), song(2)]
    got, had = asyncio.run(off.download(FakeSource(), songs, key="album:a", kind="album", name="Album", cover_id="al-Album"))
    assert (got, had) == (2, 0)
    assert off.local("s1").endswith("Artist/Album/01 - Song_1.flac")           # / in a title can't make folders
    assert off.cover_file("al-Album") is not None
    again = Offline(tmp_path)                                                    # survives a restart
    assert again.complete("album:a") == (2, 2) and [s["id"] for s in again.collection_songs("album:a")] == ["s1", "s2"]
    src = FakeSource()
    assert asyncio.run(again.download(src, songs, key="album:a", kind="album", name="Album")) == (0, 2)
    assert src.fetched == []                                                     # nothing fetched twice


def test_a_broken_download_shows_half_done_and_resumes(tmp_path):
    off = Offline(tmp_path)
    songs = [song(1), song(2), song(3)]
    try:
        asyncio.run(off.download(FakeSource(fail_on="s2"), songs, key="album:a", kind="album", name="Album"))
    except RuntimeError:
        pass
    assert off.complete("album:a") == (1, 3)
    assert not list(tmp_path.rglob("*.part"))
    src = FakeSource()
    asyncio.run(off.download(src, songs, key="album:a", kind="album", name="Album"))
    assert src.fetched == ["s2", "s3"] and off.complete("album:a") == (3, 3)


def test_remove_keeps_songs_another_download_still_uses(tmp_path):
    off = Offline(tmp_path)
    asyncio.run(off.download(FakeSource(), [song(1), song(2)], key="album:a", kind="album", name="Album"))
    asyncio.run(off.download(FakeSource(), [song(2)], key="playlist:p", kind="playlist", name="Mix"))
    assert off.remove("album:a") == 1
    assert not off.has("s1") and off.has("s2") and Path(off.local("s2")).exists()
    assert off.remove("playlist:p") == 1
    assert not (tmp_path / "Artist").exists()                                     # empty folders tidied


def test_a_transcoded_download_is_named_after_its_real_format(tmp_path):
    class OpusSource(FakeSource):
        async def download(self, song_id, path, max_bitrate=0):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"OggS\x00\x02" + b"\x00" * 22 + b"OpusHead" + b"\x00" * 20)
            return 56
    off = Offline(tmp_path)
    asyncio.run(off.download(OpusSource(), [song(1)], key="song:s1", kind="song", name="Song", max_bitrate=96))
    assert off.local("s1").endswith("01 - Song_1.opus") and Path(off.local("s1")).exists()
