"""manage_gremlin: Mabel's hands on the Gremlin Index (Anthon's workshop inventory).

The index is his own project, tk-index (~/Documents/tk-index): a SQLite master
copy with a checkout log and an undo log, plus a web UI. Its ``db.py`` is the
shared layer, so this tool imports it instead of touching tables itself.

She can look things up (find, where, out) and do the bookkeeping he asked for:
check something out and return it. Moving, adding and editing stay in his web
UI. Every write runs inside ``db.recorded(..., actor="mabel")``, so it shows up
in the web UI's history and can be undone there like any of his own changes.

Setting ``gremlin_index_path`` points at the database (default below); the
package is imported from ``<repo>/src`` next to it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPO = "~/Documents/tk-index"
DEFAULT_DB = DEFAULT_REPO + "/gremlin.sqlite3"
ACTOR = "mabel"
MAX_RESULTS = 12
_ARTICLES = re.compile(r"^(?:the|my|a|an|that|this|those|these|some)\s+", re.I)


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def db_path() -> Path:
    return Path(str(_setting("gremlin_index_path", DEFAULT_DB) or DEFAULT_DB)).expanduser()


def load_inventory(path: Path):
    """tk_index.db: already importable, else from the repo next to the database or setting gremlin_index_repo."""
    try:
        return importlib.import_module("tk_index.db")
    except ImportError:
        repo = Path(str(_setting("gremlin_index_repo", "") or DEFAULT_REPO)).expanduser()
        for src in (path.parent / "src", repo / "src"):
            if (src / "tk_index" / "db.py").exists():
                sys.path.insert(0, str(src))
                return importlib.import_module("tk_index.db")
        raise


def open_index(path: Path | None = None):
    path = path or db_path()
    if not path.exists():
        raise FileNotFoundError(f"The Gremlin Index database isn't at {path} (setting gremlin_index_path).")
    inv = load_inventory(path)
    return inv, inv.connect(path)


# ── reading ─────────────────────────────────────────────────────────────

def _clean_query(text: str) -> str:
    text = " ".join(str(text or "").split()).strip(" .,!?\"'")
    return _ARTICLES.sub("", text)


def search(inv, db, text: str, *, include_retired: bool = False) -> list[dict]:
    """Whole phrase first; if that finds nothing, items matching every word."""
    query = _clean_query(text)
    if not query:
        return []
    found = inv.search_items(db, query)
    if not found:
        words = [w for w in re.findall(r"[\w×-]+", query) if len(w) >= 2]
        hits = None
        for word in words:
            ids = {i["id"]: i for i in inv.search_items(db, word)}
            hits = ids if hits is None else {k: v for k, v in hits.items() if k in ids}
        found = list((hits or {}).values())
    if not include_retired:
        found = [i for i in found if i["status"] != "retired"]
    return found


def where(inv, db, item_id: str) -> str:
    """Outermost place first: 'Kontoret › Left side (Main desktop) › White SKÅDIS, C04R03'."""
    steps = inv.item_path(db, item_id)
    parts = []
    for step in reversed(steps):
        if "container" in step:
            parts.append(f"inside {step['container']['name']} ({step['container']['id']})")
            continue
        label = step.get("label") or step.get("layout") or "?"
        host = step.get("host")
        parts.append(f"{label} ({host['name']})" if host and host.get("name") and host["name"] != label else label)
    containers = [p for p in parts if p.startswith("inside ")]
    places = [p for p in parts if not p.startswith("inside ")]
    line = " › ".join(places + containers)
    address = inv.get_item(db, item_id)["address"]
    if address:
        position = address[10:]
        if position.strip("0"):
            line += f", spot {position}"
        line += f"  [{address}]"
    return line or "no home yet (unplaced)"


def _brief(inv, db, item: dict, *, with_place: bool = True) -> dict:
    out = {"id": item["id"], "name": item["name"], "status": item["status"]}
    if item.get("spec_code"):
        out["spec"] = item["spec_code"]
    if item.get("quantity") is not None:
        out["quantity"] = item["quantity"]
    if with_place:
        out["home"] = where(inv, db, item["id"]) if item.get("address") else "no home yet"
    if item.get("checkout_id"):
        out["out_since"] = item["checked_out_at"] + " UTC"
        if item.get("checked_out_to"):
            out["out_to"] = item["checked_out_to"]
        if item.get("checkout_reason"):
            out["out_reason"] = item["checkout_reason"]
    return out


def checked_out(inv, db) -> list[dict]:
    """What's out right now; things that went along inside a container are listed under it."""
    rows = db.execute("SELECT c.*, i.name FROM checkouts c JOIN items i ON i.id=c.item_id "
                      "WHERE c.returned_at IS NULL ORDER BY c.checked_out_at").fetchall()
    tops = [dict(r) for r in rows if r["group_id"] is None]
    inside: dict[int, list[str]] = {}
    for r in rows:
        if r["group_id"] is not None:
            inside.setdefault(r["group_id"], []).append(r["name"])
    out = []
    for row in tops:
        entry = {"id": row["item_id"], "name": row["name"], "out_since": row["checked_out_at"] + " UTC",
                 "days_out": days_since(row["checked_out_at"])}
        if row["destination"]:
            entry["out_to"] = row["destination"]
        if row["reason"]:
            entry["reason"] = row["reason"]
        if inside.get(row["id"]):
            entry["with_it"] = inside[row["id"]]
        entry["home"] = where(inv, db, row["item_id"])
        out.append(entry)
    return out


