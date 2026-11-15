"""Navidrome (OpenSubsonic) client for Mabel's music player.

Navidrome stays its own service (on the other PC); Mabel's backend talks to
it so the browser never holds the credentials and streams come from Mabel's
own origin (CSP media-src 'self', works over the tailnet on the phone).

Settings (data/settings.json):
  music_navidrome_url       e.g. http://host:4533
  music_navidrome_user
  music_navidrome_password  stored encrypted (src/secret_storage, "enc:" prefix)

Auth is Subsonic token auth: t = md5(password + salt), a fresh salt per call.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

import httpx

API_VERSION = "1.16.1"
CLIENT = "mabel"
TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# What the browser may ask for. Reads are GET at /api/music/rest/<method>;
# writes must be POST there (Subsonic itself is GET-only, so a cross-site
# <img src> could otherwise star songs or edit playlists).
READ_METHODS = frozenset({
    "ping", "search3", "getAlbum", "getArtist", "getArtists", "getAlbumList2",
    "getRandomSongs", "getStarred2", "getPlaylists", "getPlaylist", "getSimilarSongs2",
    "getTopSongs", "getLyricsBySongId", "getSong", "getGenres", "getSongsByGenre",
    "getArtistInfo2", "getAlbumInfo2", "getNowPlaying",
})
WRITE_METHODS = frozenset({
    "star", "unstar", "scrobble", "createPlaylist", "updatePlaylist", "deletePlaylist",
})
# Never forwarded from the browser: Mabel sets these.
RESERVED_PARAMS = frozenset({"u", "p", "t", "s", "c", "v", "f", "apiKey"})


class MusicError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Config:
    url: str
    user: str
    password: str

    @property
    def configured(self) -> bool:
        return bool(self.url and self.user and self.password)


def _setting(key: str, default=""):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def load_config() -> Config:
    password = str(_setting("music_navidrome_password", "") or "")
    if password:
        try:
            from src.secret_storage import decrypt
            password = decrypt(password)
        except Exception:
            password = ""
    return Config(
        url=str(_setting("music_navidrome_url", "") or "").rstrip("/"),
        user=str(_setting("music_navidrome_user", "") or ""),
        password=password,
    )


def save_config(url: str, user: str, password: str) -> None:
    from src.secret_storage import encrypt
    from src.settings import load_settings, save_settings
    settings = dict(load_settings())
    settings["music_navidrome_url"] = url.rstrip("/")
    settings["music_navidrome_user"] = user
    settings["music_navidrome_password"] = encrypt(password) if password else ""
    save_settings(settings)


def clear_config() -> None:
    from src.settings import load_settings, save_settings
    settings = dict(load_settings())
    for key in ("music_navidrome_url", "music_navidrome_user", "music_navidrome_password"):
        settings.pop(key, None)
    save_settings(settings)


def normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise MusicError("The Navidrome address must start with http:// or https://", 400)
    for suffix in ("/app", "/rest"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def auth_params(cfg: Config) -> dict:
    salt = secrets.token_hex(8)
    token = hashlib.md5((cfg.password + salt).encode("utf-8")).hexdigest()  # noqa: S324 - Subsonic protocol
    return {"u": cfg.user, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT, "f": "json"}


def rest_url(cfg: Config, method: str) -> str:
    return f"{cfg.url}/rest/{method}"


def clean_params(items) -> list[tuple[str, str]]:
    """Browser query/form pairs minus the ones Mabel owns. Keeps repeats (id=a&id=b)."""
    return [(k, str(v)) for k, v in items if k not in RESERVED_PARAMS]


def unwrap(payload: dict) -> dict:
    body = (payload or {}).get("subsonic-response")
    if not isinstance(body, dict):
        raise MusicError("Navidrome sent something that isn't a Subsonic response")
    if body.get("status") != "ok":
        err = body.get("error") or {}
        code = err.get("code")
        # 40/41: wrong credentials / token auth not supported
        status = 401 if code in (40, 41, 44) else 502
        raise MusicError(f"Navidrome: {err.get('message') or 'request failed'}", status)
    return body


async def call(cfg: Config, method: str, params: list[tuple[str, str]] | None = None,
               client: httpx.AsyncClient | None = None) -> dict:
    if not cfg.configured:
        raise MusicError("Music isn't connected to Navidrome yet.", 409)
    query = list(auth_params(cfg).items()) + list(params or [])
    try:
        if client is not None:
            resp = await client.get(rest_url(cfg, method), params=query)
        else:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                resp = await c.get(rest_url(cfg, method), params=query)
    except httpx.HTTPError as exc:
        raise MusicError(f"Couldn't reach Navidrome at {cfg.url} ({type(exc).__name__})") from exc
    if resp.status_code >= 400:
        raise MusicError(f"Navidrome answered HTTP {resp.status_code}")
    try:
        return unwrap(resp.json())
    except ValueError as exc:
        raise MusicError("Navidrome sent invalid JSON") from exc
