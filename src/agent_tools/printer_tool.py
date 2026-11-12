"""print_on_paper: Anthon asks Mabel to print something on the thermal printer on his desk.

The text is saved as a small note in her vault (journal/printed/), queued like
her own prints (src/persona/printer.py), and printed right away when the
printer answers. If it's switched off, the job waits and prints once he turns
it on (the heartbeat retries every couple of minutes for up to three days).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[2]
MAX_CHARS = 4000          # ~1.5 m of paper at 32 columns; enough for a recipe or a list


def save_note(text: str, title: str, *, mabel_vault: Path = _REPO, now: datetime | None = None) -> str:
    """Write the print as journal/printed/<stamp>.md and return its path relative to the vault."""
    now = now or datetime.now(timezone.utc)
    folder = mabel_vault / "journal" / "printed"
    folder.mkdir(parents=True, exist_ok=True)
    stem = now.astimezone().strftime("%Y-%m-%d-%H%M%S")
    path = folder / f"{stem}.md"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}.md"
    words = (f"{title}\n\n" if title else "") + text
    meta = {"type": "printed", "written": now.isoformat(timespec="seconds"), "asked_by": "Anthon",
            "title": title or None, "printed_text": words}
    path.write_text("---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
                    + words + "\n", encoding="utf-8")
    return str(path.relative_to(mabel_vault))


class PrintTool:
    def __init__(self, *, mabel_vault: Path = _REPO, data_dir: Path | None = None, sender=None):
        self.mabel_vault, self.data_dir, self.sender = mabel_vault, data_dir, sender

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.persona import printer
        try:
            args = json.loads(content) if content.strip().startswith("{") else {"text": content}
        except ValueError:
            args = {"text": content}
        if not isinstance(args, dict):
            return {"error": "print_on_paper expects {\"text\": ..., \"title\": optional}", "exit_code": 1}
        text = re.sub(r"\n{3,}", "\n\n", str(args.get("text") or "").strip())
        title = " ".join(str(args.get("title") or "").split())[:60]
        if not text:
            return {"error": "print_on_paper needs the text to print", "exit_code": 1}
        if len(text) > MAX_CHARS:
            return {"error": f"That's too long for the receipt printer ({len(text)} characters, max {MAX_CHARS}). "
                             "Shorten it or split it into parts.", "exit_code": 1}
        if not printer.enabled():
            return {"error": "Printing is switched off (setting printer_enabled).", "exit_code": 1}
        ref = save_note(text, title, mabel_vault=self.mabel_vault)
        job = printer.enqueue("chat", ref, title=title or text.splitlines()[0][:60], data_dir=self.data_dir)
        if job is None:
            return {"error": "The print couldn't be queued.", "exit_code": 1}
        results = await asyncio.to_thread(printer.flush, data_dir=self.data_dir, mabel_vault=self.mabel_vault,
                                          sender=self.sender)
        mine = next((r for r in results if r["id"] == job["id"]), None)
        if mine and mine["outcome"] == "printed":
            status = "printed: it's coming out of the printer now"
        elif any(r["outcome"] == "waiting" for r in results):          # it stops at the first unreachable try
            status = ("queued: the printer didn't answer (probably switched off). It prints by itself within a "
                      "couple of minutes of being switched on.")
        else:
            status = "queued: another print is going out first; this one follows within a couple of minutes"
        return {"output": json.dumps({"status": status, "note": ref, "characters": len(text)}, ensure_ascii=False),
                "exit_code": 0}
