"""A Plex Media Server that answers like the real one, including its quirks.

Canned responses, served over real HTTP on localhost, so `plex.py` is exercised
through httpx rather than mocked away. The quirks are the point: the responses
here reproduce the behaviour that cost debugging rounds against the live
server, so a regression in the client is caught here rather than in front of a
DVR.

Reproduced faithfully:
  - a bulk section listing carries Genre but NOT Team
  - per-programme metadata carries Team
  - a sports programme that has no Team array at all, which is most of them
  - `/butler` is NOT wrapped in a MediaContainer, unlike everything else
  - a guid that arrives percent-encoded twice is answered 400
  - a create returns the new subscription key in its body
  - `oneShot` comes back as the string 'true'
  - `mediaIndex` is a string
  - a subscription body carries Directory for a series, Video for an event
"""
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

PROVIDER = "tv.plex.providers.epg.cloud:5"

# One live game and its repeat, on two channels, plus an ordinary episode and
# a DRM-locked airing. Enough to exercise every choice the app makes.
GAME_GUID = "plex://episode/game1"
EPISODE_GUID = "plex://episode/ep1"
DRM_GUID = "plex://episode/drm1"
NO_TEAM_GUID = "plex://episode/shop1"

CHANNELS = {
    "41.1": ("KQGGDT", "41.1 KQGGDT (NBC)", "id-41-1"),
    "38.1": ("WQFFDT", "38.1 WQFFDT (Independent)", "id-38-1"),
    "9.1": ("WQDDDT", "9.1 WQDDDT (ABC)", "id-9-1"),
    "5.1": ("KQCCDT", "5.1 KQCCDT (CBS)", "id-5-1"),
}

