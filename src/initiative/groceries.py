"""The shared grocery list: a Google Sheet (his SO edits it too) mirrored with an Obsidian note,
plus a reminder from Mabel when he walks into a store.

Anthon asked for it 2026-11-17:
- **Sheet** (``groceries_sheet_id``): tab "List" with Item | Amount | Note | Added by | Bought | Bought at,
  Bought as checkboxes; tab "Bought" keeps history. Mabel reaches it through the Sheets API with the
  OAuth login of her rclone Drive remote (``groceries_google_remote``, default the letters one).
- **Note** (``groceries_note``): ``- [ ] Oat milk (2) — for the cake`` lines in his vault.
- **Sync**: three-way, against the list both sides last agreed on (``base`` in the state file), so an
  add, a tick or a deletion on either side carries over. When both sides changed the same field the
  sheet wins, except that "bought" wins over "not bought". Bought items stay (ticked) for a day so a
  mis-tick can be undone, then move to the Bought tab and leave the note.
- **Store reminder**: Home Assistant zones named like "Citytorget [01]" are stores of kind 01 (setting
  ``groceries_zone_code``). The PC is usually suspended while he's out, so a HA automation wakes it
  (Wake-on-LAN) when he enters such a zone; the heartbeat then sees his person entity in the zone,
  syncs, and Mabel texts him the list in her words (a plain list if her model can't answer in time),
  as a chat plus a phone notification. Once per visit, with a cooldown per zone.

State: data/persona/groceries.json. CLI: python -m src.initiative.groceries --sync | --list | --create
"""
from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

NOTE = "daily life of Anthon/Groceries.md"
LIST_TAB, BOUGHT_TAB = "List", "Bought"
HEADER = ["Item", "Amount", "Note", "Added by", "Bought", "Bought at"]
KEEP_BOUGHT = timedelta(days=1)
SYNC_EVERY = timedelta(minutes=5)
ZONE_COOLDOWN = timedelta(hours=2)
MESSAGE_LIMIT = 700
TOKENS = 3000
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

_lock = threading.Lock()


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── items ───────────────────────────────────────────────────────────────

@dataclass
class Item:
    name: str
    amount: str = ""
    note: str = ""
    added_by: str = ""
    bought: bool = False
    bought_at: str = ""          # ISO time, set when it was first seen ticked

    @property
    def key(self) -> str:
        return key_of(self.name)


def key_of(name: str) -> str:
    """Same thing, however it's typed: "Coca-Cola", "coca cola" and "Coca Cola " are one item."""
    return re.sub(r"[\W_]+", " ", str(name or "").casefold()).strip()


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# ── the note ────────────────────────────────────────────────────────────

NOTE_LINE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.+?)\s*$")
NOTE_TITLE = ("# Groceries\n\nShared with the Google Sheet \"Groceries\"; Mabel keeps the two in step. "
              "Add a line, tick what you've bought. Ticked things leave the list after a day.\n\n")


def parse_note(text: str) -> list[Item]:
    items = []
    for line in text.splitlines():
        m = NOTE_LINE.match(line)
        if not m:
            continue
        body, note = m.group(2), ""
        if " — " in body:
            body, note = body.split(" — ", 1)
        amount = ""
        tail = re.search(r"\s*\(([^()]*)\)\s*$", body)
        if tail:
            amount, body = tail.group(1), body[:tail.start()]
        name = _clean(body)
        if name:
            items.append(Item(name=name, amount=_clean(amount), note=_clean(note), bought=m.group(1) != " "))
    return items


def note_line(item: Item) -> str:
    text = item.name + (f" ({item.amount})" if item.amount else "") + (f" — {item.note}" if item.note else "")
    return f"- [{'x' if item.bought else ' '}] {text}"


def render_note(items: list[Item], previous: str = "") -> str:
    """Keeps whatever he wrote above the first checkbox; the checkbox lines are rewritten."""
    lines = previous.splitlines(keepends=True)
    first = next((i for i, l in enumerate(lines) if NOTE_LINE.match(l)), None)
    head = "".join(lines[:first]) if first is not None else (previous if previous.strip() else NOTE_TITLE)
    if head and not head.endswith("\n\n"):
        head = head.rstrip("\n") + "\n\n"
    return head + "\n".join(note_line(i) for i in items) + ("\n" if items else "")


def _vault() -> Path | None:
    try:
        from src.agent_runtime.vault_target import persisted_vault_path
        path = persisted_vault_path()
        return Path(path) if path and Path(path).is_dir() else None
    except Exception:
        return None


