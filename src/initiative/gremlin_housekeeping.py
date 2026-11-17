"""Housekeeping rounds on the Gremlin Index: one storage place a day, suggestions only.

Anthon asked (2026-11-17) for a scheduled pass over his inventory that notes
missing or wrong spec IDs, inconsistent naming, things that still need sorting
and items with missing specs (a cable without a length), and helps him
standardise. He applies the changes himself in the tk-index web UI; nothing here
writes to the database (it is opened read-only).

A round:
1. picks the place reviewed longest ago (never-reviewed first). A place is one
   layout (the first 10 address characters, e.g. GRILHE0012); big places are
   split into parts of ``PART_SIZE`` items so she can be careful with each;
2. runs the fixed checks below over its items (``checks``);
3. gives her the items, the findings and the spec IDs already in use, and asks
   for concrete suggestions (``ID | change | why``); proposed spec IDs must pass
   tk-index's own format and family rules or they are dropped;
4. adds a checkbox section to the vault note ``HOUSEKEEPING_NOTE`` (newest on
   top, separate from his To do.md) and queues a slip for the desk printer.

Fixed findings are always listed; her lines add the judgement calls (a proposed
spec ID, a clearer name, which category). Gates match her other idle jobs: once a
day (06:00 -> 06:00), him away 20 min, no chat or research, GPU free, model up.

State: data/persona/gremlin_housekeeping.json.
CLI: python -m src.initiative.gremlin_housekeeping --preview [--place GRILHE0012] | --now | --places
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
HOUSEKEEPING_NOTE = "daily life of Anthon/Gremlin housekeeping.md"
PRINT_DIR = ("journal", "gremlin")
DAY_START_HOUR = 6
MIN_IDLE_MINUTES = 20
PART_SIZE = 30            # items per round; a bigger place is done in parts
SMALL_PLACE = 10          # places smaller than this share a round with others in their area
MAX_SUGGESTIONS = 20
TOKENS = 10000            # her private thinking counts against this too
UNPLACED = "UNPLACED"

_running = False

# Category words → spec family, for telling her which existing codes to look at.
CATEGORY_FAMILIES = {
    "cables & power": ("CAB", "WIR", "BAT"), "tools": ("TOL",), "electronics": ("ELE", "DEV"),
    "electronic components": ("ELE",), "fasteners & hardware": ("FAS",), "materials": ("MAT",),
    "paints & minis": ("PNT", "MIN"), "3d printing / making": ("MAT", "TOL", "GFST", "GFC"),
    "storage / gridfinity": ("GFST", "GFC"), "project parts": ("ELE", "KIT"),
}
CABLE_WORDS = re.compile(r"\b(cable|cord|wire|jumper|extension|spool)s?\b", re.I)
# Things named after cables that aren't one (a bin for cables, a tie, sleeving, a meter with leads).
NOT_A_CABLE = re.compile(r"\b(bin|caddy|holder|organi[sz]er|stand|rack|box|tray|tie|ties|sleeving|tester|meter|multimeter|stripper|cutter|crimp\w*|clips?|labels?|adapter)\b", re.I)
LENGTH = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:mm|cm|m)\b|\b\d+(?:[.,]\d+)?\s*(?:ft|in)\b", re.I)
UNKNOWN = re.compile(r"\b([\w/ -]{2,30}?)\s+(?:unknown|unverified|unmeasured|not specified|unspecified)\b", re.I)
SIZE_IN_NAME = re.compile(r"\b(\d{1,2})\s*[x×]\s*(\d{1,2})\b", re.I)
GFST = re.compile(r"^GFST(\d\d)(\d\d)$")


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


def running() -> bool:
    return _running


def day_of(now: datetime) -> str:
    return (now.astimezone() - timedelta(hours=DAY_START_HOUR)).strftime("%Y-%m-%d")


# ── state ───────────────────────────────────────────────────────────────

def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "gremlin_housekeeping.json"


def load_state(data_dir: Path) -> dict:
    try:
        data = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gremlin_housekeeping.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ── reading the index (read-only) ───────────────────────────────────────

def _specs_module():
    """tk_index.specs (format + families), imported the way manage_gremlin imports tk_index.db."""
    from src.agent_tools import gremlin_tool
    gremlin_tool.load_inventory(gremlin_tool.db_path())
    import importlib
    return importlib.import_module("tk_index.specs")


def read_index(path: Path | None = None) -> tuple[list[dict], dict[str, str]]:
    """All live items and the layout labels, from a read-only connection."""
    from src.agent_tools import gremlin_tool
    path = path or gremlin_tool.db_path()
    if not Path(path).exists():
        raise FileNotFoundError(f"The Gremlin Index database isn't at {path} (setting gremlin_index_path).")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        items = [dict(r) for r in db.execute(
            "SELECT id, name, spec_code, category, quantity, quantity_note, stage, specs, notes, address,"
            " parent_item_id, span_columns, span_rows FROM items WHERE stage IS NOT 'Retired' ORDER BY id")]
        labels = {r["key"]: r["label"] for r in db.execute("SELECT key, label FROM layouts")}
    finally:
        db.close()
    return items, labels


def place_of(item: dict) -> str:
    return (item.get("address") or "")[:10] or UNPLACED


def places(items: list[dict]) -> dict[str, list[dict]]:
    """Round keys → items. A place with more than PART_SIZE items becomes KEY#1, KEY#2, …"""
    by_place: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_place[place_of(item)].append(item)
    # Small places in the same area (STDGAR0001..0004: the garage) make one round together.
    small: dict[str, list[str]] = defaultdict(list)
    for key, group in by_place.items():
        if key != UNPLACED and len(group) < SMALL_PLACE:
            small[key[3:6]].append(key)
    for area, keys in small.items():
        if len(keys) > 1 and sum(len(by_place[k]) for k in keys) <= PART_SIZE:
            by_place[f"AREA{area}"] = [i for k in sorted(keys) for i in by_place.pop(k)]
    # What's still tiny (a lone item in the hallway, the kitchen…) goes into one round.
    tiny = [k for k, g in by_place.items() if k != UNPLACED and not k.startswith("AREA") and len(g) < 3]
    if len(tiny) > 1:
        by_place["AREAMISC"] = [i for k in sorted(tiny) for i in by_place.pop(k)]
    rounds = {}
    for key, group in sorted(by_place.items()):
        if len(group) <= PART_SIZE:
            rounds[key] = group
            continue
        for n, start in enumerate(range(0, len(group), PART_SIZE), 1):
            rounds[f"{key}#{n}"] = group[start:start + PART_SIZE]
    return rounds


