"""The Gremlin Index (tk-index) for the INVENTORY channel: its HTTP API and a port of its place model.

The model mirrors tk-index's web UI (static/js/core.js and views.js) so the TUI zooms the same
way: routes "" (home), room/KON, item/00000002, code/DES, area/DES0001, level/DRWLHE0001/3, with
the same redirects that skip pages holding a single thing, the same titles, codes and counts.
Writes go through tk-index's own endpoints, so every change is in its undo history.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx

VIEW_RANK = {"drawers": 0, "shelves": 1, "gridfinity": 2, "compartments": 3, "pegboard": 4, "list": 9}
ARTICLES = re.compile(r"^(the|a|an|den|det|de|en|ett)\s+", re.I)
ENDINGS = ["erna", "arna", "orna", "na", "en", "et", "er", "ar", "or", "n", "t", "s"]
CELL = re.compile(r"^C(\d\d)R(\d\d)$")


def POS(a: str) -> str:
    return a[10:]


def KEY(a: str) -> str:
    return a[:10]


def LET(area: str) -> str:
    return area[:3]


class IndexError_(RuntimeError):
    pass


class TKIndex:
    """tk-index's JSON API (no auth; loopback, or the tailnet via tailscale serve)."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.url, timeout=20)

    async def close(self) -> None:
        await self.http.aclose()

    async def get(self, path: str, **params):
        resp = await self.http.get(f"/api/{path}", params=params or None)
        if resp.status_code >= 300:
            raise IndexError_(_error(resp))
        return resp.json()

    async def post(self, path: str, data: dict | None = None):
        resp = await self.http.post(f"/api/{path}", json=data or {})
        if resp.status_code >= 300:
            raise IndexError_(_error(resp))
        return resp.json() if resp.content else {}

    async def world(self) -> "World":
        return World(await self.get("world"))

    async def item(self, item_id: str) -> dict:
        return await self.get(f"items/{item_id}")

    async def image(self, path: str) -> bytes | None:
        resp = await self.http.get(f"/images/{path}")
        return resp.content if resp.status_code == 200 else None


def _error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error") or resp.text)[:300]
    except ValueError:
        return f"{resp.status_code}: {resp.text[:200]}"


@dataclass
class Card:
    route: str
    title: str
    code: str
    count: int


@dataclass
class Zone:
    """One drawing in an area view: a grid, a stack of levels, a sorted shelf or a list."""
    kind: str            # grid | levels | media | list
    layout: dict
    data: dict = field(default_factory=dict)


