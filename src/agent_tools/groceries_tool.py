"""manage_groceries: Mabel's hands on the shared grocery list (src/initiative/groceries.py).

list = what's on it; add = put things on it ("we're out of oat milk"); bought = tick them off
("got the milk"); remove = take something off that isn't needed after all. Every change goes
through a full sync, so the Google Sheet his partner uses and his Obsidian note both get it.
Small, undoable list edits: asking for one is the approval (a self-approving family).
"""
from __future__ import annotations

import asyncio
import json

from src.initiative import groceries as g

ACTIONS = ("list", "add", "bought", "remove")
ALIASES = {"show": "list", "read": "list", "get": "list", "need": "add", "put": "add", "got": "bought",
           "buy": "bought", "check": "bought", "tick": "bought", "done": "bought", "delete": "remove",
           "drop": "remove", "unneeded": "remove"}


def _names(args: dict) -> list[str]:
    raw = args.get("items") or args.get("item") or args.get("name") or []
    if isinstance(raw, str):
        raw = [p for p in raw.replace(";", ",").split(",")]
    return [g._clean(x) for x in raw if g._clean(x)][:30]


def _brief(item: g.Item) -> dict:
    out = {"item": item.name}
    for key in ("amount", "note", "added_by"):
        if getattr(item, key):
            out[key] = getattr(item, key)
    if item.bought:
        out["bought"] = True
    return out


def run(args: dict, *, sync=g.sync) -> dict:
    action = str(args.get("action") or "list").strip().lower()
    action = ALIASES.get(action, action)
    if action not in ACTIONS:
        return {"error": f"Unknown action {action!r}. Use one of: {', '.join(ACTIONS)}.", "exit_code": 1}
    names = _names(args)
    if action != "list" and not names:
        return {"error": f"{action} needs \"items\": the things, e.g. [\"oat milk\", \"eggs\"].", "exit_code": 1}
    amount, note = g._clean(args.get("amount"))[:40], g._clean(args.get("note"))[:120]
    report = {"added": [], "already_there": [], "ticked": [], "removed": [], "not_on_list": []}

    def change(items: list[g.Item]) -> list[g.Item]:
        by_key = {i.key: i for i in items}
        for name in names:
            key, item = g.key_of(name), by_key.get(g.key_of(name))
            if action == "add":
                if item and not item.bought:
                    report["already_there"].append(item.name)
                    if amount and not item.amount:
                        item.amount = amount
                elif item:                                   # bought earlier: needed again
                    item.bought, item.bought_at = False, ""
                    report["added"].append(item.name)
                else:
                    new = g.Item(name=name[:1].upper() + name[1:], amount=amount if len(names) == 1 else "",
                                 note=note if len(names) == 1 else "", added_by="Mabel")
                    items.append(new)
                    by_key[key] = new
                    report["added"].append(new.name)
            elif item is None:
                report["not_on_list"].append(name)
            elif action == "bought":
                item.bought = True
                report["ticked"].append(item.name)
            else:
                items.remove(item)
                by_key.pop(key)
                report["removed"].append(item.name)
        return items

    try:
        result = sync(change=None if action == "list" else change)
    except Exception as exc:
        return {"error": f"The grocery list isn't reachable: {exc}", "exit_code": 1}
    items = result["items"]
    payload = {k: v for k, v in report.items() if v}
    payload["on_the_list"] = [_brief(i) for i in g.open_items(items)]
    bought = [i.name for i in items if i.bought]
    if bought:
        payload["bought_recently"] = bought
    if not payload["on_the_list"]:
        payload["note"] = "The list is empty."
    return {"output": json.dumps(payload, ensure_ascii=False), "exit_code": 0}


class GroceriesTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        text = str(content or "").strip()
        try:
            args = json.loads(text) if text.startswith("{") else {"action": "list"}
        except ValueError:
            return {"error": "manage_groceries expects JSON like {\"action\": \"add\", \"items\": [\"oat milk\"]}",
                    "exit_code": 1}
        if not isinstance(args, dict):
            return {"error": "manage_groceries expects a JSON object", "exit_code": 1}
        return await asyncio.to_thread(run, args)