def label_of(round_key: str, labels: dict[str, str], group: list[dict] = ()) -> str:
    key, _, part = round_key.partition("#")
    if key == UNPLACED:
        name = "Items without an address"
    elif key.startswith("AREA"):
        names = sorted({labels.get(place_of(i)) or place_of(i) for i in group})
        name = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
    else:
        name = labels.get(key) or key
    return f"{name} (part {part})" if part else name


def next_place(rounds: dict[str, list[dict]], state: dict) -> str | None:
    """Never-reviewed first (in address order), then the one reviewed longest ago."""
    reviewed = state.get("reviewed") or {}
    candidates = [k for k, v in rounds.items() if v]
    if not candidates:
        return None
    return min(candidates, key=lambda k: (reviewed.get(k) or "", k))


# ── the fixed checks ────────────────────────────────────────────────────

def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def main_categories(items: list[dict], min_items: int = 5) -> set[str]:
    counts = Counter(i.get("category") or "" for i in items)
    return {c for c, n in counts.items() if c and n >= min_items}


def tidy_name(name: str, separator: str | None = None) -> str:
    """House style: ' - ' between parts (his choice 2026-11-17; the older names use ' · '), '×' in sizes
    (tk-index's footprint inference reads '1×3', not '1x3'), single spaces, a capital first letter."""
    sep = separator if separator is not None else str(_setting("gremlin_name_separator", " - ") or " - ")
    fixed = SIZE_IN_NAME.sub(lambda m: f"{m[1]}×{m[2]}", name)
    fixed = re.sub(r"\s+[-–—|·]\s+", sep, fixed)
    fixed = re.sub(r"\s{2,}", " ", fixed).strip()
    first = fixed.split(" ", 1)[0]
    if first.isalpha() and first.islower():          # not "nRF52840", "iPad", "eBay"
        fixed = fixed[:1].upper() + fixed[1:]
    return fixed