class NoteStore:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def default(cls) -> "NoteStore | None":
        vault = _vault()
        return cls(vault / str(_setting("groceries_note", NOTE) or NOTE)) if vault else None

    def read(self) -> tuple[list[Item], str]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return [], ""
        return parse_note(text), text

    def write(self, items: list[Item], previous: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".md.tmp")
        tmp.write_text(render_note(items, previous), encoding="utf-8")
        os.replace(tmp, self.path)


# ── the sheet ───────────────────────────────────────────────────────────

class GoogleError(RuntimeError):
    pass


def _rclone_conf() -> Path:
    binary = shutil.which("rclone")
    if binary:
        try:
            out = subprocess.run([binary, "config", "file"], capture_output=True, text=True, timeout=10).stdout
            path = out.strip().splitlines()[-1].strip()
            if path.endswith(".conf"):
                return Path(path)
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return Path("~/.config/rclone/rclone.conf").expanduser()


class GoogleAuth:
    """Access tokens from the refresh token of Mabel's rclone Drive remote (scope drive covers Sheets)."""

    def __init__(self, remote: str | None = None, post=None):
        self.remote = (remote or str(_setting("groceries_google_remote", "mabel-mail") or "mabel-mail")).rstrip(":")
        self._post = post
        self._token, self._expires = "", 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        cfg = configparser.ConfigParser()
        cfg.read(_rclone_conf())
        if self.remote not in cfg:
            raise GoogleError(f"rclone remote {self.remote!r} isn't configured")
        section = cfg[self.remote]
        try:
            refresh = json.loads(section.get("token", "{}"))["refresh_token"]
        except (ValueError, KeyError) as exc:
            raise GoogleError(f"rclone remote {self.remote!r} has no refresh token") from exc
        post = self._post
        if post is None:
            import httpx
            post = httpx.post
        resp = post("https://oauth2.googleapis.com/token", data={
            "client_id": section.get("client_id", ""), "client_secret": section.get("client_secret", ""),
            "refresh_token": refresh, "grant_type": "refresh_token"}, timeout=15)
        data = resp.json()
        if resp.status_code != 200 or not data.get("access_token"):
            raise GoogleError(f"Google refused the login: {data.get('error_description') or data.get('error')}")
        self._token, self._expires = data["access_token"], time.time() + float(data.get("expires_in", 3000))
        return self._token


def _bool(cell) -> bool:
    return str(cell).strip().upper() in ("TRUE", "YES", "X", "1", "✓", "JA")


