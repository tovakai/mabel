"""Keep note-edit turns from browsing instead of editing.

On a sealed note-edit turn the model can read, list and search, and write.
Live runs spent 14+ read-only rounds listing folders and reading unrelated
notes before the first write. This budget counts read-only rounds before the
first successful write: it nudges first, then withdraws the discovery tools
(ls, glob, grep) so only reading known paths and writing remain.

It never grants tools, never forces a write, and stops counting after the
first successful write (reading back to verify is fine).
"""
from __future__ import annotations

from dataclasses import dataclass, field

WRITE_TOOLS = frozenset({"edit_file", "write_file", "apply_patch", "vault_batch_edit"})
DISCOVERY_TOOLS = frozenset({"ls", "glob", "grep"})

NUDGE_AFTER = 5
RESTRICT_AFTER = 8

NUDGE = (
    "You have spent {rounds} rounds reading and searching without editing. You "
    "already have what this request needs: make the requested edits now with "
    "edit_file/write_file. Do not list folders or read unrelated notes. If "
    "something essential is genuinely missing, ask the user instead."
)
RESTRICT = (
    "Folder listing and searching (ls, glob, grep) are now unavailable for this "
    "turn. Make the requested edits to the notes you have already read, or tell "
    "the user exactly what is missing."
)


@dataclass
class EditExplorationBudget:
    read_rounds: int = 0
    wrote: bool = False
    nudged: bool = False
    restricted: bool = False
    _done: set = field(default_factory=set)

    def observe(self, records) -> str | None:
        """Return "nudge", "restrict" or None after one round's tool records."""
        if self.wrote:
            return None
        tools = []
        for record in records or ():
            if not isinstance(record, dict):
                continue
            name = record.get("tool_name")
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            if name in WRITE_TOOLS and result.get("exit_code") == 0 and not result.get("error"):
                self.wrote = True
                return None
            tools.append(name)
        if not tools:
            return None
        self.read_rounds += 1
        if self.read_rounds >= RESTRICT_AFTER and not self.restricted:
            self.restricted = True
            return "restrict"
        if self.read_rounds >= NUDGE_AFTER and not self.nudged:
            self.nudged = True
            return "nudge"
        return None

    def message(self, decision: str) -> str:
        return (NUDGE.format(rounds=self.read_rounds) if decision == "nudge" else RESTRICT)


NOTE_EDIT_RULES = (
    "Note-editing rules for this turn:\n"
    "- Change only what the user asked for; keep the note's existing structure and frontmatter.\n"
    "- Never write commentary about the edit, this chat or your sources into the note "
    "(e.g. 'that was a typo; it's fixed', 'your theory'); explain in your reply instead.\n"
    "- Never write first-person content in the user's voice (progress, opinions, ratings) "
    "unless the user supplied it.\n"
    "- Add facts only from tool results in this turn or text already in the vault. Without "
    "web access, do not add 'public info' from memory or earlier chats; say in your reply "
    "what could not be verified."
)