def checks(group: list[dict], everything: list[dict], specs=None) -> list[dict]:
    """Findings for one round. kind 'fix' = always listed (with a concrete change); 'hint' = for her to judge."""
    specs = specs or _specs_module()
    main = main_categories(everything)
    spec_by_name = defaultdict(set)
    for item in everything:
        if item.get("spec_code"):
            spec_by_name[_norm_name(item["name"])].add(item["spec_code"])
    found = []

    def add(item, kind, what, change="", why=""):
        found.append({"id": item["id"], "name": item["name"], "kind": kind, "what": what,
                      "change": change, "why": why})

    for item in group:
        name, spec = item.get("name") or "", (item.get("spec_code") or "").strip()
        detail = f"{item.get('specs') or ''} {item.get('notes') or ''}"
        # spec IDs
        if spec == specs.PLACEHOLDER:
            add(item, "fix", "placeholder spec ID", "clear the spec ID (00000000 means none)")
        elif spec and not (specs.SPEC_PATTERN.fullmatch(spec) and specs.family_of(spec)):
            add(item, "fix", f"spec ID {spec} doesn't fit the format",
                f"spec ID: {specs.RENAMES[spec]}" if spec in getattr(specs, "RENAMES", {}) else "",
                "8 letters/digits starting with a family (GFST, CAB, TOL, …)")
        elif spec and (m := GFST.match(spec)):
            size = SIZE_IN_NAME.search(name)
            if size:
                named = {(int(size[1]), int(size[2])), (int(size[2]), int(size[1]))}
                if (int(m[1]), int(m[2])) not in named:
                    add(item, "fix", f"spec {spec} says {int(m[1])}×{int(m[2])}, the name says {size[1]}×{size[2]}",
                        f"spec ID: GFST{int(size[1]):02d}{int(size[2]):02d}", "check which one is right")
        elif not spec:
            add(item, "hint", "no spec ID")
        if spec and len(spec_by_name[_norm_name(name)] - {spec}) and _norm_name(name):
            others = ", ".join(sorted(spec_by_name[_norm_name(name)] - {spec}))
            add(item, "fix", f"same name as items with spec {others}", "", "identical things share one spec ID")
        # naming
        tidy = tidy_name(name)
        if tidy != name:
            why = []
            if SIZE_IN_NAME.sub(lambda m: f"{m[1]}×{m[2]}", name) != name:
                why.append("'×' in sizes, so the index can read the footprint")
            if re.search(r"\s[–—|·]\s", name):
                why.append("' - ' between parts")
            add(item, "fix", "name style", f"name: {tidy}", ", ".join(why) or "spacing and capitals")
        # sorting
        if re.search(r"\bunsort", f"{name} {detail}", re.I):
            add(item, "fix", "still unsorted", "sort it, then split or rename the item", "'unsorted' in name or notes")
        # categories
        category = item.get("category") or ""
        if not category:
            add(item, "hint", "no category")
        elif category not in main:
            head = category.split(" / ")[0]
            if head in main:
                add(item, "fix", f"one-off category '{category}'", f"category: {head}",
                    "keeps the category list short")
            else:
                add(item, "hint", f"one-off category '{category}'")
        # missing specs
        if (spec[:3] in ("CAB", "WIR") or (category == "Cables & power" and CABLE_WORDS.search(name))) \
                and not NOT_A_CABLE.search(name) and not LENGTH.search(f"{name} {item.get('specs') or ''}"):
            add(item, "hint", "cable/wire with no length")
        for m in UNKNOWN.finditer(item.get("specs") or ""):
            add(item, "hint", f"specs say '{m.group(0).strip()}'")
        if item.get("quantity") is None and not (item.get("quantity_note") or "").strip():
            add(item, "hint", "quantity unknown")
        if item.get("stage") in ("Needs ID", "Review"):
            add(item, "hint", f"stage is '{item['stage']}'")
    return found