class SheetStore:
    def __init__(self, sheet_id: str, auth: GoogleAuth | None = None, client=None):
        self.sheet_id = sheet_id
        self.auth = auth or GoogleAuth()
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import httpx
            self._client = httpx.Client(timeout=20)
        return self._client

    def _call(self, method: str, path: str, **kw) -> dict:
        resp = self.client.request(method, f"{SHEETS}{path}",
                                   headers={"Authorization": f"Bearer {self.auth.token()}"}, **kw)
        if resp.status_code >= 300:
            try:
                message = resp.json().get("error", {}).get("message", "")
            except ValueError:
                message = resp.text[:200]
            raise GoogleError(f"Sheets API {resp.status_code}: {message[:200]}")
        return resp.json() if resp.content else {}

    @classmethod
    def create(cls, title: str = "Groceries", auth: GoogleAuth | None = None, client=None) -> "SheetStore":
        store = cls("", auth, client)
        body = {"properties": {"title": title},
                "sheets": [{"properties": {"title": LIST_TAB, "gridProperties": {"frozenRowCount": 1}}},
                           {"properties": {"title": BOUGHT_TAB, "gridProperties": {"frozenRowCount": 1}}}]}
        made = store._call("POST", "", json=body)
        store.sheet_id = made["spreadsheetId"]
        list_tab = made["sheets"][0]["properties"]["sheetId"]
        store._call("POST", f"/{store.sheet_id}/values:batchUpdate", json={
            "valueInputOption": "RAW", "data": [
                {"range": f"{LIST_TAB}!A1:F1", "values": [HEADER]},
                {"range": f"{BOUGHT_TAB}!A1:F1", "values": [HEADER]}]})
        store._call("POST", f"/{store.sheet_id}:batchUpdate", json={"requests": [
            {"setDataValidation": {"range": {"sheetId": list_tab, "startRowIndex": 1, "startColumnIndex": 4,
                                             "endColumnIndex": 5},
                                   "rule": {"condition": {"type": "BOOLEAN"}}}},
            {"repeatCell": {"range": {"sheetId": list_tab, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                            "fields": "userEnteredFormat.textFormat.bold"}}]})
        return store

    def read(self) -> list[Item]:
        rows = self._call("GET", f"/{self.sheet_id}/values/{LIST_TAB}!A2:F").get("values", [])
        items = []
        for row in rows:
            row = list(row) + [""] * (6 - len(row))
            name = _clean(row[0])
            if name:
                items.append(Item(name=name, amount=_clean(row[1]), note=_clean(row[2]), added_by=_clean(row[3]),
                                  bought=_bool(row[4]), bought_at=_clean(row[5])))
        return items

    def write(self, items: list[Item], old_rows: int) -> None:
        values = [[i.name, i.amount, i.note, i.added_by, i.bought, i.bought_at] for i in items]
        values += [["", "", "", "", "", ""]] * max(0, old_rows - len(items))     # blank what's left over
        if values:
            self._call("PUT", f"/{self.sheet_id}/values/{LIST_TAB}!A2:F{len(values) + 1}",
                       params={"valueInputOption": "RAW"}, json={"values": values})

    def archive(self, items: list[Item]) -> None:
        if items:
            self._call("POST", f"/{self.sheet_id}/values/{BOUGHT_TAB}!A1:F:append",
                       params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                       json={"values": [[i.name, i.amount, i.note, i.added_by, True, i.bought_at] for i in items]})


# ── state ───────────────────────────────────────────────────────────────

def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "groceries.json"


def load_state(data_dir: Path) -> dict:
    try:
        data = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".groceries.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sheet_id(data_dir: Path) -> str:
    return str(_setting("groceries_sheet_id", "") or load_state(data_dir).get("sheet_id") or "").strip()


# ── the merge ───────────────────────────────────────────────────────────

FIELDS = ("amount", "note", "added_by", "bought")
NOTE_FIELDS = ("amount", "note", "bought")          # what a note line can say


def merge(sheet: list[Item], note: list[Item], base: dict[str, dict], now: datetime) -> tuple[list[Item], list[Item]]:
    """(the list both sides should show, items to move to the Bought tab)."""
    s = {i.key: i for i in sheet}
    n = {i.key: i for i in note}
    order = list(dict.fromkeys([*s, *n]))
    merged = []
    for key in order:
        si, ni, b = s.get(key), n.get(key), base.get(key)
        if b is None:                                    # new on one side (or both)
            item = Item(**asdict(si or ni))
            if si and ni:
                item.bought = si.bought or ni.bought
                for f in ("amount", "note"):
                    setattr(item, f, getattr(si, f) or getattr(ni, f))
            if ni and not si and not item.added_by:
                item.added_by = "Anthon"
        elif si is None or ni is None:                   # deleted on one side
            continue
        else:
            item = Item(**asdict(si))
            for f in FIELDS:
                sv, nv, bv = getattr(si, f), getattr(ni, f), b.get(f, "" if f != "bought" else False)
                if f not in NOTE_FIELDS:
                    nv = bv                              # the note doesn't carry it: no change from there
                if f == "bought" and sv != bv and nv != bv:
                    item.bought = sv or nv
                elif sv != bv:
                    setattr(item, f, sv)
                elif nv != bv:
                    setattr(item, f, nv)
        if item.bought and not item.bought_at:
            item.bought_at = now.isoformat(timespec="seconds")
        if not item.bought:
            item.bought_at = ""
        merged.append(item)
    keep, done = [], []
    for item in merged:
        try:
            old = item.bought and now - datetime.fromisoformat(item.bought_at) >= KEEP_BOUGHT
        except ValueError:
            old = False
        (done if old else keep).append(item)
    return keep, done


def sync(*, data_dir: Path | None = None, sheet: SheetStore | None = None, note: NoteStore | None = None,
         now: datetime | None = None, change=None) -> dict:
    """Bring sheet and note in step. ``change(items) -> items`` edits the merged list first (the chat tool)."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or _now()
    with _lock:
        state = load_state(data_dir)
        if sheet is None:
            sid = sheet_id(data_dir)
            sheet = SheetStore(sid) if sid else None
        note = note if note is not None else NoteStore.default()
        if sheet is None and note is None:
            raise GoogleError("no grocery sheet and no vault note configured")
        sheet_items = sheet.read() if sheet else []
        note_items, note_text = note.read() if note else ([], "")
        base = state.get("base") or {}
        if sheet is None:
            sheet_items = note_items                      # note only: nothing to merge against
        if note is None:
            note_items = sheet_items
        items, done = merge(sheet_items, note_items, base, now)
        if change is not None:
            items = change(items)
        if sheet:
            if [asdict(i) for i in items] != [asdict(i) for i in sheet_items]:
                sheet.write(items, len(sheet_items))
            sheet.archive(done)
        if note and ([note_line(i) for i in items] != [note_line(i) for i in note_items] or not note_text):
            note.write(items, note_text)
        state["base"] = {i.key: asdict(i) for i in items}
        state["last_sync"] = now.isoformat(timespec="seconds")
        state.pop("last_error", None)
        save_state(data_dir, state)
    return {"items": items, "archived": len(done)}


def open_items(items: list[Item]) -> list[Item]:
    return [i for i in items if not i.bought]


def maybe_sync(now: datetime | None = None, *, data_dir: Path | None = None) -> bool:
    """Heartbeat: sync every SYNC_EVERY when enabled and configured. Errors are kept in the state file."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or _now()
    if not _setting("groceries_enabled", True) or not sheet_id(data_dir):
        return False
    state = load_state(data_dir)
    last = state.get("last_attempt")
    if last and now - datetime.fromisoformat(last) < SYNC_EVERY:
        return False
    state["last_attempt"] = now.isoformat(timespec="seconds")
    save_state(data_dir, state)
    try:
        sync(data_dir=data_dir, now=now)
        return True
    except Exception as exc:
        logger.warning("[groceries] sync failed: %s", exc)
        state = load_state(data_dir)
        state["last_error"] = {"at": now.isoformat(timespec="seconds"), "error": str(exc)[:300]}
        save_state(data_dir, state)
        return False


# ── the store reminder ──────────────────────────────────────────────────

ZONE_CODE = re.compile(r"\[(\d{2})\]\s*$")


def zone_code(state: str) -> str | None:
    m = ZONE_CODE.search(state or "")
    return m.group(1) if m else None


def place_name(state: str) -> str:
    return ZONE_CODE.sub("", state or "").strip()


def person_state(get=None) -> str | None:
    """The raw state of his person entity ('home', 'not_home', or a zone's name), None if unknown."""
    try:
        from src.initiative import outreach
        ha = outreach.home_assistant()
        entity = str(_setting("heartbeat_presence_entity", "") or "").strip()
        if not ha or not re.fullmatch(r"(?:person|device_tracker)\.[a-z0-9_]+", entity):
            return None
        if get is None:
            import httpx
            get = httpx.get
        resp = get(f"{ha['base_url'].rstrip('/')}/api/states/{entity}",
                   headers={"Authorization": f"Bearer {ha['api_key']}"}, timeout=5)
        return str(resp.json().get("state", "")) if resp.status_code == 200 else None
    except Exception:
        return None


def entered_store(state: dict, current: str | None, now: datetime) -> str | None:
    """The zone he just walked into, if it's a grocery store and not one we already reminded him about."""
    if current is None:
        return None
    previous = state.get("person_state")
    state["person_state"] = current
    code = str(_setting("groceries_zone_code", "01") or "01")
    if current == previous or zone_code(current) != code:
        return None
    last = (state.get("reminded") or {}).get(current)
    if last and now - datetime.fromisoformat(last) < ZONE_COOLDOWN:
        return None
    return current


def plain_message(store: str, items: list[Item]) -> str:
    listing = ", ".join(i.name + (f" ({i.amount})" if i.amount else "") for i in items)
    return f"You're at {store}. On the list: {listing}."


async def write_message(store: str, items: list[Item], *, owner: str, ask=None) -> str:
    listing = "\n".join(f"- {i.name}" + (f" ({i.amount})" if i.amount else "") + (f": {i.note}" if i.note else "")
                        + (f" [added by {i.added_by}]" if i.added_by and i.added_by not in ("Anthon", "Mabel") else "")
                        for i in items)
    prompt = ("Anthon just walked into a grocery store, " + store + ". The shared grocery list (his partner adds to "
              "it too) says:\n" + listing + "\n\nText him a short reminder, in your own voice, so he doesn't leave "
              "without them. Name every item. It's read on a phone in a shop: short, no lecture. "
              "Reply with only the message text.")
    system = "You are Mabel."
    try:
        from src.persona import composer
        library = composer().modules.load()
        if library.core:
            system = library.core.body
    except Exception:
        pass
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if ask is None:
        from src import model_roles
        reply = await model_roles.call("default", messages, owner=owner, max_tokens=TOKENS, temperature=0.7,
                                       timeout=150)
    else:
        reply = await ask(messages, owner)
    message = str(reply or "").strip().strip('"').strip()
    if not message or len(message) > MESSAGE_LIMIT:
        return ""
    said = key_of(message)
    missing = [i.name for i in items if key_of(i.name) not in said]
    if missing:                                          # never drop something from the list
        message += "\n\nAlso: " + ", ".join(missing) + "."
    return message


async def remind(store_state: str, *, data_dir: Path, owner: str = "tovakai", now: datetime | None = None,
                 ask=None, send: bool = True) -> dict:
    """He's in a store: fresh list, her message (or a plain one), chat + phone notification."""
    import asyncio
    now = now or _now()
    try:
        result = await asyncio.to_thread(sync, data_dir=data_dir, now=now)
        items = open_items(result["items"])
    except Exception as exc:
        logger.warning("[groceries] sync before the reminder failed, using the last list: %s", exc)
        items = open_items([Item(**v) for v in (load_state(data_dir).get("base") or {}).values()])
    store = place_name(store_state)
    state = load_state(data_dir)
    state.setdefault("reminded", {})[store_state] = now.isoformat(timespec="seconds")
    if not items:
        save_state(data_dir, state)
        return {"status": "nothing_on_the_list", "store": store}
    try:
        message = await write_message(store, items, owner=owner, ask=ask)
    except Exception as exc:
        logger.warning("[groceries] her message failed, sending the plain list: %s", exc)
        message = ""
    message = message or plain_message(store, items)
    record = {"at": now.isoformat(timespec="seconds"), "store": store, "items": [i.name for i in items],
              "message": message}
    if send:
        from src.initiative import outreach
        session_id = outreach.create_chat(owner, f"🛒 {store}", message, {"groceries_store": store_state})
        push = message if len(message) <= outreach.PUSH_LIMIT else \
            message[:outreach.PUSH_LIMIT].rsplit(" ", 1)[0] + "…"
        delivered = outreach.send_notification(push, outreach.chat_link(session_id), f"groceries-{store}")
        record.update(session_id=session_id, status="sent" if delivered else "in_app_only")
    else:
        record["status"] = "preview"
    state["history"] = (state.get("history") or [])[-60:] + [record]
    save_state(data_dir, state)
    logger.info("[groceries] reminded him at %s: %d item(s), %s", store, len(items), record["status"])
    return record


def observe(now: datetime | None = None, *, data_dir: Path | None = None, current: str | None = None,
            read=person_state) -> str | None:
    """Heartbeat, every tick: the store zone he just entered (to remind about), else None. Cheap."""
    from src.constants import DATA_DIR
    if not _setting("groceries_enabled", True):
        return None
    data_dir = Path(data_dir or DATA_DIR)
    now = now or _now()
    current = current if current is not None else read()
    state = load_state(data_dir)
    before = state.get("person_state")
    store = entered_store(state, current, now)
    if current is not None and current != before:
        save_state(data_dir, state)
    return store


def main() -> None:
    import argparse
    from src.constants import DATA_DIR
    cli = argparse.ArgumentParser(prog="python -m src.initiative.groceries")
    cli.add_argument("--create", action="store_true", help="make the Google Sheet (once) and remember its id")
    cli.add_argument("--sync", action="store_true", help="sync sheet and note now")
    cli.add_argument("--list", action="store_true", help="show the list")
    cli.add_argument("--preview-reminder", metavar="ZONE", help='what she would text in e.g. "Citytorget [01]"')
    args = cli.parse_args()
    data_dir = Path(DATA_DIR)
    if args.create:
        if sheet_id(data_dir):
            print("already have a sheet:", sheet_id(data_dir))
            return
        store = SheetStore.create()
        state = load_state(data_dir)
        state["sheet_id"] = store.sheet_id
        save_state(data_dir, state)
        print("created:", f"https://docs.google.com/spreadsheets/d/{store.sheet_id}")
    if args.sync or args.list:
        result = sync(data_dir=data_dir)
        for item in result["items"]:
            print(note_line(item))
    if args.preview_reminder:
        import asyncio
        print(json.dumps(asyncio.run(remind(args.preview_reminder, data_dir=data_dir, send=False)),
                         indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