# The guide has to sit inside the app's own horizons: `passes._future` looks
# thirty days ahead, and the grid draws around the current time. So the anchor
# is soon rather than a fixed far-future epoch, and every other time is derived
# from it, which keeps assertions exact without pinning them to a date.
#
# It is computed once, at import, so one run sees one guide.
LIVE_AT = int(os.environ.get("COUCHELEPHANT_FAKE_ANCHOR")
              or (int(time.time()) // 1800) * 1800 + 1800)
REPEAT_AT = LIVE_AT + 2 * 86400
EPISODE_AT = LIVE_AT + 3600
DRM_AT = LIVE_AT + 7200
NO_TEAM_AT = LIVE_AT + 10800


def _media(vcn, begins, premiere=False, drm=False, res="720"):
    cs, title, ident = CHANNELS[vcn]
    m = {
        "id": f"{vcn}-{begins}", "channelVcn": vcn, "channelCallSign": cs,
        "channelTitle": title, "channelIdentifier": ident,
        "beginsAt": begins, "endsAt": begins + 7200,
        "videoResolution": res, "protocol": "livetv",
        "channelThumb": "http://127.0.0.1:1/logo.png",
    }
    if premiere:
        m["premiere"] = "1"
    if drm:
        m["drm"] = True
    return m


SPORTS_ITEMS = [{
    "guid": GAME_GUID, "ratingKey": "plex%3A%2F%2Fepisode%2Fgame1",
    "title": "Chiefs at Buccaneers", "grandparentTitle": "NFL Football",
    "summary": "A game.", "type": "episode", "year": 2026,
    "duration": 7_200_000,
    # No contentRating on purpose. Most sport in a real guide carries none,
    # which is what makes blank handling worth testing.
    "Genre": [{"tag": "Football"}],
    # A bulk listing carries no Team. This is the quirk B1 depended on.
    "Media": [_media("41.1", LIVE_AT, premiere=True),
              _media("38.1", REPEAT_AT)],
}, {
    # Sport, but not a game: a studio show with no Team array anywhere. Plex
    # answers 200 and simply omits Team, so enrichment can never fill this row
    # in. Most of a real guide's sports section looks like this, which is why
    # an attempt has to be written down even when it finds nothing.
    "guid": NO_TEAM_GUID, "ratingKey": "plex%3A%2F%2Fepisode%2Fshop1",
    "title": "Football Fan Shop", "grandparentTitle": "Football Fan Shop",
    "summary": "Merchandise.", "type": "episode", "year": 2026,
    "duration": 1_800_000,
    "Genre": [{"tag": "Sports talk"}],
    "Media": [_media("38.1", NO_TEAM_AT)],
}]

SHOW_ITEMS = [{
    "guid": EPISODE_GUID, "ratingKey": "plex%3A%2F%2Fepisode%2Fep1",
    "title": "Quiz Night", "grandparentTitle": "Quiz Show",
    "summary": "An episode.", "type": "episode", "year": 2026,
    "contentRating": "TV-PG", "duration": 3_600_000,
    "Genre": [{"tag": "Game Show"}, {"tag": "Comedy"}],
    "Media": [_media("9.1", EPISODE_AT, premiere=True, res="1080")],
}, {
    "guid": DRM_GUID, "ratingKey": "plex%3A%2F%2Fepisode%2Fdrm1",
    "title": "Locked Broadcast", "grandparentTitle": "Locked",
    "summary": "Encrypted.", "type": "episode", "year": 2020,
    "contentRating": "TV-MA", "duration": 5_400_000,
    "Genre": [{"tag": "Drama"}],
    "Media": [_media("5.1", DRM_AT, premiere=True, drm=True)],
}]

TEAMS = [{"key": "236", "title": "Kansas City Chiefs"},
         {"key": "237", "title": "Tampa Bay Buccaneers"}]

TEAM_TAGS = {GAME_GUID: [{"id": 236, "tag": "Kansas City Chiefs"},
                         {"id": 237, "tag": "Tampa Bay Buccaneers"}]}

MOVIE_ITEMS = []

# A guide big enough to photograph. Tests want the smallest one that proves
# the behaviour; the README wants a screen with television on it. Same server,
# same shapes, more rows. See tests/demo_guide.py.
if os.environ.get("COUCHELEPHANT_DEMO_GUIDE") == "1":
    from tests import demo_guide as _demo

    CHANNELS = {vcn: (call, title, f"id-{vcn.replace('.', '-')}")
                for vcn, (call, title) in _demo.CHANNELS.items()}
    SPORTS_ITEMS, SHOW_ITEMS, MOVIE_ITEMS, TEAM_TAGS = _demo.build(LIVE_AT, _media)
    TEAMS = [{"key": str(i), "title": n} for i, n in _demo.TEAMS]


def _settings():
    """As the real server sends them, summary and all.

    Plex writes a `summary` for every setting. They matter here because the
    panel shows them, and one of them ("Detect commercials") is long enough
    that rendering it inline pushed a single row past six hundred pixels.
    """
    return [
        {"id": "minVideoQuality", "value": "0", "type": "int",
         "label": "Resolution", "enumValues": "0:Prefer HD|720:HD only",
         "summary": "Choose the minimum resolution for airings to be recorded."},
        {"id": "replaceLowerQuality", "value": "false", "type": "bool",
         "label": "Replace lower resolution items",
         "summary": "Set whether items in your library may be replaced by higher "
                    "resolution recordings."},
        {"id": "recordPartials", "value": "true", "type": "bool",
         "label": "Allow partial airings",
         "summary": "Choose whether a recording may begin for an airing already "
                    "in progress."},
        {"id": "startOffsetMinutes", "value": "0", "type": "int",
         "label": "Minutes before start",
         "summary": "Increase the recording duration by adding minutes before "
                    "the scheduled time."},
        {"id": "endOffsetMinutes", "value": "0", "type": "int",
         "label": "Minutes after end",
         # No list of allowed values, so the field takes any number.
         "summary": "Increase the recording duration by adding minutes after "
                    "the scheduled time."},
        {"id": "comskipMethod", "value": "0", "type": "int",
         "label": "Detect commercials",
         # A dropdown, and a wide one. The fake used to send this as a plain
         # number, so the option grid was easier here than on a real server
         # and a truncated label passed the test.
         "enumValues": "0:Disabled|1:Detect and delete commercials"
                       "|2:Detect commercials and mark for skip",
         # The long one. Inline it was twenty lines.
         "summary": "Attempt to automatically detect and remove commercials from "
                    "recordings. This process may take a long time and cause high "
                    "CPU usage. 'Detect and delete commercials' will delete "
                    "detected commercial footage from your video files."},
        {"id": "lineupChannel", "value": "", "type": "text",
         "label": "Limit to channel",
         "enumValues": ":Any|id-41-1:41.1 KQGGDT (NBC)"},
        {"id": "startTimeslot", "value": "-1", "type": "int",
         "label": "Limit to airing time",
         # URL encoded inside the enum, as Plex really sends it.
         "enumValues": f"-1:Any|{LIVE_AT}:07%3A00 PM"},
        # A recurring rule can honour these. A one-shot booking cannot, so a
        # pass must not be offered them.
        {"id": "onlyNewAirings", "value": "1", "type": "int", "label": "Airings"},
        {"id": "autoDeletionItemPolicyWatchedLibrary", "value": "0", "type": "int",
         "label": "Delete episodes after playing"},
        # No label: plumbing, and must stay hidden.
        {"id": "oneShot", "value": "false", "type": "bool", "label": ""},
        {"id": "comskipEnabled", "value": "-1", "type": "int", "label": ""},
    ]


class State:
    """What the fake server remembers, so tests can assert on it."""

    def __init__(self):
        self.subscriptions = {}
        self.next_key = 100
        self.created = []          # every create, with its parsed parameters
        self.deleted = []
        self.metadata_calls = 0
        self.drop_next_create = False   # make Plex discard what it just made
        self.seen_urls = []        # every request line, to check what leaks
        # What the DVR reports about its own upkeep. A health check reads
        # these, so a test drives them directly rather than waiting a week.
        self.refreshed_at = None        # None means "as of now"
        self.epg_task_enabled = True
        self.epg_task_interval = 1
        self.serve_butler = True        # a server too old to have /butler
        self.reset()

    def reset(self):
        self.subscriptions = {}
        self.next_key = 100
        self.created = []
        self.deleted = []
        self.metadata_calls = 0
        self.drop_next_create = False
        self.seen_urls = []
        self.refreshed_at = None
        self.epg_task_enabled = True
        self.epg_task_interval = 1
        self.serve_butler = True


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _container(self, **kw):
        self._send({"MediaContainer": dict(kw)})

    def do_GET(self):
        STATE.seen_urls.append(self.path)
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path

        if p == "/":
            return self._container(friendlyName="fakeplex", version="1.0.0-test")

        if p == "/butler":
            if not STATE.serve_butler:
                return self._send({"error": "no such route"}, 404)
            # The trap: this one is NOT a MediaContainer. Unwrapping it the
            # usual way gives an empty dict, which a health check would read
            # as "Plex has no scheduled tasks" and act on.
            return self._send({"ButlerTasks": {"ButlerTask": [
                {"name": "BackupDatabase", "interval": 3, "enabled": True},
                {"name": "RefreshEpgGuides",
                 "interval": STATE.epg_task_interval,
                 "enabled": STATE.epg_task_enabled},
                {"name": "RefreshLibraries", "interval": 1, "enabled": True},
            ]}})

        if p == "/livetv/dvrs":
            return self._container(Dvr=[{
                "key": "5", "epgIdentifier": PROVIDER,
                "lineupTitle": "Test Lineup",
                "refreshedAt": (STATE.refreshed_at
                                if STATE.refreshed_at is not None
                                else int(time.time())),
                "Device": [{"key": "1", "ChannelMapping": [
                    {"deviceIdentifier": v, "channelKey": c[2]}
                    for v, c in CHANNELS.items()]}],
            }])

        if p == f"/{PROVIDER}/sections":
            return self._container(Directory=[
                {"key": "1", "title": "Shows"},
                {"key": "2", "title": "Movies"},
                {"key": "3", "title": "Sports"}])

        if p.startswith(f"/{PROVIDER}/sections/") and p.endswith("/all"):
            section = p.split("/")[-2]
            itype = (q.get("type") or ["4"])[0]
            # Querying a section with the wrong type returns nothing rather
            # than erroring, exactly as the real server does.
            if section == "3" and itype == "4":
                return self._container(Metadata=SPORTS_ITEMS)
            if section == "1" and itype == "4":
                return self._container(Metadata=SHOW_ITEMS)
            if section == "2" and itype == "1":
                return self._container(Metadata=MOVIE_ITEMS)
            return self._container(Metadata=[])

        if p.endswith("/team"):
            return self._container(Directory=TEAMS)

        if p.startswith(f"/{PROVIDER}/metadata/"):
            STATE.metadata_calls += 1
            key = urllib.parse.unquote(p.rsplit("/", 1)[-1])
            item = None
            for src in (SPORTS_ITEMS, SHOW_ITEMS, MOVIE_ITEMS):
                for m in src:
                    if urllib.parse.unquote(m["ratingKey"]) == key:
                        item = dict(m)
            if not item:
                return self._send({"error": "no such item"}, 404)
            tags = TEAM_TAGS.get(item["guid"])
            if tags:
                item["Team"] = tags
            return self._container(Metadata=[item])

        if p == "/media/subscriptions/template":
            # A Plex server in another language localizes these titles. The
            # app must not care, so the suite can ask for German ones.
            de = os.environ.get("COUCHELEPHANT_FAKE_LANG") == "de"

            guid = (q.get("guid") or [""])[0]
            # The trap: a guid that was already percent-encoded and then
            # encoded again by the client. The real server answers 400.
            if "%3A" in guid or "%2F" in guid:
                return self._send({"error": "bad guid"}, 400)
            if not guid.startswith("plex://"):
                return self._send({"error": "bad guid"}, 400)
            g = urllib.parse.quote(guid, safe='')
            tags = TEAM_TAGS.get(guid)
            if tags:
                # A game, in the real server's order: the single event, then
                # the whole league, then one rule per team. The league sitting
                # in front of the team is what once turned "follow the
                # Chiefs" into "record every NFL game".
                league = next((m.get("grandparentTitle") for src in (SPORTS_ITEMS,)
                               for m in src if m["guid"] == guid), "Sport")
                subs = [{"title": "Diese Sendung" if de else "This Event",
                         "type": 4, "targetLibrarySectionID": 2,
                         "parameters": f"hints%5Bguid%5D={g}", "Setting": _settings()},
                        {"title": (f"Alle {league}-Sendungen" if de
                                   else f"All {league} Events"), "type": 2,
                         "targetLibrarySectionID": 2,
                         "parameters": f"hints%5Bguid%5D={g}&hints%5Bleague%5D=1",
                         "Setting": _settings()}]
                for t in tags:
                    # A real server answers 15 for a team, not 2. Only 4 means
                    # one broadcast; every other type recurs.
                    subs.append({"title": (f"Alle {t['tag']}-Sendungen" if de
                                          else f"All {t['tag']} Events"), "type": 15,
                                 "targetLibrarySectionID": 2,
                                 "parameters": f"hints%5Bguid%5D={g}&hints%5Bteam%5D={t['id']}",
                                 "Setting": _settings()})
                return self._container(SubscriptionTemplate=[{"MediaSubscription": subs}])
            return self._container(SubscriptionTemplate=[{
                "MediaSubscription": [
                    {"title": "Diese Folge" if de else "This Episode", "type": 4,
                     "targetLibrarySectionID": 2,
                     "parameters": f"hints%5Bguid%5D={g}",
                     "Setting": _settings()},
                    {"title": "Alle Folgen" if de else "All Episodes", "type": 2,
                     "targetLibrarySectionID": 2,
                     "parameters": f"hints%5Bguid%5D={g}",
                     "Setting": _settings()},
                ]}])

        if p == "/media/subscriptions":
            return self._container(
                size=len(STATE.subscriptions),
                MediaSubscription=list(STATE.subscriptions.values()))

        if p == "/media/subscriptions/scheduled":
            ops = []
            for key, sub in STATE.subscriptions.items():
                ops.append({
                    "id": f"op-{key}",
                    "mediaSubscriptionID": key,
                    # A string, as the real server sends it.
                    "mediaIndex": "0",
                    "status": "scheduled",
                    "Metadata": sub["_meta"],
                })
            return self._container(MediaGrabOperation=ops)

        if p.startswith("/media/subscriptions/"):
            key = p.rsplit("/", 1)[-1]
            sub = STATE.subscriptions.get(key)
            if not sub:
                return self._send({"error": "gone"}, 404)
            return self._container(MediaSubscription=[sub])

        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        STATE.seen_urls.append(self.path)
        u = urllib.parse.urlsplit(self.path)
        if u.path != "/media/subscriptions":
            return self._send({"error": "not found"}, 404)
        q = urllib.parse.parse_qs(u.query, keep_blank_values=True)
        prefs = {k[6:-1]: v[0] for k, v in q.items()
                 if k.startswith("prefs[") and k.endswith("]")}
        guid = (q.get("hints[guid]") or [""])[0]
        one_shot = prefs.get("oneShot") in ("1", "true")

        STATE.created.append({"guid": guid, "prefs": prefs,
                              "type": (q.get("type") or [""])[0],
                              "section": (q.get("targetLibrarySectionID") or [""])[0]})

        key = str(STATE.next_key)
        STATE.next_key += 1

        meta = None
        for src in (SPORTS_ITEMS, SHOW_ITEMS, MOVIE_ITEMS):
            for m in src:
                if m["guid"] == guid:
                    meta = m
        media = list((meta or {}).get("Media") or [])
        pinned = prefs.get("startTimeslot")
        if pinned and pinned != "-1":
            media = [m for m in media if str(m["beginsAt"]) == str(pinned)] or media

        # The reply is titled in the server's own language, like a real one.
        de = os.environ.get("COUCHELEPHANT_FAKE_LANG") == "de"
        every = (lambda w: f"Alle {w}-Sendungen") if de else (lambda w: f"All {w} Events")
        if q.get("hints[league]"):
            title = every((meta or {}).get("grandparentTitle"))
        elif q.get("hints[team]"):
            names = {str(t["id"]): t["tag"] for t in TEAM_TAGS.get(guid, [])}
            title = every(names.get(q["hints[team]"][0], "?"))
        elif one_shot:
            sport = meta in SPORTS_ITEMS
            title = (("Diese Sendung" if sport else "Diese Folge") if de
                     else ("This Event" if sport else "This Episode"))
        else:
            title = "Alle Folgen" if de else "All Episodes"
        sub = {
            "key": key, "type": 4 if one_shot else 2,
            "targetLibrarySectionID": 2, "title": title,
            # oneShot comes back as a string, not a 1.
            "Setting": [{"id": k, "value": ("true" if v in ("1", "true") else v)}
                        for k, v in prefs.items()],
            "_meta": {"guid": guid,
                      "title": (meta or {}).get("title"),
                      "grandparentTitle": (meta or {}).get("grandparentTitle"),
                      "Media": media},
        }
        # A series rule carries Directory; a single event carries Video.
        if one_shot:
            sub["Video"] = {"title": (meta or {}).get("title"),
                            "grandparentTitle": (meta or {}).get("grandparentTitle")}
        else:
            sub["Directory"] = {"title": (meta or {}).get("grandparentTitle")}

        if STATE.drop_next_create:
            # Plex answers 200 with a key and then discards the subscription.
            STATE.drop_next_create = False
        else:
            STATE.subscriptions[key] = sub
        return self._container(size=1, MediaSubscription=[sub])

    def do_DELETE(self):
        key = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
        if key in STATE.subscriptions:
            del STATE.subscriptions[key]
            STATE.deleted.append(key)
            return self._container(size=0)
        return self._send({"error": "gone"}, 404)


def start():
    """Run the fake server on a free localhost port. Returns (url, stop)."""
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    return url, srv.shutdown