def codes_in_use(everything: list[dict], families: set[str], specs=None, limit: int = 60) -> list[str]:
    """'SPEC: example name' for codes already used in these families, so she reuses instead of inventing."""
    specs = specs or _specs_module()
    seen = {}
    for item in everything:
        spec = item.get("spec_code") or ""
        fam = specs.family_of(spec)
        if fam and fam in families and spec not in seen:
            seen[spec] = item["name"]
    return [f"{k}: {v}" for k, v in sorted(seen.items())][:limit]


# ── her pass ────────────────────────────────────────────────────────────

def _identity() -> str:
    try:
        from src.persona import composer
        library = composer().modules.load()
        return (library.core.body if library.core else "") or "You are Mabel."
    except Exception:
        return "You are Mabel."


def _item_line(item: dict) -> str:
    bits = [f"{item['id']} | {item['name']}", f"spec: {item.get('spec_code') or '-'}",
            f"category: {item.get('category') or '-'}"]
    qty = item.get("quantity")
    bits.append(f"qty: {qty:g}" if isinstance(qty, (int, float)) else f"qty: {item.get('quantity_note') or '?'}")
    if item.get("specs"):
        bits.append(f"specs: {item['specs'][:160]}")
    if item.get("notes"):
        bits.append(f"notes: {item['notes'][:160]}")
    return " ; ".join(bits)


def prompt(label: str, group: list[dict], found: list[dict], families_text: str, in_use: list[str],
           categories: list[str] = ()) -> str:
    no_spec = [f["id"] for f in found if f["what"] == "no spec ID"]
    findings = "\n".join(f"- {f['id']} {f['what']}" + (f" -> {f['change']}" if f["change"] else "")
                         for f in found if f["what"] != "no spec ID")
    if no_spec:
        findings += f"\n- no spec ID yet ({len(no_spec)} of {len(group)}): " + ", ".join(no_spec)
    findings = findings.strip("\n") or "- (nothing)"
    return (
        "You're doing a housekeeping round on Anthon's Gremlin Index (his workshop inventory), one place at a "
        f"time. Today: {label}. He asked for this: find missing or wrong spec IDs, inconsistent names, things "
        "that need sorting, and items missing specs (a cable without a length, a spool without a gauge), and "
        "help him standardise. He makes the changes himself in the index's web UI, so each suggestion must be "
        "concrete enough to just type in.\n\n"
        "Spec IDs: exactly 8 characters, A-Z and 0-9, starting with a family. Identical things share one spec "
        "ID. Families:\n" + families_text + "\n\n"
        "Categories: use only these (moving an item into one of them is fine; don't invent new ones):\n"
        + (", ".join(categories) or "(any)") + "\n\n"
        "Spec IDs already in use nearby (reuse one when the item is the same kind of thing):\n"
        + ("\n".join(in_use) or "(none yet)") + "\n\n"
        "The items here:\n" + "\n".join(_item_line(i) for i in group) + "\n\n"
        "What the fixed checks found (these are listed for him anyway; add to them, don't repeat them):\n"
        + findings + "\n\n"
        f"Write at most {MAX_SUGGESTIONS} suggestions, most useful first, one per line, exactly:\n"
        "ID | change | why\n"
        "where change is what to type, e.g. 'spec ID: CABAC100', 'name: USB-C to USB-A cable - 1 m', "
        "'category: Tools', 'specs: add the length', 'split into: …', and why is at most 12 words. "
        "When several items need the same thing (a set of spools that only differ in colour), write ONE line "
        "with all their IDs, comma-separated, in the ID field.\n"
        "A spec ID names the kind of thing by what matters when you need another one (material, gauge, size, "
        "connectors, length), not its colour. If that property isn't known yet, suggest measuring or checking "
        "it instead of inventing a provisional code. Only suggest what the item data supports. "
        "Skip items that are fine. If nothing needs doing, reply NONE. No other text.")


