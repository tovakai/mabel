"""INBOX (COMMS's second app): his Beeper chats (WhatsApp, Signal, Telegram…), relayed by Mabel.

Our own Textual code, modelled on beeptui (github.com/mitchmalone/beeptui, MIT): Beeper Desktop
does the networks and encryption; we talk to its API through Mabel (/api/beeper, routes/beeper_routes.py).

Left: chats (unread first marks, network). Right: the conversation and a compose line.
Chats: j/k move · enter open · / filter · u unread only · , . network · r refresh.
Conversation: j/k pick a message · i or tab write · r reply to it · e edit yours · + react ·
m mark unread · esc back to the chats. Compose: enter sends · esc stops.
New messages are fetched every few seconds (Beeper's WebSocket can come later).
"""
from __future__ import annotations

from datetime import datetime, timezone

from rich.text import Text
from textual import events, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Input

from .api import MabelError
from .palette import P

POLL_CHATS_S = 8.0
POLL_OPEN_S = 3.0
REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "🙏"]


def _when(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    now = datetime.now().astimezone()
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if (now - t).days < 7:
        return t.strftime("%a")
    return t.strftime("%d %b")


def _items(data) -> list[dict]:
    return data if isinstance(data, list) else (data or {}).get("items", [])


def _preview(chat: dict) -> str:
    pv = chat.get("preview") or {}
    if isinstance(pv, dict):
        who = "you: " if pv.get("isSender") else (f"{pv.get('senderName')}: " if chat.get("type") == "group"
                                                  and pv.get("senderName") else "")
        body = pv.get("text") or ("📎 attachment" if pv.get("attachments") else "")
        return (who + body).replace("\n", " ")
    return ""


class ChatList(Widget, can_focus=True):
    def __init__(self, inbox: "Inbox", **kw):
        super().__init__(**kw)
        self.inbox = inbox

    def render(self) -> Text:
        return self.inbox.draw_chats(self.has_focus, self.size.height, self.size.width)

    async def _on_key(self, event: events.Key) -> None:
        if self.inbox.chat_key(event):
            event.stop()
            event.prevent_default()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # mouse (the PC): a click opens the chat under it, the wheel moves through the list
    def on_click(self, event: events.Click) -> None:
        ib = self.inbox
        at = event.get_content_offset(self)               # inside the border and padding
        if at is None:
            return
        i = ib.top + at.y // 2                            # two lines per chat
        if 0 <= i < len(ib.chats):
            ib.index = i
            ib.open_chat(ib.chats[i])
            ib.refresh_all()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.inbox.scroll_chats(3)
        event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.inbox.scroll_chats(-3)
        event.stop()


class Conversation(Widget, can_focus=True):
    def __init__(self, inbox: "Inbox", **kw):
        super().__init__(**kw)
        self.inbox = inbox

    def render(self) -> Text:
        return self.inbox.draw_messages(self.has_focus, self.size.height, self.size.width)

    async def _on_key(self, event: events.Key) -> None:
        if self.inbox.message_key(event):
            event.stop()
            event.prevent_default()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def on_click(self, event: events.Click) -> None:
        self.focus()
        ib = self.inbox
        at = event.get_content_offset(self)
        if at is not None and 0 <= at.y < len(ib.line_map) and ib.line_map[at.y] >= 0:
            ib.msg_index = ib.line_map[at.y]
            ib.refresh_all()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self.inbox.scroll_messages(1)
        event.stop()

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self.inbox.scroll_messages(-1)
        event.stop()


class Compose(Input):
    BINDINGS = [Binding("escape", "stop", show=False)]

    def on_click(self, event: events.Click) -> None:
        ib = self.screen.query_one(Inbox)
        if ib.open and getattr(ib, "_compose_mode", "") not in ("send", "edit", "react", "filter"):
            ib.start_compose()                                # clicking the line starts writing

    def action_stop(self) -> None:
        self.screen.query_one(Inbox).stop_compose()


class Inbox(Vertical):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.chats: list[dict] = []
        self.accounts: dict[str, str] = {}          # accountID → network name
        self.index = 0
        self.top = 0
        self.filter = ""
        self.unread_only = False
        self.network: str | None = None
        self.open: dict | None = None
        self.messages: list[dict] = []
        self.msg_index = -1                          # -1 = the newest
        self.reply_to: dict | None = None
        self.editing: dict | None = None
        self.message = ""
        self.state = "connecting"                    # connecting | ready | no-token | offline
        self.line_map: list[int] = []                # conversation rows on screen → message index (-1: none)

    def compose(self) -> ComposeResult:
        with Horizontal(id="inbox-main"):
            chats = ChatList(self, id="inbox-chats", classes="panel section")
            chats.border_title = "CHATS"
            yield chats
            with Vertical(id="inbox-right"):
                conv = Conversation(self, id="inbox-conv", classes="panel section")
                conv.border_title = "—"
                yield conv
                yield Compose(id="inbox-compose", placeholder="i or tab: write · enter sends · esc")

    def on_mount(self) -> None:
        self.load_chats()
        self.set_interval(POLL_CHATS_S, self._poll_chats)
        self.set_interval(POLL_OPEN_S, self._poll_open)

    def focus_list(self) -> None:
        self.query_one(ChatList).focus()

    # --- data ------------------------------------------------------------------------------------

    @property
    def api(self):
        return self.app.mabel

    def _visible(self) -> bool:
        return self.display and self.screen.is_current and self.parent is not None and \
            getattr(self.parent, "current", "inbox") == "inbox"

    def _poll_chats(self) -> None:
        if self._visible() and self.state in ("ready", "offline"):
            self.load_chats(quiet=True)

    def _poll_open(self) -> None:
        if self._visible() and self.open and self.state == "ready":
            self.load_messages(quiet=True)

    @work(exclusive=True, group="inbox-chats")
    async def load_chats(self, quiet: bool = False) -> None:
        try:
            if not self.accounts:
                st = await self.api.beeper_status()
                if not st.get("token"):
                    self.state = "no-token"
                    self.refresh_all()
                    return
                self.accounts = {a.get("accountID", ""): a.get("network") or a.get("accountID", "")
                                 for a in st.get("accounts") or []}
            params = {"limit": 100}
            if self.filter:
                params["query"] = self.filter
            if self.unread_only:
                params["unreadOnly"] = "true"
            if self.network:
                params["accountIDs"] = [k for k, v in self.accounts.items() if v == self.network]
            path = "v1/chats/search" if (self.filter or self.unread_only) else "v1/chats"
            data = await self.api.beeper("GET", path, params=params)
            chats = _items(data)
            if self.network and path == "v1/chats":
                chats = [c for c in chats if self.accounts.get(c.get("accountID")) == self.network]
            keep = self.chats[self.index]["id"] if self.chats and self.index < len(self.chats) else None
            self.chats = [c for c in chats if not c.get("isArchived")]
            if keep:
                self.index = next((i for i, c in enumerate(self.chats) if c["id"] == keep), self.index)
            self.state = "ready"
        except MabelError as exc:
            self.state = "no-token" if "set-token" in str(exc) else "offline"
            if not quiet:
                self.message = str(exc)
        except Exception as exc:
            self.state = "offline"
            if not quiet:
                self.message = f"can't reach Mabel: {exc}"
        self.refresh_all()

    @work(exclusive=True, group="inbox-messages")
    async def load_messages(self, quiet: bool = False) -> None:
        chat = self.open
        if not chat:
            return
        try:
            data = await self.api.beeper("GET", f"v1/chats/{chat['id']}/messages")
            msgs = [m for m in _items(data) if not m.get("isHidden")]
            msgs.sort(key=lambda m: m.get("sortKey") or m.get("timestamp") or "")
            if self.open is chat:
                grew = len(msgs) != len(self.messages)
                self.messages = msgs
                if grew and self.msg_index != -1 and not quiet:
                    self.msg_index = -1
        except Exception as exc:
            if not quiet:
                self.message = str(exc)
        self.refresh_all()

    def open_chat(self, chat: dict) -> None:
        self.open, self.messages, self.msg_index = chat, [], -1
        self.reply_to = self.editing = None
        self.query_one(Conversation).border_title = f"{chat.get('title') or '?'} · {self._network(chat)}"
        self.load_messages()
        if chat.get("unreadCount"):
            self._post(f"v1/chats/{chat['id']}/read", {})
            chat["unreadCount"] = 0
        self.query_one(Conversation).focus()

    @work(group="inbox-actions")
    async def _post(self, path: str, body: dict, method: str = "POST", then_reload: bool = False) -> None:
        try:
            await self.api.beeper(method, path, body=body)
            if then_reload:
                self.load_messages()
        except Exception as exc:
            self.message = f"✖ {exc}"
            self.refresh_all()

    def _network(self, chat: dict) -> str:
        return chat.get("network") or self.accounts.get(chat.get("accountID"), "")

    # --- drawing ---------------------------------------------------------------------------------

    def refresh_all(self) -> None:
        self.query_one(ChatList).refresh()
        self.query_one(Conversation).refresh()
        title = "CHATS"
        if self.network:
            title += f" · {self.network}"
        if self.unread_only:
            title += " · unread"
        if self.filter:
            title += f" · “{self.filter}”"
        self.query_one(ChatList).border_title = title

    def draw_chats(self, focused: bool, height: int, width: int) -> Text:
        t = Text()
        if self.state == "no-token":
            t.append("\n Beeper isn't connected yet.\n\n", style=f"bold {P.amber_hi}")
            t.append(" On the PC, in a terminal, run this\n (one line; it may wrap here):\n\n", style=P.amber)
            t.append(" cd ~/Documents/mabel; .venv-arch/bin/python -m src.beeper --set-token\n", style=P.teal)
            t.append("\n then r here.", style=P.amber_dim)
            return t
        if self.state == "connecting":
            return Text(" connecting…", style=P.amber_dim)
        if self.state == "offline" and not self.chats:
            return Text(f" ✖ {self.message or 'offline'}\n\n r tries again", style=P.red)
        rows = max(1, (height - 1) // 2)
        if self.index < self.top:
            self.top = self.index
        elif self.index >= self.top + rows:
            self.top = self.index - rows + 1
        for i in range(self.top, min(len(self.chats), self.top + rows)):
            c = self.chats[i]
            on = i == self.index
            unread = c.get("unreadCount") or 0
            when = _when(c.get("lastActivity"))
            name = (c.get("title") or "?")[: max(6, width - len(when) - 7)]
            style = f"bold {P.bg} on {P.amber}" if on and focused else (f"bold {P.amber_hi}" if unread else P.amber)
            mark = f"{unread:>2} " if unread else ("▸  " if self.open and c["id"] == self.open["id"] else "   ")
            line = f"{mark}{name}"
            t.append(line.ljust(max(0, width - len(when) - 1)), style=style)
            t.append(when + "\n", style=style if on and focused else P.amber_dim)
            sub = f"   {self._network(c)[:10]} · {_preview(c)}"
            t.append(sub[: width - 1] + "\n", style=P.amber_dim if not (on and focused) else f"{P.bg} on {P.amber_dim}")
        if not self.chats:
            t.append(" no chats" + (" match" if self.filter or self.unread_only or self.network else ""), style=P.amber_dim)
        return t

    def draw_messages(self, focused: bool, height: int, width: int) -> Text:
        t = Text()
        if not self.open:
            t.append("\n enter on a chat opens it", style=P.amber_dim)
            if self.message:
                t.append(f"\n\n {self.message}", style=P.sand)
            return t
        by_id = {m["id"]: m for m in self.messages}
        lines: list[tuple[int, Text]] = []                 # (message index, line)
        last_day = None
        wrap = max(10, width - 4)
        for i, m in enumerate(self.messages):
            day = (m.get("timestamp") or "")[:10]
            if day != last_day:
                lines.append((-2, Text(f"── {day} ──", style=P.amber_faint)))
                last_day = day
            mine = m.get("isSender")
            who = "you" if mine else (m.get("senderName") or "?")
            head = Text()
            head.append(f"{who}", style=f"bold {P.teal if mine else P.amber_hi}")
            head.append(f"  {_when(m.get('timestamp'))}", style=P.amber_dim)
            if m.get("editedTimestamp"):
                head.append(" (edited)", style=P.amber_dim)
            status = (m.get("sendStatus") or {}).get("status", "")
            if mine and status and status not in ("sent", "delivered", "read", "success"):
                head.append(f" · {status}", style=P.red)
            lines.append((i, head))
            if m.get("linkedMessageID"):
                ref = by_id.get(m["linkedMessageID"])
                quote = (ref.get("text") or "…") if ref else "an earlier message"
                lines.append((i, Text(f"  ↳ {quote[: wrap - 4]}", style=P.amber_dim)))
            body = "(deleted)" if m.get("isDeleted") else (m.get("text") or "")
            for a in m.get("attachments") or []:
                kind = "🎤 voice" if a.get("isVoiceNote") else ("sticker" if a.get("isSticker") else a.get("type", "file"))
                body += f"\n📎 {kind} {a.get('fileName') or ''}".rstrip()
            for link in m.get("links") or []:
                body += f"\n🔗 {link.get('title') or link.get('url')}"
            for para in body.split("\n") or [""]:
                while para:
                    lines.append((i, Text("  " + para[:wrap], style=P.amber if not m.get("isDeleted") else P.amber_dim)))
                    para = para[wrap:]
            if m.get("reactions"):
                counts: dict[str, int] = {}
                for r in m["reactions"]:
                    counts[r.get("reactionKey", "?")] = counts.get(r.get("reactionKey", "?"), 0) + 1
                lines.append((i, Text("  " + "  ".join(f"{k}{n if n > 1 else ''}" for k, n in counts.items()),
                                      style=P.sand)))
        sel = self.msg_index if self.msg_index >= 0 else len(self.messages) - 1
        rows = max(1, height - 2)
        last_sel = max((n for n, (i, _) in enumerate(lines) if i == sel), default=len(lines) - 1)
        start = max(0, min(len(lines) - rows, last_sel - rows + 2)) if self.msg_index >= 0 else max(0, len(lines) - rows)
        self.line_map = [i for i, _ in lines[start:start + rows]]
        for i, line in lines[start:start + rows]:
            if focused and i == sel and self.msg_index >= 0:
                t.append("▌", style=P.amber_hi)
            else:
                t.append(" ")
            t.append_text(line)
            t.append("\n")
        hint = ""
        if self.reply_to:
            hint = f"replying to {self.reply_to.get('senderName') or 'you'}: {(self.reply_to.get('text') or '')[:40]}"
        elif self.editing:
            hint = "editing your message"
        elif self.message:
            hint = self.message
        if hint:
            t.append(f" {hint}", style=f"bold {P.sand}")
        return t

    # --- keys --------------------------------------------------------------------------------------

    def chat_key(self, event: events.Key) -> bool:
        k, ch = event.key, event.character or ""
        self.message = ""
        if k in ("down", "j"):
            self.index = min(len(self.chats) - 1, self.index + 1)
        elif k in ("up", "k"):
            self.index = max(0, self.index - 1)
        elif k in ("enter", "l", "right") and self.chats:
            self.open_chat(self.chats[self.index])
        elif k == "r":
            self.accounts = {} if self.state == "no-token" else self.accounts
            self.load_chats()
        elif k == "u":
            self.unread_only = not self.unread_only
            self.index = 0
            self.load_chats()
        elif ch in (",", ".") and self.accounts:
            nets = [None] + sorted(set(self.accounts.values()))
            self.network = nets[(nets.index(self.network) + (1 if ch == "." else -1)) % len(nets)]
            self.index = 0
            self.load_chats()
        elif ch == "/" or k == "slash":
            box = self.query_one(Compose)
            box.placeholder, box.value = "filter chats (empty clears)", self.filter
            self._compose_mode = "filter"
            box.focus()
        else:
            return False
        self.refresh_all()
        return True

    def message_key(self, event: events.Key) -> bool:
        k, ch = event.key, event.character or ""
        n = len(self.messages)
        self.message = ""
        if k in ("up", "k") and n:
            self.msg_index = (n - 1 if self.msg_index < 0 else self.msg_index) - 1
            self.msg_index = max(0, self.msg_index)
        elif k in ("down", "j") and n:
            if self.msg_index >= 0:
                self.msg_index = -1 if self.msg_index + 1 >= n else self.msg_index + 1
        elif k in ("i", "tab", "enter"):
            self.start_compose()
        elif k in ("h", "left", "escape"):
            self.focus_list()
        elif k == "r" and n:
            self.reply_to = self._selected()
            self.start_compose()
        elif k == "e" and n:
            m = self._selected()
            if m and m.get("isSender"):
                self.editing = m
                self.start_compose(m.get("text") or "")
            else:
                self.message = "you can only edit your own messages"
        elif (k == "plus" or ch == "+") and n:
            self._react_menu()
        elif k == "m" and self.open:
            self._post(f"v1/chats/{self.open['id']}/unread", {})
            self.message = "marked unread"
        else:
            return False
        self.refresh_all()
        return True

    def scroll_chats(self, step: int) -> None:
        if self.chats:
            self.index = max(0, min(len(self.chats) - 1, self.index + step))
            self.refresh_all()

    def scroll_messages(self, step: int) -> None:
        """The wheel: up walks back through the messages, down comes forward (past the newest: follow new ones)."""
        n = len(self.messages)
        if not n:
            return
        cur = n - 1 if self.msg_index < 0 else self.msg_index
        cur += step
        self.msg_index = -1 if cur >= n - 1 and step > 0 else max(0, min(n - 1, cur))
        self.refresh_all()

    def _selected(self) -> dict | None:
        if not self.messages:
            return None
        return self.messages[self.msg_index if self.msg_index >= 0 else -1]

    def _react_menu(self) -> None:
        box = self.query_one(Compose)
        box.placeholder = "react: " + "  ".join(f"{i + 1} {e}" for i, e in enumerate(REACTIONS)) + "  (or type one)"
        box.value = ""
        self._compose_mode = "react"
        box.focus()

    # --- compose ---------------------------------------------------------------------------------

    def start_compose(self, value: str = "") -> None:
        if not self.open:
            return
        if self.open.get("isReadOnly"):
            self.message = "you can't write in this chat"
            self.refresh_all()
            return
        box = self.query_one(Compose)
        box.placeholder = f"message {self.open.get('title') or ''} · enter sends · esc"
        box.value = value
        self._compose_mode = "edit" if self.editing else "send"
        box.focus()

    def stop_compose(self) -> None:
        self.reply_to = self.editing = None
        box = self.query_one(Compose)
        box.value = ""
        box.placeholder = "i or tab: write · enter sends · esc"
        self._compose_mode = ""
        (self.query_one(Conversation) if self.open else self.query_one(ChatList)).focus()
        self.refresh_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "inbox-compose":
            return
        event.stop()
        text, mode = event.value.strip(), getattr(self, "_compose_mode", "send")
        if mode == "filter":
            self.filter = text
            self.index = 0
            self.load_chats()
            self.stop_compose()
            self.focus_list()
            return
        if mode == "react":
            key = REACTIONS[int(text) - 1] if text.isdigit() and 1 <= int(text) <= len(REACTIONS) else text
            m = self._selected()
            if key and m:
                self._post(f"v1/chats/{self.open['id']}/messages/{m['id']}/reactions", {"reactionKey": key},
                           then_reload=True)
            self.stop_compose()
            return
        if not text or not self.open:
            self.stop_compose()
            return
        if mode == "edit" and self.editing:
            self._post(f"v1/chats/{self.open['id']}/messages/{self.editing['id']}", {"text": text},
                       method="PUT", then_reload=True)
        else:
            body = {"text": text}
            if self.reply_to:
                body["replyToMessageID"] = self.reply_to["id"]
            self.messages.append({"id": "pending", "isSender": True, "text": text, "timestamp":
                                  datetime.now(timezone.utc).isoformat(), "sendStatus": {"status": "sending"}})
            self.msg_index = -1
            self._post(f"v1/chats/{self.open['id']}/messages", body, then_reload=True)
        self.reply_to = self.editing = None
        event.input.value = ""
        self.refresh_all()