class World:
    def __init__(self, w: dict):
        self.w = w
        self.layouts: list[dict] = w.get("layouts") or []
        self.items: list[dict] = w.get("items") or []
        self.synonyms: dict = w.get("synonyms") or {}
        self.icons: dict = w.get("icons") or {}
        self._eq: dict[str, list[str]] = {}
        self._index()

    # ── index (core.js index()) ─────────────────────────────────────────

    def _index(self) -> None:
        self.L, self.I, self.on, self.kids, self.hosted, self.area, self.sit = {}, {}, {}, {}, {}, {}, {}
        self.names, self.nick = {}, {}
        for l in self.layouts:
            self.L[l["key"]] = l
            self.area.setdefault(l["area"], []).append(l)
            if l.get("host_item_id"):
                self.hosted.setdefault(l["host_item_id"], []).append(l)
            if l.get("parent_address"):
                self.sit.setdefault(KEY(l["parent_address"]), []).append(l)
        for i in self.items:
            self.I[i["id"]] = i
            if i.get("address"):
                self.on.setdefault(KEY(i["address"]), []).append(i)
            if i.get("parent_item_id"):
                self.kids.setdefault(i["parent_item_id"], []).append(i)
        for a in self.w.get("aliases") or []:
            if a.get("display"):
                self.names[a["target"]] = a["alias"]
            elif re.fullmatch(r"\d{8}", str(a.get("target"))):
                self.nick.setdefault(a["target"], []).append(a["alias"])
        for ls in self.area.values():
            ls.sort(key=lambda l: VIEW_RANK.get(l.get("view"), 9))
        self.cL, self.cI = {}, {}
        for i in self.items:
            if not i.get("address") or i.get("status") == "retired":
                continue
            seen, a = set(), i["address"]
            while a:
                l = self.L.get(KEY(a))
                if not l or l["key"] in seen:
                    break
                seen.add(l["key"])
                self.cL[l["key"]] = self.cL.get(l["key"], 0) + 1
                if l.get("host_item_id"):
                    self.cI[l["host_item_id"]] = self.cI.get(l["host_item_id"], 0) + 1
                    a = (self.I.get(l["host_item_id"]) or {}).get("address")
                else:
                    a = l.get("parent_address")

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def is_root(l: dict) -> bool:
        return not l.get("host_item_id") and not l.get("parent_address")

    def roots_of(self, R: str) -> list[dict]:
        return [l for l in self.layouts if self.is_root(l) and LET(l["area"]) == R]

    def room_codes(self) -> list[str]:
        return list(dict.fromkeys(LET(l["area"]) for l in self.layouts if self.is_root(l)))

    def code_layouts(self, X: str) -> list[dict]:
        return [l for l in self.layouts if LET(l["area"]) == X and not l.get("host_item_id") and l.get("parent_address")]

    @staticmethod
    def areas_of(ls: list[dict]) -> list[str]:
        return list(dict.fromkeys(l["area"] for l in ls))

    @staticmethod
    def top_only(items: list[dict]) -> list[dict]:
        by = {i["id"]: i for i in items}
        return [i for i in items if not (i.get("parent_item_id") and i["parent_item_id"] in by
                                         and by[i["parent_item_id"]].get("address") == i.get("address"))]

    def kids_of(self, item_id: str) -> list[dict]:
        own = (self.I.get(item_id) or {}).get("address")
        return [k for k in self.kids.get(item_id, []) if k.get("address") and k["address"] == own]

    def host_code(self, item_id: str) -> str | None:
        codes = list(dict.fromkeys(LET(l["area"]) for l in self.hosted.get(item_id, [])))
        if len(codes) != 1:
            return None
        X = codes[0]
        own = (self.I.get(item_id) or {}).get("address")
        if own and self.L.get(KEY(own)) and LET(self.L[KEY(own)]["area"]) == X:
            return None
        for l in self.layouts:
            if LET(l["area"]) == X and l.get("host_item_id") and l["host_item_id"] != item_id:
                where = self.L.get(KEY((self.I.get(l["host_item_id"]) or {}).get("address") or ""))
                if LET((where or {}).get("area", "")) != X:
                    return None
        return X

    # ── routes ──────────────────────────────────────────────────────────

    def place_route(self, addr: str | None) -> str:
        if not addr:
            return ""
        l = self.L.get(KEY(addr))
        if not l:
            return ""
        if self.is_root(l):
            return "room/" + LET(l["area"])
        if l.get("position_format") == "level":
            return f"level/{l['key']}/{addr[10]}"
        return "area/" + l["area"]

    def redirect(self, r: str) -> str | None:
        k, a, b = (r.split("/") + ["", "", ""])[:3]
        if k == "item":
            ar = self.areas_of(self.hosted.get(a, []))
            if len(ar) == 1 and not self.kids_of(a):
                return "area/" + ar[0]
        if k == "code":
            ar = self.areas_of(self.code_layouts(a))
            if len(ar) == 1:
                return "area/" + ar[0]
        if k == "level":
            its = [i for i in self.on.get(a, []) if i["address"][10] == b]
            st = [l for l in self.sit.get(a, []) if l["parent_address"][10] == b]
            if not its and len(st) == 1:
                return "area/" + st[0]["area"]
        return None

    def resolve(self, r: str) -> str:
        for _ in range(6):
            x = self.redirect(r)
            if not x or x == r:
                break
            r = x
        return r

    def layout_parent(self, l: dict) -> str:
        if l.get("host_item_id"):
            return "item/" + l["host_item_id"]
        if l.get("parent_address"):
            pr = self.place_route(l["parent_address"])
            if pr.startswith("room/") and len(self.areas_of(self.code_layouts(LET(l["area"])))) > 1:
                return "code/" + LET(l["area"])
            return pr
        return "room/" + LET(l["area"])

    def parent_of(self, r: str) -> str | None:
        k, a = (r.split("/") + ["", ""])[:2]
        if not k:
            return None
        if k == "room":
            return ""
        if k == "item":
            return self.place_route((self.I.get(a) or {}).get("address"))
        if k == "code":
            ls = self.code_layouts(a)
            return self.place_route(ls[0]["parent_address"]) if ls else ""
        if k == "area":
            ls = self.area.get(a, [])

            def leads_back(x):
                host = self.I.get(x.get("host_item_id") or "") or {}
                return x.get("host_item_id") and (self.L.get(KEY(host.get("address") or "")) or {}).get("area") == a
            l = next((x for x in ls if not self.is_root(x) and not leads_back(x)), None) \
                or next((x for x in ls if not self.is_root(x)), None) or (ls[0] if ls else None)
            if not l:
                return ""
            return "room/" + LET(l["area"]) if self.is_root(l) else self.layout_parent(l)
        if k == "level":
            return "area/" + (self.L.get(a) or {}).get("area", "")
        return ""

    def chain(self, r: str) -> list[str]:
        out, c, n = [], r, 0
        while c is not None and n < 25 and c not in out:
            n += 1
            out.insert(0, c)
            c = self.parent_of(c)
        return [x for i, x in enumerate(out) if i == len(out) - 1 or self.resolve(x) == x]

    def valid(self, r: str) -> bool:
        k, a, b = (r.split("/") + ["", "", ""])[:3]
        if not k:
            return True
        return {"room": lambda: bool(self.roots_of(a)), "item": lambda: a in self.I,
                "code": lambda: bool(self.code_layouts(a)), "area": lambda: a in self.area,
                "level": lambda: a in self.L and bool(re.fullmatch(r"\d", b))}.get(k, lambda: False)()

    # ── names ───────────────────────────────────────────────────────────

    def level_word(self, l: dict | None) -> str:
        view = (l or {}).get("view")
        return "Drawer" if view == "drawers" else "Shelf" if view == "shelves" else "Level"

    def sole_host_of_area(self, A: str) -> str | None:
        ls = self.area.get(A, [])
        h = ls[0].get("host_item_id") if ls else None
        if h and all(l.get("host_item_id") == h for l in ls) and self.resolve("item/" + h) == "area/" + A:
            return h
        return None

    def title(self, r: str) -> str:
        k, a, b = (r.split("/") + ["", "", ""])[:3]
        if not k:
            return "Home"
        if k == "room":
            roots = self.roots_of(a)
            return self.names.get(a) or (roots[0]["label"] if roots else a)
        if k == "item":
            c = self.host_code(a)
            return (c and self.names.get(c)) or (self.I.get(a) or {}).get("name") or a
        if k == "code":
            return self.names.get(a) or a
        if k == "area":
            if a in self.names:
                return self.names[a]
            h = self.sole_host_of_area(a)
            if h:
                return self.title("item/" + h)
            ls = self.area.get(a, [])
            l = next((x for x in ls if x.get("prefix") == "STD"), ls[0] if ls else None)
            return (l or {}).get("label") or a
        if k == "level":
            return self.names.get(a + b + "00000") or f"{self.level_word(self.L.get(a))} {b}"
        return r

    def code(self, r: str) -> str:
        k, a, b = (r.split("/") + ["", "", ""])[:3]
        if k == "room":
            return " · ".join(l["key"] for l in self.roots_of(a))
        if k == "item":
            c = self.host_code(a)
            return (c + " · " if c else "") + "#" + a
        if k == "code":
            return a
        if k == "area":
            ls = self.area.get(a, [])
            size = f" · {ls[0]['columns']}×{ls[0]['rows']}" if ls and ls[0].get("columns") else ""
            return "+".join(l["prefix"] for l in ls) + " " + a + size
        if k == "level":
            return a + b + "00000"
        return ""

    def count(self, r: str) -> int:
        k, a, b = (r.split("/") + ["", "", ""])[:3]
        if k == "room":
            return sum(self.cL.get(l["key"], 0) for l in self.roots_of(a))
        if k == "item":
            return self.cI.get(a) or len(self.kids.get(a, []))
        if k == "code":
            return sum(self.cL.get(l["key"], 0) for l in self.code_layouts(a))
        if k == "area":
            return sum(self.cL.get(l["key"], 0) for l in self.area.get(a, []))
        if k == "level":
            return len([i for i in self.on.get(a, []) if i["address"][10] == b]) + \
                sum(self.cL.get(x["key"], 0) for x in self.sit.get(a, []) if x["parent_address"][10] == b)
        return 0

    def card(self, r: str) -> Card:
        return Card(r, self.title(r), self.code(r), self.count(r))

    def place_title(self, addr: str | None) -> str:
        r = self.place_route(addr)
        if not r:
            return addr or "no home"
        l = self.L.get(KEY(addr))
        t = self.title(r)
        if l and l.get("position_format") == "grid":
            return f"{t} · {POS(addr)}"
        if l and l.get("position_format") == "seq" and POS(addr) != "000000":
            return f"{t} · spot {int(POS(addr))}"
        return t

    # ── what a page shows (views.js) ────────────────────────────────────

    def cards_for(self, items: list[dict], layouts: list[dict]) -> tuple[list[Card], list[dict]]:
        cards, things, seen = [], [], set()
        for i in items:
            if self.hosted.get(i["id"]):
                cards.append(self.card("item/" + i["id"]))
            else:
                things.append(i)
        for l in layouts:
            X = LET(l["area"])
            if l.get("parent_address") and self.place_route(l["parent_address"]).startswith("room/") \
                    and len(self.areas_of(self.code_layouts(X))) > 1:
                if X in seen:
                    continue
                seen.add(X)
                cards.append(self.card("code/" + X))
            else:
                cards.append(self.card("area/" + l["area"]))
        return cards, things

    def room_content(self, R: str) -> tuple[list[dict], list[dict]]:
        roots = self.roots_of(R)
        items = self.top_only([i for l in roots for i in self.on.get(l["key"], [])])
        lays = [x for l in roots for x in self.sit.get(l["key"], []) if not x.get("host_item_id")]
        return items, lays

    def page(self, r: str) -> dict:
        """{"cards": [...], "things": [...], "things_head": str, "zones": [...], "inline": (title, route)}."""
        k, a, b = (r.split("/") + ["", "", ""])[:3]
        out = {"cards": [], "things": [], "things_head": "Things here", "zones": [], "inline": None}
        if not k:
            rooms = sorted(self.room_codes(), key=lambda R: (self.count("room/" + R) == 0, self.title("room/" + R).lower()))
            out["cards"] = [self.card("room/" + R) for R in rooms]
        elif k == "room":
            items, lays = self.room_content(a)
            out["cards"], out["things"] = self.cards_for(items, lays)
        elif k == "item":
            areas = self.areas_of(self.hosted.get(a, []))
            levelled = [x for x in areas if any(l.get("position_format") == "level" for l in self.area.get(x, []))]
            inline = levelled[0] if len(levelled) == 1 else None
            out["cards"] = [self.card("area/" + x) for x in areas if x != inline]
            if inline:
                out["inline"] = (self.title("area/" + inline), "area/" + inline)
                out["zones"] = self.zones(inline)
            out["things"], out["things_head"] = self.kids_of(a), "Inside"
        elif k == "code":
            out["cards"] = [self.card("area/" + x) for x in self.areas_of(self.code_layouts(a))]
        elif k == "area":
            out["zones"] = self.zones(a)
        elif k == "level":
            its = self.top_only([i for i in self.on.get(a, []) if i["address"][10] == b])
            st = [x for x in self.sit.get(a, []) if x["parent_address"][10] == b]
            out["cards"], out["things"] = self.cards_for(its, st)
            out["things_head"] = "In this " + self.level_word(self.L.get(a)).lower()
        return out

    def zones(self, A: str) -> list[Zone]:
        out = []
        for l in self.area.get(A, []):
            fmt = l.get("position_format")
            if fmt == "grid":
                out.append(Zone("grid", l, self.grid(l)))
            elif fmt == "level":
                out.append(Zone("levels", l, {"levels": self.levels(l)}))
            elif fmt == "sorted":
                out.append(Zone("media", l, {"items": self.in_order(self.top_only(self.on.get(l["key"], [])))}))
            else:
                its = self.top_only(self.on.get(l["key"], []))
                cards, _ = self.cards_for([i for i in its if self.hosted.get(i["id"])], self.sit.get(l["key"], []))
                plain = [i for i in its if not self.hosted.get(i["id"])]
                spots: dict[str, list] = {}
                for i in plain:
                    spots.setdefault(POS(i["address"]), []).append(i)
                out.append(Zone("list", l, {"cards": cards, "spots": dict(sorted(spots.items()))}))
        return out

    def grid_dims(self, l: dict) -> tuple[int, int]:
        c, r = l.get("columns") or 0, l.get("rows") or 0
        for i in self.on.get(l["key"], []):
            m = CELL.match(POS(i["address"]))
            if m:
                c = max(c, int(m[1]) + (i.get("span_columns") or 1) - 1)
                r = max(r, int(m[2]) + (i.get("span_rows") or 1) - 1)
        return c or 1, r or 1

    def grid(self, l: dict) -> dict:
        C, R = self.grid_dims(l)
        top = self.top_only([i for i in self.on.get(l["key"], []) if CELL.match(POS(i["address"]))])
        blocks = []
        for i in top:
            m = CELL.match(POS(i["address"]))
            blocks.append({"item": i, "c": int(m[1]), "r": int(m[2]), "w": i.get("span_columns") or 1,
                           "h": i.get("span_rows") or 1, "kids": len(self.kids.get(i["id"], [])),
                           "assumed": bool(i.get("span_columns")) and not i.get("footprint_confirmed"),
                           "unknown": not i.get("span_columns")})
        subs = []
        for s in self.sit.get(l["key"], []):
            m = CELL.match(POS(s["parent_address"]))
            if m:
                subs.append({"layout": s, "c": int(m[1]), "r": int(m[2]), "route": "area/" + s["area"]})
        return {"cols": C, "rows": R, "blocks": blocks, "subs": subs, "compartments": l.get("view") == "compartments"}

    def levels(self, l: dict) -> list[dict]:
        n = max([l.get("levels") or 0, 1] + [int(i["address"][10]) for i in self.on.get(l["key"], [])]
                + [int(s["parent_address"][10]) for s in self.sit.get(l["key"], [])])
        out = []
        for f in range(n, 0, -1):
            F, addr = str(f), l["key"] + str(f) + "00000"
            out.append({"route": f"level/{l['key']}/{F}", "name": self.names.get(addr) or f"{self.level_word(l)} {F}",
                        "where": "top" if f == n else "bottom" if f == 1 else "",
                        "subs": [s for s in self.sit.get(l["key"], []) if s["parent_address"][10] == F],
                        "items": self.top_only([i for i in self.on.get(l["key"], []) if i["address"][10] == F])})
        return out

    # ── sorting and search ──────────────────────────────────────────────

    @staticmethod
    def sort_title(i: dict) -> str:
        return (i.get("sort_key") or ARTICLES.sub("", str(i.get("name") or "").strip())).strip()

    @classmethod
    def in_order(cls, items: list[dict]) -> list[dict]:
        def collate(t: str) -> str:
            t = t.lower()
            t = re.sub(r"[åäöæø]", lambda m: {"å": "{1", "ä": "{2", "ö": "{3", "æ": "{2", "ø": "{3"}[m[0]], t)
            return re.sub(r"\d+", lambda m: m[0].zfill(8), t)
        return sorted(items, key=lambda i: (collate(cls.sort_title(i)), i["id"]))

    def equivalents(self, t: str) -> list[str]:
        if t in self._eq:
            return self._eq[t]
        found = set()
        forms = [t] + [t[: -len(e)] for e in ENDINGS if t.endswith(e) and len(t) - len(e) >= 3]
        for w in forms:
            if w in self.synonyms:
                found.add(w)
                found.update(self.synonyms[w])
        if not found and len(t) >= 4:
            for w, others in self.synonyms.items():
                if w.startswith(t):
                    found.add(w)
                    found.update(others)
        found.discard(t)
        self._eq[t] = list(found)
        return self._eq[t]

    def matches(self, i: dict, q: str) -> bool:
        if not q:
            return True
        hay = " ".join(str(i.get(k) or "") for k in ("name", "id", "address", "spec_code", "category",
                                                     "sort_key", "search_words"))
        hay = (hay + " " + " ".join(self.nick.get(i["id"], []))).lower()
        starts = " " + re.sub(r"[^a-z0-9åäöé]+", " ", hay)
        return all(t in hay or any(" " + o in starts for o in self.equivalents(t)) for t in q.lower().split())

    def search(self, q: str, status: str = "") -> list[dict]:
        live = [i for i in self.items if i.get("status") != "retired"]
        if status:
            live = [i for i in live if i.get("status") == status]
        return self.in_order([i for i in live if self.matches(i, q)])[:200]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for i in self.items:
            counts[i.get("status") or "?"] = counts.get(i.get("status") or "?", 0) + 1
        return counts