def parse(reply: str, ids: set[str], specs=None, categories: set[str] | None = None) -> list[dict]:
    specs = specs or _specs_module()
    allowed = {c.casefold(): c for c in (categories or ())}
    out = []
    for line in str(reply or "").splitlines():
        parts = [p.strip(" `*-") for p in line.split("|")]
        if len(parts) < 2 or not parts[1]:
            continue
        line_ids = [i for i in dict.fromkeys(re.findall(r"\b\d{8}\b", parts[0])) if i in ids]
        if not line_ids or re.sub(r"[\d,;\s&and]+", "", parts[0]):
            continue                                  # unknown IDs only, or not an ID field at all
        change = parts[1][:160]
        field, _, value = change.partition(":")
        if _ and not value.strip():
            continue                                 # "spec ID:" with nothing after it
        if allowed and field.strip().casefold() == "category":
            if value.strip().casefold() not in allowed:
                continue                             # a new category would grow the list he wants kept short
            change = f"category: {allowed[value.strip().casefold()]}"
        proposed = re.match(r"spec(?:\s*ID)?\s*:\s*([A-Za-z0-9 -]{4,12})\s*$", change, re.I)
        if proposed:
            try:
                normal = specs.normalize_spec(proposed.group(1))
            except Exception:
                continue                     # not a valid spec ID: drop rather than print a wrong code
            if not normal:
                continue
            change = f"spec ID: {normal}"
        out.append({"id": line_ids[0], "ids": line_ids, "change": change,
                    "why": (parts[2] if len(parts) > 2 else "")[:120]})
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


async def _ask(messages: list[dict], owner: str) -> str:
    from src import model_roles
    return await model_roles.call("default", messages, owner=owner, max_tokens=TOKENS, temperature=0.3,
                                  timeout=600)


def merge(found: list[dict], hers: list[dict], names: dict[str, str]) -> list[dict]:
    """The to-do lines: fixed findings with a change, then hers (minus repeats of the same change)."""
    lines, seen = [], set()
    for f in found:
        if f["kind"] != "fix":
            continue
        change = f["change"] or f["what"]
        key = (f["id"], change.lower())
        if key not in seen:
            seen.add(key)
            lines.append({"id": f["id"], "name": f["name"], "change": change, "style": f["what"] == "name style",
                          "why": f["why"] or (f["what"] if f["change"] else "")})
    for h in hers:
        key = (h["id"], h["change"].lower())
        if key not in seen:
            seen.add(key)
            name = names.get(h["id"], "")
            if len(h.get("ids") or ()) > 1:
                name += f" + {len(h['ids']) - 1} more"
            lines.append({"name": name, "style": False, **h})
    # Plain renames to the house style are many and easy: after the ones that need thought.
    return [l for l in lines if not l["style"]] + [l for l in lines if l["style"]]


# ── outputs ─────────────────────────────────────────────────────────────

def _vault() -> Path | None:
    try:
        from src.agent_runtime.vault_target import persisted_vault_path
        path = persisted_vault_path()
        return Path(path) if path and Path(path).is_dir() else None
    except Exception:
        return None


def note_section(day: str, label: str, round_key: str, lines: list[dict]) -> str:
    head = f"## {day} · {label} ({round_key})\n"
    if not lines:
        return head + "\nAll tidy here, nothing to change.\n"
    def row(line):
        why = f" _({line['why']})_" if line.get("why") else ""
        ids = ", ".join(f"`{i}`" for i in (line.get("ids") or [line["id"]]))
        return f"- [ ] **{line['name']}** {ids}: {line['change']}{why}"
    main = [row(l) for l in lines if not l.get("style")]
    style = [row(l) for l in lines if l.get("style")]
    body = "\n".join(main)
    if style:
        body += ("\n\n" if main else "") + "Names in the house style:\n\n" + "\n".join(style)
    return head + "\n" + body + "\n"


