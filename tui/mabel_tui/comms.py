"""COMMS: chat with Mabel. Sessions on the left (ctrl+s), the conversation, the prompt at the bottom.

Her reply streams in as Markdown. Tool calls show as one dim line each; an "Allow this action?"
card gets Allow once / Deny buttons that resume the turn. control_music commands play locally.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, ListItem, ListView, Markdown, Static

from .nav import mark


def _stamp(value: str | None = None) -> str:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return datetime.now().strftime("%H:%M")


class Message(Vertical):
    def __init__(self, role: str, text: str = "", when: str | None = None):
        super().__init__(classes=f"msg -{role}")
        self.role, self.text, self.when = role, text, when

    def compose(self) -> ComposeResult:
        who = "◢ YOU" if self.role == "user" else "◣ MABEL"
        yield Static(f"{who}  {_stamp(self.when)}", classes="msg-head")
        yield Markdown(self.text)

    @property
    def body(self) -> Markdown:
        return self.query_one(Markdown)


class Approval(Vertical):
    def __init__(self, card: dict):
        super().__init__(classes="approval")
        self.card = card

    def compose(self) -> ComposeResult:
        action = self.card.get("action") or {}
        what = f"{action.get('tool', '?')}: {str(action.get('content', ''))[:200]}"
        yield Static(f"▲ {self.card.get('question') or 'Allow this action?'}", classes="warn")
        yield Static(what, classes="dim")
        with Horizontal():
            yield Button("Allow once", id="allow", variant="primary")
            yield Button("Deny", id="deny", variant="error")


class SessionItem(ListItem):
    def __init__(self, session: dict):
        super().__init__(Label(f"{(session.get('name') or 'untitled')[:26]}"))
        self.session = session


class Comms(Horizontal):
    BINDINGS = [
        Binding("ctrl+s", "toggle_sessions", "sessions"),
        Binding("ctrl+n", "new_session", "new chat"),
    ]

    def __init__(self, **kw):
        super().__init__(id="comms-view", **kw)
        self.session: dict | None = None
        self.busy = False

    def compose(self) -> ComposeResult:
        sessions = mark(ListView(id="sessions", classes="panel"))
        sessions.border_title = "CHANNELS"
        yield sessions
        with Vertical(id="chat-col"):
            log = mark(VerticalScroll(id="log", classes="panel"))
            log.border_title = "COMMS"
            yield log
            yield mark(Input(placeholder="talk to Mabel…", id="prompt"))

    @property
    def mabel(self):
        return self.app.mabel

    def on_mount(self) -> None:
        self.load_sessions(select_first=True)

    # ── sessions ────────────────────────────────────────────────────────

    @work(exclusive=True, group="sessions")
    async def load_sessions(self, select_first: bool = False) -> None:
        try:
            sessions = await self.mabel.sessions()
        except Exception as exc:
            self.app.notify(f"Couldn't load chats: {exc}", severity="error")
            return
        view = self.query_one("#sessions", ListView)
        await view.clear()
        await view.extend(SessionItem(s) for s in sessions[:80])
        if select_first and sessions and self.session is None:
            self.open_session(sessions[0])

    @on(ListView.Selected, "#sessions")
    def picked(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionItem):
            self.open_session(event.item.session)

    @work(exclusive=True, group="open")
    async def open_session(self, session: dict) -> None:
        self.session = session
        log = self.query_one("#log", VerticalScroll)
        log.border_title = f"COMMS ░ {(session.get('name') or 'untitled').upper()[:40]}"
        await log.remove_children()
        try:
            history = await self.mabel.history(session["id"])
        except Exception as exc:
            self.app.notify(f"Couldn't open the chat: {exc}", severity="error")
            return
        await log.mount_all(Message(m["role"], m.get("content") or "", (m.get("metadata") or {}).get("timestamp"))
                            for m in history[-60:])
        log.scroll_end(animate=False)
        if self.app.on_channel("comms"):
            self.query_one("#prompt", Input).focus()

    def action_toggle_sessions(self) -> None:
        # Wide (PC): shown unless hidden. Narrow (the deck): hidden unless shown.
        self.query_one("#sessions").toggle_class("-shown" if self.screen.has_class("-narrow") else "-hidden")

    def action_focus_prompt(self) -> None:
        self.query_one("#prompt", Input).focus()

    @work(exclusive=True, group="open")
    async def action_new_session(self) -> None:
        try:
            created = await self.mabel.new_session(f"Terminal {datetime.now():%d %b %H:%M}", like=self.session)
        except Exception as exc:
            self.app.notify(f"Couldn't start a chat: {exc}", severity="error")
            return
        self.session = created
        log = self.query_one("#log", VerticalScroll)
        log.border_title = "COMMS ░ NEW CHANNEL"
        await log.remove_children()
        self.load_sessions()
        self.query_one("#prompt", Input).focus()

    # ── talking ─────────────────────────────────────────────────────────

    @on(Input.Submitted, "#prompt")
    def send(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or self.busy:
            return
        if self.session is None:
            self.app.notify("No chat open: ctrl+n starts one", severity="warning")
            return
        event.input.value = ""
        note = getattr(self, "pending_note", None)
        if note:
            self.pending_note = None
            event.input.placeholder = "talk to Mabel…"
            name, rel, content = note
            full = f"About my note “{name}” ({rel}):\n\n{content[:8000]}\n\n---\n{text}"
            self.run_turn(full, shown=f"[about “{name}”] {text}")
            return
        self.run_turn(text)

    @work(exclusive=True, group="turn")
    async def run_turn(self, text: str, approval: tuple[str, str] | None = None, shown: str | None = None) -> None:
        log = self.query_one("#log", VerticalScroll)
        self.busy = True
        self.app.set_status("◉ MABEL IS TYPING")
        if text:
            await log.mount(Message("user", shown or text))
        reply = Message("assistant", "")
        await log.mount(reply)
        log.scroll_end(animate=False)
        stream = Markdown.get_stream(reply.body)
        got_text = False
        try:
            async for event in self.mabel.stream(self.session["id"], text, approval=approval):
                kind = event.get("type")
                if "delta" in event and not kind:
                    got_text = True
                    await stream.write(event["delta"])
                    log.scroll_end(animate=False)
                elif kind == "final_response":
                    await stream.stop()
                    stream = None
                    await reply.body.update(event.get("content") or "")
                    got_text = True
                elif kind == "tool_start":
                    await log.mount(Static(f"▸ {event.get('tool') or event.get('name') or 'tool'}", classes="tool-line"),
                                    before=reply)
                elif kind == "tool_output" and isinstance(event.get("ask_user"), dict) \
                        and event["ask_user"].get("approval_id"):
                    await log.mount(Approval(event["ask_user"]))
                elif kind == "ui_control":
                    command = (event.get("data") or {}).get("music_command")
                    if isinstance(command, dict):
                        asyncio.create_task(self.app.player.apply(command))
                        self.app.notify("♪ Mabel put something on", timeout=3)
                elif kind == "error":
                    await log.mount(Static(f"✖ {event.get('error')}", classes="warn"))
        except Exception as exc:
            await log.mount(Static(f"✖ lost the line to Mabel: {exc}", classes="warn"))
        finally:
            if stream is not None:
                await stream.stop()
            if not got_text:
                await reply.remove()
            self.busy = False
            self.app.set_status("")
            log.scroll_end(animate=False)

    @on(Button.Pressed, ".approval Button")
    def decide(self, event: Button.Pressed) -> None:
        card = event.button.ancestors_with_self
        approval = next((a for a in card if isinstance(a, Approval)), None)
        if approval is None or self.busy:
            return
        decision = "approve_once" if event.button.id == "allow" else "deny"
        approval.remove()
        self.run_turn("", approval=(approval.card["approval_id"], decision))
