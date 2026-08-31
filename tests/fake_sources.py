"""The outside sources, answering the way the real ones do.

Served over real HTTP on localhost so the provider modules are exercised
through httpx rather than mocked away. The shapes are copied from real
responses, including the parts that matter:

  - TVmaze gives a month-only premiere date for a show with no announced day
  - TVmaze names a network that may be a streaming service no aerial can reach
  - TheSportsDB omits strTime for a game whose kickoff is not set yet
  - TMDB answers nothing without a key

No test may reach a third party, so every provider is pointed here.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

TVMAZE_HITS = {
    "gobiligook": [{
        "score": 0.9,
        "show": {"id": 99999, "name": "Gobiligook", "premiered": "2027-03",
                 "status": "In Development",
                 "network": {"name": "ABC"}, "webChannel": None},
    }],
    "quorbis": [{
        "score": 0.4,
        "show": {"id": 99998, "name": "Quorbis The Series",
                 "premiered": "2027-05-21", "status": "In Development",
                 "network": None, "webChannel": {"name": "A Streamer"}},
    }],
}

SPORTSDB_EVENTS = [
    {"idEvent": "2000001", "strEvent": "Ravens vs Falcons",
     "strHomeTeam": "Ravens", "strAwayTeam": "Falcons",
     "dateEvent": "2027-01-10", "strTime": "20:15:00", "strTVStation": None},
    {"idEvent": "2000002", "strEvent": "Ravens vs Pilots",
     "strHomeTeam": "Ravens", "strAwayTeam": "Pilots",
     "dateEvent": "2027-01-17", "strTime": None, "strTVStation": None},
    {"idEvent": "2000003", "strEvent": "Falcons vs Pilots",
     "strHomeTeam": "Falcons", "strAwayTeam": "Pilots",
     "dateEvent": "2027-01-24", "strTime": "13:00:00", "strTVStation": None},
]

TMDB_HITS = [{"id": 55555, "title": "Quorbis Rising",
              "release_date": "2027-05-21", "overview": ""}]

_server = None
_thread = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = urlparse(self.path)
        params = parse_qs(parts.query)
        term = (params.get("q") or params.get("query") or [""])[0].strip().lower()
        if parts.path == "/search/shows":
            self._send(TVMAZE_HITS.get(term, []))
        elif parts.path.endswith("/eventsseason.php"):
            self._send({"events": SPORTSDB_EVENTS})
        elif parts.path == "/3/search/movie":
            self._send({"results": TMDB_HITS if term else []})
        else:
            self.send_response(404)
            self.end_headers()


def start() -> str:
    global _server, _thread
    _server = HTTPServer(("127.0.0.1", 0), _Handler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    return f"http://127.0.0.1:{_server.server_address[1]}"


def stop() -> None:
    if _server is not None:
        _server.shutdown()