def add_to_note(vault: Path, section: str) -> Path:
    """Newest round on top, under the title. Rounds he has ticked off stay until he deletes them."""
    path = vault / str(_setting("gremlin_housekeeping_note", HOUSEKEEPING_NOTE))
    path.parent.mkdir(parents=True, exist_ok=True)
    title = ("# Gremlin housekeeping\n\nMabel's suggestions from her rounds through the Gremlin Index, one place "
             "at a time. Make the changes in the index's web UI, then tick them off.\n")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = title
    first = re.search(r"^## ", text, re.M)
    text = (text[:first.start()] + section + "\n" + text[first.start():]) if first \
        else text.rstrip("\n") + "\n\n" + section
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def print_text(label: str, lines: list[dict]) -> str:
    out = ["GREMLIN HOUSEKEEPING", label, ""]
    for line in lines:
        if line.get("style"):
            continue
        ids = line.get("ids") or [line["id"]]
        name = line["name"].split(" + ")[0] if len(ids) > 1 else line["name"]
        out.append(f"[ ] {ids[0]} {name[:32]}")
        if len(ids) > 1:
            more = f" +{len(ids) - 3} more" if len(ids) > 3 else ""
            out.append("    and " + " ".join(ids[1:3]) + more)
        out.append(f"    -> {line['change']}")
    renames = sum(1 for l in lines if l.get("style"))
    if renames:
        out.append(f"[ ] {renames} name(s) to the house style")
    out += ["", f"{len(lines)} to do. Full list in", "Obsidian: Gremlin housekeeping"]
    return "\n".join(out)


