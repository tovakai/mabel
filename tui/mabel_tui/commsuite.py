"""CH-01 COMMS as a suite (suite.py): MABEL (the chat with her) and INBOX (his Beeper chats).

F1 / alt+1 opens the channel; pressed again it moves to the other app.
"""
from __future__ import annotations

from textual.containers import Vertical

from .comms import Comms
from .inbox import Inbox
from .suite import Suite


class CommsSuite(Suite):
    APPS = [("mabel", "MABEL", "talk with her"),
            ("inbox", "INBOX", "your chats via Beeper · WhatsApp, Signal, Telegram…")]
    FKEY = "F1"

    def make(self):
        return [Vertical(Comms(), id="mabel", classes="comms-holder"), Inbox(id="inbox")]   # Comms keeps its own id

    def shown(self, key: str) -> None:
        if key == "inbox":
            inbox = self.query_one(Inbox)
            if inbox.state != "ready":
                inbox.load_chats()

    def focus_app(self, key: str) -> None:
        if key == "mabel":
            self.query_one("#prompt").focus()                 # chat is for typing: straight into the prompt
        else:
            self.query_one(Inbox).focus_list()
