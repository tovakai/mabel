"""COMMS · INBOX (mabel_tui/inbox.py) against a fake Beeper relay, driven by keys."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

from textual.app import App

from mabel_tui import inbox as inbox_mod
from mabel_tui.inbox import Inbox
from mabel_tui.palette import css_variables

CHATS = [
    {"id": "c1", "accountID": "wa", "title": "Maja", "type": "single", "unreadCount": 2,
     "lastActivity": "2026-11-18T17:00:00Z", "preview": {"text": "home soon?", "senderName": "Maja"}},
    {"id": "c2", "accountID": "tg", "title": "Gremlins", "type": "group", "unreadCount": 0,
     "lastActivity": "2026-11-17T09:00:00Z", "preview": {"text": "lol", "senderName": "Bob"}},
]
MESSAGES = {"c1": [
    {"id": "m1", "chatID": "c1", "senderName": "Maja", "isSender": False, "text": "home soon?",
     "timestamp": "2026-11-18T16:59:00Z", "sortKey": "1"},
    {"id": "m2", "chatID": "c1", "senderName": "you", "isSender": True, "text": "20 min",
     "timestamp": "2026-11-18T17:00:00Z", "sortKey": "2", "reactions": [{"reactionKey": "👍", "participantID": "maja"}]},
]}


class FakeMabel:
    def __init__(self, token=True):
        self.token, self.calls = token, []

    async def beeper_status(self):
        return {"token": self.token, "accounts": [{"accountID": "wa", "network": "WhatsApp"},
                                                   {"accountID": "tg", "network": "Telegram"}]}

    async def beeper(self, method, path, *, params=None, body=None):
        self.calls.append((method, path, params, body))
        if path in ("v1/chats", "v1/chats/search"):
            items = CHATS
            if params and params.get("unreadOnly"):
                items = [c for c in items if c["unreadCount"]]
            if params and params.get("query"):
                items = [c for c in items if params["query"].lower() in c["title"].lower()]
            return {"items": copy.deepcopy(items), "hasMore": False}      # like a real server: fresh objects
        if path.endswith("/messages") and method == "GET":
            return {"items": MESSAGES.get(path.split("/")[2], []), "hasMore": False}
        return {}


class Host(App):
    CSS_PATH = Path(inbox_mod.__file__).with_name("theme.tcss")

    def __init__(self, mabel):
        super().__init__()
        self.mabel = mabel

    def get_css_variables(self):
        return {**super().get_css_variables(), **css_variables()}

    def compose(self):
        yield Inbox(id="inbox")


def test_inbox_without_a_token():
    async def run():
        app = Host(FakeMabel(token=False))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.3)
            ib = app.query_one(Inbox)
            assert ib.state == "no-token" and "set-token" in ib.draw_chats(False, 30, 40).plain
    asyncio.run(run())


def test_inbox_reading_and_writing():
    async def run():
        fake = FakeMabel()
        app = Host(fake)
        async with app.run_test(size=(140, 40)) as pilot:
            ib = app.query_one(Inbox)
            await pilot.pause(0.3)
            ib.focus_list()
            assert ib.state == "ready" and [c["title"] for c in ib.chats] == ["Maja", "Gremlins"]
            listing = ib.draw_chats(True, 30, 42).plain
            assert " 2 Maja" in listing and "WhatsApp" in listing
            await pilot.press("enter")                                    # open Maja: marks read
            await pilot.pause(0.3)
            assert ib.open["id"] == "c1" and [m["id"] for m in ib.messages] == ["m1", "m2"]
            assert ("POST", "v1/chats/c1/read", None, {}) in fake.calls
            conv = ib.draw_messages(True, 30, 80).plain
            assert "home soon?" in conv and "20 min" in conv and "👍" in conv
            await pilot.press("i", *"on my way", "enter")                 # send
            await pilot.pause(0.3)
            assert ("POST", "v1/chats/c1/messages", None, {"text": "on my way"}) in fake.calls
            await pilot.press("escape", "k", "k", "r", *"yes", "enter")   # reply to m1
            await pilot.pause(0.3)
            assert ("POST", "v1/chats/c1/messages", None, {"text": "yes", "replyToMessageID": "m1"}) in fake.calls
            await pilot.press("escape", "j", "e")                         # edit m2 (mine)
            box = app.query_one(inbox_mod.Compose)
            assert box.value == "20 min"
            box.value = "15 min"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert ("PUT", "v1/chats/c1/messages/m2", None, {"text": "15 min"}) in fake.calls
            await pilot.press("escape", "plus", "2", "enter")             # react ❤️ to the selected one
            await pilot.pause(0.3)
            assert any(c[1].endswith("/reactions") and c[3] == {"reactionKey": "❤️"} for c in fake.calls)
            await pilot.press("escape", "escape")                         # back to the chats
            assert app.focused is app.query_one(inbox_mod.ChatList)
            await pilot.press("u")
            await pilot.pause(0.3)
            assert [c["title"] for c in ib.chats] == ["Maja"]
            await pilot.press("u", "full_stop")                           # network: Telegram first (sorted)
            await pilot.pause(0.3)
            assert ib.network == "Telegram" and [c["title"] for c in ib.chats] == ["Gremlins"]
    asyncio.run(run())


class _Wheel:                                                   # stand-in for a MouseScroll event
    def stop(self):
        pass


def _scroll(app, sel, up):
    w = app.query_one(sel)
    (w.on_mouse_scroll_up if up else w.on_mouse_scroll_down)(_Wheel())


def test_inbox_with_the_mouse():
    async def run():
        app = Host(FakeMabel())
        async with app.run_test(size=(140, 40)) as pilot:
            ib = app.query_one(Inbox)
            await pilot.pause(0.3)
            await pilot.click("#inbox-chats", offset=(5, 3))            # 2nd chat (border + 2 lines per chat)
            await pilot.pause(0.3)
            assert ib.open and ib.open["id"] == "c2"
            await pilot.click("#inbox-chats", offset=(5, 1))            # 1st chat
            await pilot.pause(0.3)
            assert ib.open["id"] == "c1" and len(ib.messages) == 2
            _scroll(app, "#inbox-conv", up=True)                        # the wheel walks back
            assert ib.msg_index == 0
            _scroll(app, "#inbox-conv", up=False)
            _scroll(app, "#inbox-conv", up=False)
            assert ib.msg_index == -1, "down past the newest follows new messages again"
            await pilot.pause(0.1)
            row = ib.line_map.index(0)                                  # click Maja's message
            await pilot.click("#inbox-conv", offset=(4, row + 1))
            assert ib.msg_index == 0
            ib.focus_list()
            _scroll(app, "#inbox-chats", up=False)
            assert ib.index == 1
            await pilot.click("#inbox-compose")
            assert app.focused is app.query_one(inbox_mod.Compose) and ib._compose_mode == "send"
    asyncio.run(run())