def write_print(now: datetime, round_key: str, label: str, lines: list[dict], mabel_vault: Path = _REPO) -> str:
    import yaml
    folder = mabel_vault.joinpath(*PRINT_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone().strftime("%Y-%m-%d")
    name = f"{stamp}-{re.sub(r'[^A-Za-z0-9]+', '-', round_key).strip('-')}.md"
    meta = {"type": "gremlin_housekeeping", "date": stamp, "written": now.isoformat(timespec="seconds"),
            "place": round_key, "printed_text": print_text(label, lines)}
    body = "\n".join(f"- {l['id']} {l['name']}: {l['change']}" for l in lines)
    (folder / name).write_text("---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n"
                               + f"# Housekeeping: {label}\n\n{body}\n", encoding="utf-8")
    return "/".join(PRINT_DIR + (name,))


# ── the round ───────────────────────────────────────────────────────────

def gate(sig, *, data_dir: Path, now: datetime, research_running: bool) -> list[str]:
    """Reasons not to do a round right now (empty = go). Doesn't open the database."""
    reasons = []
    if not _setting("gremlin_housekeeping_enabled", True):
        reasons.append("switched off (gremlin_housekeeping_enabled)")
    if _running:
        reasons.append("already on a round")
    if load_state(data_dir).get("last_day") == day_of(now):
        reasons.append("did a round today")
    if research_running:
        reasons.append("a research session is running")
    if sig.chat_active is not False:
        reasons.append("a chat is running (or unknown)")
    if sig.idle != "idle" or (sig.idle_minutes or 0) < MIN_IDLE_MINUTES:
        reasons.append(f"Anthon hasn't been away {MIN_IDLE_MINUTES} min")
    if sig.gpu != "free":
        reasons.append(f"GPU not free ({sig.gpu_reason or sig.gpu})")
    if not sig.strata:
        reasons.append("her model isn't up")
    return reasons


async def run_round(*, data_dir: Path, owner: str = "tovakai", now: datetime | None = None, place: str | None = None,
                    ask=None, send: bool = True, vault: Path | None = None, index_path: Path | None = None,
                    mabel_vault: Path = _REPO) -> dict:
    """One place: checks, her suggestions, the note and the print. send=False previews without writing."""
    global _running
    now = now or datetime.now(timezone.utc)
    _running = True
    try:
        items, labels = read_index(index_path)
        specs = _specs_module()
        rounds = places(items)
        state = load_state(data_dir)
        key = place or next_place(rounds, state)
        if not key or key not in rounds:
            return {"status": "no_place", "place": key}
        group = rounds[key]
        label = label_of(key, labels, group)
        found = checks(group, items, specs)
        families = set()
        for item in group:
            families.update(CATEGORY_FAMILIES.get((item.get("category") or "").lower(), ()))
            if specs.family_of(item.get("spec_code") or ""):
                families.add(specs.family_of(item["spec_code"]))
        families_text = "\n".join(f"{k}: {v}" for k, v in specs.FAMILIES.items())
        in_use = codes_in_use(items, families or set(specs.FAMILIES), specs)
        messages = [{"role": "system", "content": _identity()},
                    {"role": "user", "content": prompt(label, group, found, families_text, in_use,
                                                       sorted(main_categories(items)))}]
        try:
            reply = await (ask or _ask)(messages, owner)
            hers = [] if str(reply or "").strip().upper() == "NONE" else parse(reply, {i["id"] for i in group}, specs,
                                                                                     main_categories(items))
        except Exception as exc:
            logger.warning("[gremlin-housekeeping] her pass failed, listing the fixed checks only: %s", exc)
            hers = []
        lines = merge(found, hers, {i["id"]: i["name"] for i in group})
        result = {"status": "preview", "place": key, "label": label, "items": len(group), "lines": lines,
                  "hints": sum(f["kind"] == "hint" for f in found), "hers": len(hers)}
        if not send:
            return result
        vault = vault or _vault()
        note = add_to_note(vault, note_section(day_of(now), label, key, lines)) if vault else None
        printed = None
        if lines:
            from src.persona import printer
            ref = write_print(now, key, label, lines, mabel_vault)
            printed = bool(printer.enqueue("gremlin", ref, title=f"housekeeping {key}", data_dir=data_dir, now=now))
        state.setdefault("reviewed", {})[key] = now.isoformat(timespec="seconds")
        state["last_day"] = day_of(now)
        record = {"at": now.isoformat(timespec="seconds"), "place": key, "items": len(group),
                  "suggestions": len(lines), "note": str(note) if note else None, "printed": printed}
        state["history"] = (state.get("history") or [])[-120:] + [record]
        save_state(data_dir, state)
        logger.info("[gremlin-housekeeping] %s: %d item(s), %d suggestion(s)", key, len(group), len(lines))
        return {**result, "status": "done", "note": record["note"], "printed": printed}
    finally:
        _running = False


async def maybe_run(sig, *, owner: str = "tovakai", data_dir: Path | None = None, now: datetime | None = None,
                    research_running: bool = False) -> dict | None:
    """Called from the heartbeat when she'd otherwise be idle."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    if gate(sig, data_dir=data_dir, now=now, research_running=research_running):
        return None
    return await run_round(data_dir=data_dir, owner=owner, now=now)


def main() -> None:
    import argparse
    import asyncio
    from src.constants import DATA_DIR
    cli = argparse.ArgumentParser(prog="python -m src.initiative.gremlin_housekeeping")
    cli.add_argument("--places", action="store_true", help="list the rounds and when each was last reviewed")
    cli.add_argument("--preview", action="store_true", help="do a round without writing, printing or saving")
    cli.add_argument("--checks", action="store_true", help="only the fixed checks (no model)")
    cli.add_argument("--now", action="store_true", help="do a round now (ignores the gate)")
    cli.add_argument("--place", help="round key, e.g. GRILHE0012 or STDLST0001#2")
    args = cli.parse_args()
    data_dir = Path(DATA_DIR)
    if args.places or args.checks:
        items, labels = read_index()
        rounds = places(items)
        if args.places:
            reviewed = load_state(data_dir).get("reviewed") or {}
            for key, group in rounds.items():
                print(f"{key:16} {len(group):3}  {reviewed.get(key, 'never'):25} {label_of(key, labels, group)}")
            return
        key = args.place or next_place(rounds, load_state(data_dir))
        for f in checks(rounds[key], items):
            print(f"{f['kind']:4} {f['id']} {f['name'][:40]:40} {f['what']}" + (f" -> {f['change']}" if f["change"] else ""))
        return
    result = asyncio.run(run_round(data_dir=data_dir, place=args.place, send=bool(args.now)))
    print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
