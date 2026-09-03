"""Discord and Telegram, answering the way the real ones do.

Served over real HTTP on localhost so `app/notify.py` is exercised through httpx
rather than mocked away. The shapes are copied from the real APIs, including the
parts that matter:

  - Discord answers a webhook POST with 204 and an empty body, not 200 and JSON
  - Discord answers 401 for a webhook that has been deleted, which is what a
    revoked URL looks like and the one failure a user will actually hit
  - Telegram answers 200 with `{"ok": false, ...}` for a bad token, so an HTTP
    status alone never proves a message was delivered
  - Telegram's getUpdates returns an empty list until somebody messages the bot

**No test may reach Discord or Telegram.** Every send is pointed here.

`SENT` is the record of what arrived, in order, and is what the assertions read.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

_server = None
_thread = None

# Everything that arrived: (platform, path, decoded body).
SENT: list[tuple[str, str, dict]] = []

# Flip these from a test to make the far side misbehave.
DISCORD_STATUS = 204
TELEGRAM_OK = True
# What getUpdates hands back. Empty is the honest default: a bot nobody has
# messaged has no chat id to find.
TELEGRAM_UPDATES: list[dict] = []

# A token whose path segment is this is treated as revoked.
BAD_TOKEN = "000:revoked"


def reset() -> None:
    global DISCORD_STATUS, TELEGRAM_OK, TELEGRAM_UPDATES
    SENT.clear()
    DISCORD_STATUS = 204
    TELEGRAM_OK = True
    TELEGRAM_UPDATES = []


def discord_sent() -> list[dict]:
    return [b for p, _, b in SENT if p == "discord"]


def telegram_sent() -> list[dict]:
    return [b for p, path, b in SENT if p == "telegram" and path.endswith("/sendMessage")]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _empty(self, status):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode() or "{}")
        except ValueError:
            return {}

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        # Discord: /api/webhooks/<id>/<token>
        if path.startswith("/api/webhooks/"):
            SENT.append(("discord", path, body))
            if DISCORD_STATUS >= 400:
                self._json(DISCORD_STATUS, {"message": "Unknown Webhook", "code": 10015})
            else:
                self._empty(DISCORD_STATUS)
            return
        # Telegram: /bot<token>/<method>
        if path.startswith("/bot"):
            token = path[4:].split("/")[0]
            SENT.append(("telegram", path, body))
            if token == BAD_TOKEN:
                self._json(200, {"ok": False, "error_code": 401,
                                 "description": "Unauthorized"})
                return
            if path.endswith("/getUpdates"):
                self._json(200, {"ok": True, "result": TELEGRAM_UPDATES})
                return
            if not TELEGRAM_OK:
                self._json(200, {"ok": False, "error_code": 400,
                                 "description": "chat not found"})
                return
            self._json(200, {"ok": True, "result": {"message_id": len(SENT)}})
            return
        self._empty(404)

    # Telegram accepts GET for every method too, and getUpdates is the one a
    # human is most likely to reach for by hand.
    do_GET = do_POST


def start() -> str:
    global _server, _thread
    reset()
    _server = HTTPServer(("127.0.0.1", 0), _Handler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    return f"http://127.0.0.1:{_server.server_address[1]}"


def stop() -> None:
    if _server is not None:
        _server.shutdown()
