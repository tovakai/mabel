"""Structured media lookups: AniList (anime/manga), VNDB (visual novels) and MediaWiki fan wikis.

One read-only tool for anime/manga, visual novels and wiki pages. A single
JSON call replaces a web_search + web_fetch + HTML-reading detour and returns
exact fields (scores, genres, studios, dates, recommendations) instead of page
prose. AniList and VNDB hosts are fixed; wiki lookups go to any public MediaWiki
site (Fandom, wiki.gg, self-hosted fan wikis, Wikipedia) through the shared
SSRF-guarded fetcher, which refuses private addresses and pins DNS per hop.
Only the MediaWiki API is called. Results are external, untrusted data.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Any

import httpx

USER_AGENT = "Mabel/1.0 (personal assistant; media lookups)"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_OUTPUT_CHARS = 7000
ANILIST = "https://graphql.anilist.co"
VNDB = "https://api.vndb.org/kana"
_VNDB_FIELDS = ("id, title, alttitle, released, rating, votecount, length_minutes, "
                "developers.name, tags.id, tags.name, tags.rating, tags.spoiler, description")


# Questions that media_lookup answers better than web_search.
MEDIA_CUE_RE = re.compile(
    r"\b(?:anime|manga|manhwa|manhua|light\s+novels?|visual\s+novels?|vns?|eroge|otome|"
    r"anilist|myanimelist|mal|vndb|fandom|fan[-\s]?wikis?|wiki\.gg|wikis?)\b", re.I)


class LookupError_(Exception):
    pass


def _clip(text: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(text or "")))).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


async def _request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> Any:
    for attempt in range(2):
        response = await client.request(method, url, **kwargs)
        if response.status_code == 429 and attempt == 0:
            await asyncio.sleep(1.2)  # brief backoff on provider rate limits
            continue
        if response.status_code == 404:
            raise LookupError_("not found")
        response.raise_for_status()
        return response.json()
    raise LookupError_("rate limited")


# --- AniList (anime/manga; MyAnimeList ids included) ---------------------------------

_ANILIST_FIELDS = """
  id idMal title { romaji english } format status seasonYear startDate { year }
  averageScore popularity genres episodes chapters volumes siteUrl
  tags { name rank isMediaSpoiler }
  studios(isMain: true) { nodes { name } }
  staff(perPage: 4, sort: RELEVANCE) { edges { role node { name { full } } } }
