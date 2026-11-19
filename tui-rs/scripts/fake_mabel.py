#!/usr/bin/env python3
"""A pretend Mabel for trying out MABEL-OS changes safely (scripts/sandbox.sh starts it).

It answers the parts of Mabel's HTTP API that MABEL-OS uses, with harmless canned data:
chats (she replies "(fake Mabel) …"), sessions, history, her day (LIFE), the tape repo
(kept in memory), and empty music. Nothing reaches the real Mabel, your chats or your files.

    python3 fake_mabel.py [port]         default port 8797

Every chat message it receives is appended to fake_mabel_messages.jsonl in the current
folder, so you can see exactly what MABEL-OS sent. Stop it (ctrl+c) to test "Mabel is down".
"""
import json
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8797
SESSIONS: dict[str, dict] = {}   # id → {"name": …, "history": [...]}
TAPES: dict[str, bytes] = {}     # name → .dtape bytes


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def reply(self, obj, code=200, kind="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def form(self) -> dict:
        raw = self.body().decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    # ── reading ──────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/sessions":
            items = [{"id": k, "name": v["name"], "updated_at": now()} for k, v in SESSIONS.items()]
            return self.reply({"sessions": items})
        if path.startswith("/api/history/"):
            sid = path.rsplit("/", 1)[1]
            return self.reply({"history": SESSIONS.get(sid, {}).get("history", [])})
        if path == "/api/tui/life":
            return self.reply({"state": "resting", "doing": ["pretending to be Mabel"], "journal": [], "lists": {}})
        if path == "/api/tapes":
            tapes = [{"name": n, "title": n, "author": "", "description": "(on the fake Mabel)", "screen": "fill",
                      "fps": 30, "size": len(b), "sha": f"{hash(b) & 0xffffffffffffffff:016x}", "published": 0}
                     for n, b in TAPES.items()]
            return self.reply({"tapes": tapes})
        if path.startswith("/api/tapes/") and path.endswith(".dtape"):
            name = path[len("/api/tapes/"):-len(".dtape")]
            if name in TAPES:
                return self.reply(TAPES[name], kind="application/x-tar")
            return self.reply({"detail": "no such tape"}, 404)
        if path.startswith("/api/beeper"):
            return self.reply({"detail": "Beeper isn't part of the fake Mabel"}, 503)
        if path == "/tui/version":
            return self.reply({"build": "rust", "rs": {}})
        return self.reply({})  # music and everything else: empty

    # ── writing ──────────────────────────────────────────────────────────
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/session":
            form = self.form()
            sid = f"fake-{len(SESSIONS) + 1}"
            SESSIONS[sid] = {"name": form.get("name", "chat"), "history": []}
            return self.reply({"id": sid, "name": SESSIONS[sid]["name"]})
        if path == "/api/chat_stream":
            form = self.form()
            sid, message = form.get("session", ""), form.get("message", "")
            with open("fake_mabel_messages.jsonl", "a") as log:
                log.write(json.dumps({"at": now(), "session": sid, "message": message}) + "\n")
            question = message.split("=== His question ===")[-1].strip()[:120]
            answer = f"(fake Mabel) I got your message: “{question}”"
            SESSIONS.setdefault(sid, {"name": "chat", "history": []})["history"] += [
                {"role": "user", "content": message}, {"role": "assistant", "content": answer}]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in answer.split(" "):
                self.wfile.write(f"data: {json.dumps({'delta': word + ' '})}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.03)
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self.body()
        return self.reply({})

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/tapes/"):
            name = path[len("/api/tapes/"):]
            TAPES[name] = self.body()
            return self.reply({"name": name, "title": name, "size": len(TAPES[name]), "sha": "fake"})
        self.body()
        return self.reply({})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/tapes/"):
            TAPES.pop(path[len("/api/tapes/"):], None)
        return self.reply({})


if __name__ == "__main__":
    print(f"fake Mabel listening on http://127.0.0.1:{PORT} (ctrl+c stops her)", file=sys.stderr)
    ThreadingHTTPServer(("127.0.0.1", PORT), Fake).serve_forever()
