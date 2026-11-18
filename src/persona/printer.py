"""The little thermal printer on Anthon's desk: her dreams and notes, on paper.

A 58 mm ESC/POS receipt printer (GEZHI MP-58C, Bluetooth name RPP02N): 384 dots
wide, 32 columns of plain text. Printing goes over USB (``/dev/thermal-printer``,
a udev symlink) when it's plugged in, otherwise over Bluetooth serial (RFCOMM
channel 1, no pairing needed). USB alone can't power the print head, so the
printer runs on its own supply.

Nothing is printed directly: callers ``enqueue()`` a journal note, and the
heartbeat calls ``flush()`` every tick. A job waits while the printer is off or
out of reach and is retried, so a dream from 06:00 still comes out when the
printer is switched on later; after ``MAX_AGE`` it's dropped (the note stays in
her journal). Rendering happens at print time, from the note on disk.

State: ``data/persona/printer.json``. CLI: ``python -m src.persona.printer
--test | --dream PATH | --paper PATH | --flush | --status``.
"""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import textwrap
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
DEFAULT_BT = "86:67:7A:53:65:6F"
DEFAULT_USB = "/dev/thermal-printer"
COLS = 32
DOTS = 384
CHUNK = 512
RETRY_AFTER = timedelta(minutes=2)    # it turns itself off; once he presses its button, print soon after
KEEPALIVE_MINUTES = 20                 # its own auto-off is "closer to an hour"; any contact resets it
STATUS = b"\x10\x04\x01"              # DLE EOT 1: real-time status request, prints nothing
MAX_AGE = timedelta(days=3)
MAX_DONE = 100
KINDS = ("dream", "paper", "morning", "chat", "gremlin")    # chat: he asked her to print; gremlin: housekeeping slip

INIT, CENTER, LEFT = b"\x1b@", b"\x1ba\x01", b"\x1ba\x00"
BOLD, UNBOLD, TALL, NORMAL = b"\x1bE\x01", b"\x1bE\x00", b"\x1d!\x01", b"\x1d!\x00"
FEED = b"\n\n\n\n"

_ASCII = {"’": "'", "‘": "'", "“": '"', "”": '"', "—": "--", "–": "-",
          "…": "...", " ": " ", "•": "*", "·": "*", "→": "->", "←": "<-",
          "é": "e", "è": "e", "à": "a", "ö": "o", "ü": "u", "ä": "a", "ø": "o",
          "å": "a", "æ": "ae"}

_wire = threading.Lock()      # one job on the printer at a time
_lock = threading.Lock()      # the queue file
_flushing = False


def _setting(key, default):
    try:
        from src.settings import get_setting
        return get_setting(key, default)
    except Exception:
        return default


# ── rendering ───────────────────────────────────────────────────────────

def to_ascii(text: str) -> str:
    """What the printer's built-in character set can show: curly quotes and dashes become plain ones."""
    for a, b in _ASCII.items():
        text = text.replace(a, b)
    return text.encode("ascii", "ignore").decode()


def wrap(text: str, cols: int = COLS) -> str:
    """Wrap to the paper width, keeping her line breaks and blank lines."""
    out = []
    for line in to_ascii(text).splitlines():
        if not line.strip():
            out.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        out.extend(textwrap.wrap(line, cols, subsequent_indent=" " * min(indent, 8), drop_whitespace=True,
                                 break_long_words=True) or [""])
    return "\n".join(out).strip("\n") + "\n"


def raster(image) -> bytes:
    """A 1-bit PIL image as one ESC/POS ``GS v 0`` raster block."""
    width, height = image.size
    row = (width + 7) // 8
    px = image.load()
    data = bytearray()
    for y in range(height):
        for bx in range(row):
            byte = 0
            for bit in range(8):
                x = bx * 8 + bit
                if x < width and px[x, y] == 0:
                    byte |= 0x80 >> bit
            data.append(byte)
    return b"\x1dv0\x00" + bytes([row & 255, row >> 8, height & 255, height >> 8]) + bytes(data)


def picture(path: Path) -> bytes:
    """Her dream image, fitted to the paper and dithered. Contrast is lifted first: pastel pixel art goes grey."""
    from PIL import Image, ImageEnhance, ImageOps
    img = Image.open(path).convert("L")
    img = img.resize((DOTS, max(1, round(img.height * DOTS / img.width))), Image.LANCZOS)
    img = ImageEnhance.Contrast(ImageOps.autocontrast(img, cutoff=1)).enhance(1.3)
    return raster(img.convert("1"))