"""


def _anilist_item(item: dict, *, description: int) -> dict:
    title = item.get("title") or {}
    tags = [t.get("name") for t in sorted(item.get("tags") or [], key=lambda t: -(t.get("rank") or 0))
            if not t.get("isMediaSpoiler")][:10]
    out = {
        "anilist_id": item.get("id"), "mal_id": item.get("idMal"), "title": title.get("romaji"),
        "title_english": title.get("english"), "format": item.get("format"), "status": item.get("status"),
        "year": item.get("seasonYear") or (item.get("startDate") or {}).get("year"),
        "score": item.get("averageScore"), "popularity": item.get("popularity"),
        "genres": item.get("genres"), "tags": tags, "episodes": item.get("episodes"),
        "chapters": item.get("chapters"), "volumes": item.get("volumes"),
        "studios": [n.get("name") for n in ((item.get("studios") or {}).get("nodes") or [])],
        "staff": [f"{(e.get('node') or {}).get('name', {}).get('full')} ({e.get('role')})"
                  for e in ((item.get("staff") or {}).get("edges") or [])],
        "url": item.get("siteUrl"),
    }
    if description:
        out["description"] = _clip(item.get("description"), description)
    return {k: v for k, v in out.items() if v not in (None, [], "")}


async def _anilist_query(client, query: str, variables: dict) -> dict:
    data = await _request(client, "POST", ANILIST, json={"query": query, "variables": variables})
    if data.get("errors"):
        message = str((data["errors"][0] or {}).get("message") or "query failed")
        raise LookupError_("not found" if "Not Found" in message else message[:120])
    return data.get("data") or {}


async def _anilist(client, action: str, kind: str, query: str, item_id: str, limit: int) -> dict:
    if kind not in {"anime", "manga"}:
        raise LookupError_("kind must be anime or manga for anilist")
    media_type = kind.upper()
    if action == "search":
        if not query:
            raise LookupError_("search needs a query")
        q = ("query($q:String,$t:MediaType,$n:Int){Page(perPage:$n){media(search:$q,type:$t,sort:SEARCH_MATCH){"
             + _ANILIST_FIELDS + " description(asHtml:false)}}}")
        data = await _anilist_query(client, q, {"q": query, "t": media_type, "n": limit})
        return {"results": [_anilist_item(i, description=240) for i in (data.get("Page") or {}).get("media") or []]}
    if item_id and not item_id.isdigit():
        raise LookupError_("id must be a numeric AniList id")
    selector = "id:$id" if item_id else "search:$q"
    variables = {"id": int(item_id)} if item_id else {"q": query}
    if not item_id and not query:
        raise LookupError_("details/recommendations need an id or a query")
    decl = "$id:Int,$t:MediaType" if item_id else "$q:String,$t:MediaType"
    variables["t"] = media_type
    if action == "details":
        q = (f"query({decl}){{Media({selector},type:$t){{" + _ANILIST_FIELDS
             + " description(asHtml:false) relations { edges { relationType node { title { romaji } format } } } }}")
        item = (await _anilist_query(client, q, variables)).get("Media") or {}
        result = _anilist_item(item, description=1200)
        related = [f"{e.get('relationType')}: {(e.get('node') or {}).get('title', {}).get('romaji')}"
                   for e in ((item.get("relations") or {}).get("edges") or [])]
        if related:
            result["relations"] = related[:8]
        return {"result": result}
    q = (f"query({decl},$n:Int){{Media({selector},type:$t){{id title{{romaji}} recommendations(perPage:$n,sort:RATING_DESC)"
         "{nodes{rating mediaRecommendation{id idMal title{romaji english} format averageScore genres seasonYear}}}}}")
    variables["n"] = limit
    item = (await _anilist_query(client, q, variables)).get("Media") or {}
    recs = []
    for node in ((item.get("recommendations") or {}).get("nodes") or []):
        rec = node.get("mediaRecommendation") or {}
        if rec:
            recs.append({k: v for k, v in {
                "anilist_id": rec.get("id"), "title": (rec.get("title") or {}).get("romaji"),
                "title_english": (rec.get("title") or {}).get("english"), "format": rec.get("format"),
                "score": rec.get("averageScore"), "genres": rec.get("genres"), "year": rec.get("seasonYear"),
                "votes": node.get("rating")}.items() if v not in (None, [], "")})
    return {"based_on": (item.get("title") or {}).get("romaji"), "recommendations": recs}


# --- VNDB ------------------------------------------------------------------------

def _vndb_item(item: dict, *, description: int) -> dict:
    tags = sorted((t for t in item.get("tags") or [] if not t.get("spoiler")), key=lambda t: -(t.get("rating") or 0))
    out = {
        "vndb_id": item.get("id"), "title": item.get("title"), "alttitle": item.get("alttitle"),
        "released": item.get("released"), "rating": item.get("rating"), "votes": item.get("votecount"),
        "length_hours": round(item["length_minutes"] / 60, 1) if item.get("length_minutes") else None,
        "developers": [d.get("name") for d in item.get("developers") or [] if d.get("name")],
        "tags": [t.get("name") for t in tags[:12]],
        "url": f"https://vndb.org/{item.get('id')}" if item.get("id") else None,
    }
    if description:
        out["description"] = _clip(re.sub(r"\[/?[a-z]+(?:=[^\]]*)?\]", "", str(item.get("description") or "")), description)
    return {k: v for k, v in out.items() if v not in (None, [], "")}


async def _vndb_query(client, filters, *, results: int, sort: str = "") -> list:
    # "searchrank" is only valid for a search filter; id lookups take no sort.
    sort = sort or ("searchrank" if filters and filters[0] == "search" else "id")
    body = {"filters": filters, "fields": _VNDB_FIELDS, "results": results, "sort": sort}
    if sort == "rating":
        body["reverse"] = True
    data = await _request(client, "POST", f"{VNDB}/vn", json=body)
    return data.get("results") or []


async def _vndb(client, action: str, query: str, item_id: str, limit: int) -> dict:
    if action == "search":
        if not query:
            raise LookupError_("search needs a query")
        items = await _vndb_query(client, ["search", "=", query], results=limit)
        return {"results": [_vndb_item(i, description=240) for i in items]}
    vid = item_id if item_id.startswith("v") else f"v{item_id}"
    if not re.fullmatch(r"v\d+", vid):
        raise LookupError_("details/recommendations need a VNDB id like v12345 (search first)")
    items = await _vndb_query(client, ["id", "=", vid], results=1)
    if not items:
        raise LookupError_("not found")
    if action == "details":
        return {"result": _vndb_item(items[0], description=1200)}
    # VNDB has no recommendation list: find well-rated titles sharing the top tags.
    tags = sorted((t for t in items[0].get("tags") or [] if not t.get("spoiler")), key=lambda t: -(t.get("rating") or 0))
    names = [t.get("name") for t in tags[:2] if t.get("name")]
    if not names:
        return {"recommendations": [], "note": "no tags to match on"}
    tag_ids = [t.get("id") for t in tags[:2] if t.get("id")]
    filters = ["and", ["id", "!=", vid], ["votecount", ">=", 100],
               *(["tag", "=", tid] for tid in tag_ids)] if tag_ids else ["and", ["id", "!=", vid], ["search", "=", names[0]]]
    similar = await _vndb_query(client, filters, results=limit, sort="rating")
    return {"recommendations": [_vndb_item(i, description=0) for i in similar], "matched_tags": names,
            "based_on_vndb_id": vid}


# --- MediaWiki (fan wikis: Fandom, wiki.gg, self-hosted; Wikipedia) ----------------

_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_API_PATHS: dict[str, str] = {}  # host -> discovered api.php path


def _wiki_host(value: str) -> str:
    host = (value or "en.wikipedia.org").strip().lower()
    host = re.sub(r"^https?://", "", host).split("/", 1)[0].split(":", 1)[0]
    if not _HOST.fullmatch(host):
        raise LookupError_("wiki must be a public wiki host such as lessonsinlove.fandom.com, "
                           "terraria.wiki.gg or en.wikipedia.org")
    return host


def _wiki_get(host: str, path: str, params: dict) -> Any:
    """GET through the shared SSRF-guarded, DNS-pinned, size-capped fetcher."""
    from urllib.parse import urlencode
    from src.outbound_fetch import _get_public_url
    fetched = _get_public_url(f"https://{host}{path}?{urlencode(params)}",
                              {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip",
                               "Accept": "application/json"},
                              timeout=10, max_redirects=3, max_bytes=2_000_000)
    if fetched.status_code == 404:
        raise LookupError_("not found")
    if fetched.status_code >= 400:
        raise LookupError_(f"wiki returned HTTP {fetched.status_code}")
    try:
        return json.loads(bytes(fetched.content).decode(fetched.encoding or "utf-8", "replace"))
    except ValueError:
        raise LookupError_("not a MediaWiki API response")


def _wiki_api(host: str, params: dict) -> Any:
    paths = [_API_PATHS[host]] if host in _API_PATHS else ["/api.php", "/w/api.php"]
    if host.endswith(("wikipedia.org", "wikimedia.org", "wiktionary.org")) and host not in _API_PATHS:
        paths = ["/w/api.php"]
    last: Exception | None = None
    for path in paths:
        try:
            data = _wiki_get(host, path, {**params, "format": "json"})
        except LookupError_ as error:
            last = error
            continue
        if isinstance(data, dict) and ("query" in data or "parse" in data or "error" in data):
            _API_PATHS[host] = path
            return data
    raise LookupError_(f"no MediaWiki API found on {host}" + (f" ({last})" if last else ""))


async def _wiki(client, action: str, query: str, host_value: str, limit: int) -> dict:
    host = _wiki_host(host_value)
    if not query:
        raise LookupError_("wiki lookups need a query (search terms or an exact page title)")
    if action == "search":
        data = await asyncio.to_thread(_wiki_api, host, {"action": "query", "list": "search",
                                                         "srsearch": query, "srlimit": limit})
        hits = (data.get("query") or {}).get("search") or []
        return {"wiki": host, "results": [{"title": h.get("title"), "snippet": _clip(h.get("snippet"), 200)} for h in hits]}
    data = await asyncio.to_thread(_wiki_api, host, {"action": "query", "prop": "extracts", "explaintext": 1,
                                                     "exchars": 4000, "titles": query, "redirects": 1})
    pages = list(((data.get("query") or {}).get("pages") or {}).values())
    page = pages[0] if pages else {}
    if "missing" in page:
        raise LookupError_(f"no page titled {query!r} on {host}; search first")
    text = page.get("extract")
    if not text:  # most fan wikis lack the TextExtracts extension
        parsed = await asyncio.to_thread(_wiki_api, host, {"action": "parse", "page": query, "prop": "text",
                                                           "redirects": 1})
        if "error" in parsed:
            raise LookupError_(f"no page titled {query!r} on {host}; search first")
        raw = ((parsed.get("parse") or {}).get("text") or {}).get("*", "")
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        raw = re.sub(r'<(?:aside|div)[^>]*class="[^"]*(?:navbox|toc|mw-references)[^"]*"[^>]*>.*?</(?:aside|div)>',
                     " ", raw, flags=re.S | re.I)
        text = raw
    title = page.get("title") or query
    return {"wiki": host, "title": title, "text": _clip(text, 4000),
            "url": f"https://{host}/wiki/{title.replace(' ', '_')}"}


class MediaLookupTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content or "{}")
            if not isinstance(args, dict):
                raise LookupError_("arguments must be a JSON object")
            source = str(args.get("source") or "").strip().lower()
            if not source:
                # Infer the obvious source rather than rejecting the call.
                raw_id = str(args.get("id") or "").strip().lower()
                source = ("wiki" if args.get("wiki") else "vndb" if re.fullmatch(r"v\d+", raw_id)
                          else "anilist")
            action = str(args.get("action") or "search").strip().lower()
            query = str(args.get("query") or "").strip()[:200]
            item_id = str(args.get("id") or "").strip()[:20]
            kind = str(args.get("kind") or "anime").strip().lower()
            limit = max(1, min(10, int(args.get("limit") or 5)))
            if action not in {"search", "details", "recommendations"}:
                raise LookupError_("action must be search, details or recommendations")
            # Wikimedia rejects clients advertising brotli/zstd; gzip works everywhere.
            async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                         follow_redirects=False) as client:
                if source == "anilist":
                    data = await _anilist(client, action, kind, query, item_id, limit)
                elif source == "vndb":
                    data = await _vndb(client, action, query, item_id, limit)
                elif source == "wiki":
                    if action == "recommendations":
                        raise LookupError_("wiki supports search and details")
                    data = await _wiki(client, action, query, str(args.get("wiki") or ""), limit)
                else:
                    raise LookupError_("source must be anilist, vndb or wiki")
            output = json.dumps({"source": source, "action": action, **data}, ensure_ascii=False)
            if len(output) > MAX_OUTPUT_CHARS:
                output = output[:MAX_OUTPUT_CHARS] + "…[truncated]"
            return {"output": output, "exit_code": 0, "evidence_status": "available"}
        except (LookupError_, ValueError, TypeError) as error:
            return {"error": f"media_lookup: {error}", "exit_code": 1}
        except httpx.HTTPError as error:
            return {"error": f"media_lookup: provider request failed ({type(error).__name__})", "exit_code": 1}