def days_since(sqlite_utc: str, now: datetime | None = None) -> int:
    then = datetime.fromisoformat(sqlite_utc.replace(" ", "T")).replace(tzinfo=timezone.utc)
    return max(0, ((now or datetime.now(timezone.utc)) - then).days)


# ── the tool ────────────────────────────────────────────────────────────

READS = ("find", "where", "out")
WRITES = ("checkout", "return")


def _resolve(inv, db, ref: str, *, prefer: str) -> tuple[dict | None, list[dict]]:
    """One item for a write, or the candidates to ask about. An 8-digit ID is exact."""
    ref = str(ref or "").strip()
    if re.fullmatch(r"\d{1,8}", ref):
        try:
            return inv.item_dict(inv.get_item(db, ref.zfill(8))), []
        except Exception:
            return None, []
    found = search(inv, db, ref)
    if len(found) == 1:
        return found[0], []
    preferred = [i for i in found if i["status"] == prefer]
    if len(preferred) == 1:
        return preferred[0], []
    exact = [i for i in (preferred or found) if i["name"].casefold() == _clean_query(ref).casefold()]
    if len(exact) == 1:
        return exact[0], []
    return None, preferred or found


def run(args: dict, *, path: Path | None = None) -> dict:
    """Synchronous core; returns the tool result dict."""
    action = str(args.get("action") or "").strip().lower().replace("-", "_")
    action = {"search": "find", "lookup": "find", "locate": "where", "list_out": "out", "away": "out",
              "check_out": "checkout", "borrow": "checkout", "returned": "return", "back": "return",
              "put_back": "return"}.get(action, action)
    if action not in READS + WRITES:
        return {"error": f"Unknown action {action!r}. Use one of: {', '.join(READS + WRITES)}.", "exit_code": 1}
    try:
        inv, db = open_index(path)
    except Exception as exc:
        return {"error": str(exc), "exit_code": 1}
    try:
        return _dispatch(inv, db, action, args)
    finally:
        db.close()


def _dispatch(inv, db, action: str, args: dict) -> dict:
    ref = args.get("item") or args.get("query") or args.get("name") or args.get("id") or ""
    if action == "out":
        items = checked_out(inv, db)
        return _ok({"checked_out": items, "count": len(items)} if items else {"checked_out": [], "note": "Nothing is out."})
    if not str(ref).strip():
        return {"error": f"{action} needs \"item\": a name, nickname or 8-digit ID.", "exit_code": 1}
    if action == "find":
        found = search(inv, db, ref)
        return _ok({"query": ref, "count": len(found),
                    "items": [_brief(inv, db, i) for i in found[:MAX_RESULTS]],
                    **({"more": len(found) - MAX_RESULTS} if len(found) > MAX_RESULTS else {})})
    item, candidates = _resolve(inv, db, ref, prefer="away" if action == "return" else "home")
    if item is None:
        if not candidates:
            return _ok({"result": "not_found", "query": ref,
                        "note": "Nothing in the index matches. Try another word, or ask him for the ID."})
        if action == "return" and not any(c["status"] == "away" for c in candidates):
            return _ok({"result": "refused", "query": ref, "why": "None of the matching items is checked out.",
                        "matches": [_brief(inv, db, i, with_place=False) for i in candidates[:MAX_RESULTS]]})
        return _ok({"result": "which_one", "query": ref, "note": "Several items match; ask him which, then use its ID.",
                    "candidates": [_brief(inv, db, i, with_place=False) for i in candidates[:MAX_RESULTS]]})
    if action == "where":
        details = _brief(inv, db, item)
        kids = db.execute("SELECT id,name FROM items WHERE parent_item_id=? ORDER BY id LIMIT 20", (item["id"],)).fetchall()
        if kids:
            details["contains"] = [f"{k['name']} ({k['id']})" for k in kids]
        nick = inv.nicknames(db, item["id"])
        if nick:
            details["also_called"] = nick
        return _ok(details)
    try:
        if action == "checkout":
            destination = str(args.get("to") or args.get("destination") or "").strip()[:120]
            reason = str(args.get("reason") or "").strip()[:200]
            with inv.recorded(db, f"Check out {item['name']} (Mabel)", actor=ACTOR):
                count = inv.checkout(db, item["id"], destination or None, actor=ACTOR, reason=reason)
            result = "checked_out"
        else:
            with inv.recorded(db, f"Return {item['name']} (Mabel)", actor=ACTOR):
                count = inv.return_home(db, item["id"], actor=ACTOR, reason=str(args.get("reason") or "")[:200])
            result = "returned"
    except inv.InventoryError as exc:
        return _ok({"result": "refused", "item": _brief(inv, db, item, with_place=False), "why": str(exc)})
    after = inv.item_dict(inv.get_item(db, item["id"]))
    out = {"result": result, "item": _brief(inv, db, after), "items_affected": count,
           "undo": "He can undo this in the tk-index web UI."}
    if count > 1:
        out["note"] = f"{count - 1} thing(s) inside it went along."
    return _ok(out)


def _ok(payload: dict) -> dict:
    return {"output": json.dumps(payload, ensure_ascii=False), "exit_code": 0}


class GremlinTool:
    def __init__(self, *, path: Path | None = None):
        self.path = path

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if str(content or "").strip().startswith("{") else {"action": "find", "item": content}
        except ValueError:
            return {"error": "manage_gremlin expects JSON like {\"action\": \"where\", \"item\": \"soldering iron\"}",
                    "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "manage_gremlin expects a JSON object", "exit_code": 1}
        return await asyncio.to_thread(run, args, path=self.path)