def _front(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    try:
        meta = yaml.safe_load(head)
    except yaml.YAMLError:
        meta = None
    return (meta if isinstance(meta, dict) else {}), body


def render_dream(note: Path) -> bytes:
    """Header, the picture, her fragments, and what she wrote about it. Not the note she dreamt from."""
    meta, body = _front(note.read_text(encoding="utf-8"))
    heading = re.search(r"^# (.+)$", body, re.M)
    title = heading.group(1).replace("Dream:", "").strip() if heading else str(meta.get("date") or "")
    prose = body[heading.end():] if heading else body
    prose = re.split(r"^> \[!", prose, maxsplit=1, flags=re.M)[0]
    prose = re.sub(r"!\[\[[^\]]*\]\]", "", prose).strip()
    fragments = [str(f) for f in (meta.get("fragments") or [])][:8]
    out = INIT + CENTER + BOLD + TALL + b"a dream\n" + NORMAL + UNBOLD + to_ascii(title).encode() + b"\n\n"
    image = note.with_name(str(meta.get("image") or f"{note.stem}.png"))
    if image.is_file():
        out += picture(image) + b"\n"
    if fragments:
        out += "\n".join(wrap(f"~ {f} ~").rstrip("\n") for f in fragments).encode() + b"\n\n"
    out += LEFT + wrap(prose).encode() + b"\n" + CENTER + b"- Mabel -\n" + FEED
    return out


def render_paper(note: Path) -> bytes:
    """A note she chose to print for him: a small date line, then her words exactly as she wrote them."""
    meta, body = _front(note.read_text(encoding="utf-8"))
    words = str(meta.get("printed_text") or "").strip() or body.strip()
    stamp = ""
    try:
        stamp = datetime.fromisoformat(str(meta.get("written"))).astimezone().strftime("%a %-d %b, %H:%M")
    except (TypeError, ValueError):
        pass
    out = INIT + CENTER + (to_ascii(stamp).encode() + b"\n" if stamp else b"") + b"- - - - - - - -\n\n" + LEFT
    return out + wrap(words).encode() + FEED


def render_morning(note: Path) -> bytes:
    """His morning page: a header, then the page as she and the to-do list made it. "## " lines print bold."""
    meta, body = _front(note.read_text(encoding="utf-8"))
    stamp = ""
    try:
        stamp = datetime.fromisoformat(str(meta.get("written"))).astimezone().strftime("%A %-d %B")
    except (TypeError, ValueError):
        pass
    out = INIT + CENTER + BOLD + TALL + b"good morning\n" + NORMAL + UNBOLD + to_ascii(stamp).encode() + b"\n"
    out += b"- - - - - - - -\n\n" + LEFT
    for line in body.strip("\n").splitlines():
        if line.startswith("## "):
            out += b"\n" + BOLD + wrap(line[3:]).encode() + UNBOLD
        else:
            out += wrap(line).encode() if line.strip() else b"\n"
    return out + b"\n" + CENTER + b"- Mabel -\n" + FEED


def render(job: dict, mabel_vault: Path = _REPO) -> bytes:
    note = (mabel_vault / job["ref"]).resolve()
    if mabel_vault.resolve() not in note.parents or not note.is_file():
        raise FileNotFoundError(job["ref"])
    return {"dream": render_dream, "morning": render_morning}.get(job["kind"], render_paper)(note)


# ── the wire ────────────────────────────────────────────────────────────

class PrinterUnavailable(OSError):
    pass


def _send_usb(device: str, data: bytes) -> None:
    fd = os.open(device, os.O_WRONLY)
    try:
        for i in range(0, len(data), CHUNK):
            os.write(fd, data[i:i + CHUNK])
    finally:
        os.close(fd)


def _send_bt_libc(address: str, data: bytes) -> None:
    """RFCOMM through libc, for Pythons built without AF_BLUETOOTH (the uv-managed server env is one)."""
    import ctypes
    import errno
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.socket(31, socket.SOCK_STREAM, 3)          # AF_BLUETOOTH, BTPROTO_RFCOMM
    if fd < 0:
        raise OSError(ctypes.get_errno(), "bluetooth socket: " + os.strerror(ctypes.get_errno()))
    try:
        # struct sockaddr_rc { sa_family_t family; bdaddr_t addr (little-endian); uint8_t channel; } (+1 pad)
        bdaddr = bytes(reversed(bytes.fromhex(address.replace(":", ""))))
        addr = (31).to_bytes(2, "little") + bdaddr + bytes([1, 0])
        if libc.connect(fd, ctypes.c_char_p(addr), len(addr)) != 0:
            err = ctypes.get_errno() or errno.EHOSTDOWN
            raise OSError(err, os.strerror(err))
        for i in range(0, len(data), CHUNK):
            view = data[i:i + CHUNK]
            while view:
                view = view[os.write(fd, view):]
            time.sleep(0.04)
        time.sleep(min(8.0, 1.0 + len(data) / 6000))
    finally:
        os.close(fd)


def _send_bt(address: str, data: bytes) -> None:
    if not hasattr(socket, "AF_BLUETOOTH"):
        return _send_bt_libc(address, data)
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    sock.settimeout(20)
    try:
        sock.connect((address, 1))
        for i in range(0, len(data), CHUNK):        # the printer's buffer is small; let it keep up
            sock.sendall(data[i:i + CHUNK])
            time.sleep(0.04)
        time.sleep(min(8.0, 1.0 + len(data) / 6000))  # closing early can cut the tail off
    finally:
        sock.close()


def print_server() -> tuple[str, str]:
    """(url, token) of the deck's print server (tui/mabel_tui/printserver.py), or ("", "") to print directly."""
    return str(_setting("printer_server_url", "") or "").rstrip("/"), str(_setting("printer_server_token", "") or "")


def _send_server(url: str, token: str, data: bytes) -> str:
    import httpx
    try:
        r = httpx.post(f"{url}/print", content=data, headers={"Authorization": f"Bearer {token}",
                                                              "Content-Type": "application/octet-stream"},
                       timeout=httpx.Timeout(90.0, connect=8.0))
    except httpx.HTTPError as exc:
        raise PrinterUnavailable(f"print server {url}: {exc}") from exc
    if r.status_code != 200:
        try:
            why = r.json().get("error")
        except ValueError:
            why = r.text[:120]
        raise PrinterUnavailable(f"print server: {r.status_code} {why}")
    return "deck:" + str(r.json().get("route", "?"))


def send(data: bytes) -> str:
    """Print raw ESC/POS bytes: through the deck's print server when one is set (it owns the printer
    and keeps it awake), else directly: USB if it's plugged in, else Bluetooth. Returns the route used."""
    server, token = print_server()
    if server:
        with _wire:
            return _send_server(server, token, data)
    usb = str(_setting("printer_usb_device", DEFAULT_USB) or "")
    address = str(_setting("printer_bt_address", DEFAULT_BT) or "")
    errors = []
    with _wire:
        if usb and os.path.exists(usb):
            try:
                _send_usb(usb, data)
                return "usb"
            except OSError as exc:
                errors.append(f"usb: {exc}")
        if address:
            for attempt in range(2):                  # a dozing module often refuses the first connect
                try:
                    _send_bt(address, data)
                    return "bluetooth"
                except OSError as exc:
                    if attempt:
                        errors.append(f"bluetooth: {exc}")
                    else:
                        time.sleep(2)
    raise PrinterUnavailable("; ".join(errors) or "no printer configured")


# ── the queue ───────────────────────────────────────────────────────────

def _state_path(data_dir: Path) -> Path:
    return data_dir / "persona" / "printer.json"


def load_state(data_dir: Path) -> dict:
    try:
        state = json.loads(_state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("pending", [])
    state.setdefault("done", [])
    return state


def _save_state(data_dir: Path, state: dict) -> None:
    path = _state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["done"] = state["done"][-MAX_DONE:]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def enabled() -> bool:
    return bool(_setting("printer_enabled", True))


def enqueue(kind: str, ref: str, *, title: str = "", data_dir: Path | None = None,
            now: datetime | None = None) -> dict | None:
    """Queue a journal note (path relative to the vault) for printing. None when printing is off."""
    if kind not in KINDS:
        raise ValueError(kind)
    if not enabled():
        return None
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    with _lock:
        state = load_state(data_dir)
        if any(j["ref"] == ref for j in state["pending"]):
            return None
        job = {"id": f"print-{uuid.uuid4().hex[:8]}", "kind": kind, "ref": ref, "title": title[:120],
               "queued": now.isoformat(timespec="seconds"), "attempts": 0, "last_try": None, "error": ""}
        state["pending"].append(job)
        _save_state(data_dir, state)
    return job


def _contact(data_dir: Path, now: datetime, *, ok: bool) -> None:
    """Remember when we last reached the printer (ok) or last tried a keep-alive."""
    with _lock:
        state = load_state(data_dir)
        contact = state.setdefault("contact", {})
        contact["tried_at"] = now.isoformat(timespec="seconds")
        if ok:
            contact["ok_at"] = contact["tried_at"]
        _save_state(data_dir, state)


def keepalive_due(data_dir: Path, now: datetime) -> bool:
    """Time to poke the printer so its auto-off timer doesn't run out (it only comes back with its button)."""
    minutes = float(_setting("printer_keepalive_minutes", KEEPALIVE_MINUTES) or 0)
    if not enabled() or minutes <= 0 or print_server()[0]:
        return False                          # with a print server the deck keeps it awake, around the clock
    contact = load_state(data_dir).get("contact") or {}
    stamps = [datetime.fromisoformat(contact[k]) for k in ("ok_at", "tried_at") if contact.get(k)]
    return not stamps or now - max(stamps) >= timedelta(minutes=minutes)


def keepalive(*, data_dir: Path | None = None, now: datetime | None = None, sender=None) -> bool:
    """Send a status request (prints nothing). True if the printer was there. Blocking: run it in a thread."""
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    try:
        (sender or send)(STATUS)
    except OSError as exc:
        logger.info("[printer] keep-alive: not reachable (%s)", exc)
        _contact(data_dir, now, ok=False)
        return False
    _contact(data_dir, now, ok=True)
    return True


def due(state: dict, now: datetime) -> list[dict]:
    return [j for j in state["pending"]
            if not j.get("last_try") or now - datetime.fromisoformat(j["last_try"]) >= RETRY_AFTER]


def flush(*, data_dir: Path | None = None, mabel_vault: Path = _REPO, now: datetime | None = None,
          sender=None) -> list[dict]:
    """Try every due job once, oldest first. Blocking (Bluetooth connects take seconds): run it in a thread.

    Stops at the first "printer unreachable" so a switched-off printer costs one connect attempt per round.
    Returns what happened to each job tried.
    """
    global _flushing
    from src.constants import DATA_DIR
    data_dir = Path(data_dir or DATA_DIR)
    now = now or datetime.now(timezone.utc)
    sender = sender or send
    if _flushing:
        return []
    _flushing = True
    results = []
    try:
        state = load_state(data_dir)
        for job in list(state["pending"]):
            if now - datetime.fromisoformat(job["queued"]) > MAX_AGE:
                state["pending"].remove(job)
                state["done"].append({**job, "outcome": "expired", "at": now.isoformat(timespec="seconds")})
                results.append({"id": job["id"], "outcome": "expired"})
        for job in due(state, now):
            try:
                data = render(job, mabel_vault)
            except Exception as exc:                     # the note is gone or unreadable: no point retrying
                state["pending"].remove(job)
                state["done"].append({**job, "outcome": "unrenderable", "error": str(exc)[:200],
                                      "at": now.isoformat(timespec="seconds")})
                results.append({"id": job["id"], "outcome": "unrenderable"})
                continue
            try:
                route = sender(data)
            except OSError as exc:
                job.update(attempts=job["attempts"] + 1, last_try=now.isoformat(timespec="seconds"),
                           error=str(exc)[:200])
                results.append({"id": job["id"], "outcome": "waiting", "error": job["error"]})
                break
            state["pending"].remove(job)
            state["done"].append({**job, "outcome": "printed", "route": route,
                                  "at": now.isoformat(timespec="seconds")})
            results.append({"id": job["id"], "outcome": "printed", "route": route})
            logger.info("[printer] printed %s %s via %s", job["kind"], job["ref"], route)
        if any(r["outcome"] == "printed" for r in results):
            state.setdefault("contact", {})["ok_at"] = now.isoformat(timespec="seconds")
            state["contact"]["tried_at"] = state["contact"]["ok_at"]
        if results:
            with _lock:
                fresh = load_state(data_dir)            # keep jobs queued while we were printing
                handled = {j["id"] for j in state["done"]} | {j["id"] for j in state["pending"]}
                state["pending"] += [j for j in fresh["pending"] if j["id"] not in handled]
                _save_state(data_dir, state)
        return results
    finally:
        _flushing = False


def has_due(data_dir: Path, now: datetime) -> bool:
    return enabled() and bool(due(load_state(data_dir), now))


if __name__ == "__main__":
    import argparse
    cli = argparse.ArgumentParser(prog="python -m src.persona.printer")
    cli.add_argument("--test", action="store_true", help="print a short test slip now")
    cli.add_argument("--dream", metavar="NOTE", help="print a dream note now (path to its .md)")
    cli.add_argument("--paper", metavar="NOTE", help="print one of her paper notes now")
    cli.add_argument("--flush", action="store_true", help="try the queue now")
    cli.add_argument("--status", action="store_true", help="show the queue")
    args = cli.parse_args()
    from src.constants import DATA_DIR
    if args.test:
        print(send(INIT + CENTER + b"printer test\n" + LEFT + wrap("0123456789" * 4).encode() + FEED))
    if args.dream:
        print(send(render_dream(Path(args.dream).resolve())))
    if args.paper:
        print(send(render_paper(Path(args.paper).resolve())))
    if args.flush:
        print(json.dumps(flush(), indent=1))
    if args.status or not any(vars(args).values()):
        print(json.dumps(load_state(Path(DATA_DIR)), indent=1, ensure_ascii=False))
