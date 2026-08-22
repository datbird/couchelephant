"""Plex Media Server client, scoped to Live TV and the DVR.

Plex's API is documented at developer.plex.tv but the server's real paths are
older and differ from the docs, so the paths here are the ones the web client
actually calls and that were verified against a live server.

Two quirks worth knowing:
  - The EPG provider is addressed as `tv.plex.providers.epg.cloud:<dvrKey>`.
  - Creating a recording needs the template's own `parameters` string PLUS
    targetLibrarySectionID and type. Posting the template params alone is a 400.
"""
import time
import urllib.parse

import httpx

TIMEOUT = httpx.Timeout(120.0, connect=15.0)


class PlexError(RuntimeError):
    pass


class Plex:
    def __init__(self, base_url: str, token: str):
        if not base_url:
            raise PlexError("no Plex server URL configured")
        if not token:
            raise PlexError("no Plex token configured")
        self.base = base_url.rstrip("/")
        self.token = token

    def _client(self):
        return httpx.Client(timeout=TIMEOUT, headers={"Accept": "application/json"})

    def _get(self, path, params=None):
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        with self._client() as c:
            r = c.get(self.base + path, params=params)
        if r.status_code >= 400:
            raise PlexError(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json().get("MediaContainer", {})
        except Exception:
            raise PlexError(f"GET {path} returned non-JSON: {r.text[:200]}")

    # ---------- identity ----------

    def server_info(self):
        return self._get("/")

    # ---------- DVR ----------

    def dvrs(self):
        return self._get("/livetv/dvrs").get("Dvr", []) or []

    def grabber_devices(self):
        return self._get("/media/grabbers/devices").get("Device", []) or []

    def epg_sections(self, provider):
        return self._get(f"/{provider}/sections").get("Directory", []) or []

    def section_all(self, provider, section, **filters):
        """Everything in an EPG section. `type=4` means airings (episodes)."""
        return self._get(f"/{provider}/sections/{section}/all", filters).get("Metadata", []) or []

    def section_search(self, provider, section, query, type_=4):
        return self._get(
            f"/{provider}/sections/{section}/search", {"query": query, "type": type_}
        ).get("Metadata", []) or []

    def metadata(self, provider, rating_key):
        """Full metadata for one programme.

        The Team array only exists here. A bulk `/sections/N/all` listing
        returns Genre but NOT Team, which is why sports rows need enriching
        one at a time.
        """
        items = self._get(f"/{provider}/metadata/{rating_key}").get("Metadata", []) or []
        return items[0] if items else None

    def teams(self, provider, section):
        """Every team the guide knows about, with the ids used by `?team=<id>`."""
        return self._get(f"/{provider}/sections/{section}/team").get("Directory", []) or []

    def by_team(self, provider, section, team_id):
        return self.section_all(provider, section, type=4, team=team_id)

    # ---------- recordings ----------

    def subscriptions(self):
        return self._get("/media/subscriptions").get("MediaSubscription", []) or []

    def subscription(self, key):
        subs = self._get(f"/media/subscriptions/{key}").get("MediaSubscription", []) or []
        return subs[0] if subs else None

    def scheduled(self):
        return self._get("/media/subscriptions/scheduled").get("MediaGrabOperation", []) or []

    def template(self, guid):
        """Recording options for a programme, including the parameters blob.

        Callers hold the guid in either form: some rows keep it already
        percent-encoded. httpx encodes params itself, so an encoded value
        reaches Plex encoded twice and the server answers 400. Normalise here,
        where every caller benefits, rather than at each call site.
        """
        c = self._get("/media/subscriptions/template",
                      {"guid": urllib.parse.unquote(guid)})
        return c.get("SubscriptionTemplate", []) or []

    def create_recording(self, parameters: str, target_section: int, type_: int,
                         prefs: dict | None = None):
        """Schedule a recording.

        `parameters` comes verbatim from the template and is already encoded, so
        it is concatenated rather than passed through a params dict. Anything in
        `prefs` pins the recording further, which is how a specific airing is
        chosen instead of leaving Plex to guess between duplicates.
        """
        url = f"{self.base}/media/subscriptions?{parameters}"
        url += f"&targetLibrarySectionID={target_section}&type={type_}"
        for k, v in (prefs or {}).items():
            url += f"&prefs%5B{k}%5D={v}"
        url += f"&X-Plex-Token={self.token}"
        with self._client() as c:
            r = c.post(url)
        if r.status_code >= 400:
            raise PlexError(f"create recording -> HTTP {r.status_code}: {r.text[:300]}")
        # The reply carries the new subscription, key included. Reading it here
        # is exact; hunting for it afterwards in the scheduled list is not.
        try:
            subs = r.json().get("MediaContainer", {}).get("MediaSubscription") or []
            key = subs[0].get("key") if subs else None
            return str(key) if key is not None else None
        except Exception:
            return None

    def subscription_exists(self, key):
        """True while Plex still holds this subscription.

        Plex will answer a create with 200 and a key, then drop the
        subscription on its own, for instance when the airing is a repeat and
        the rule is new-airings-only. Without this check the app reports a
        recording that does not exist.
        """
        with self._client() as c:
            r = c.get(f"{self.base}/media/subscriptions/{key}",
                      params={"X-Plex-Token": self.token})
        return r.status_code < 400

    def find_subscription(self, guid, begins_at, tries=1, wait=0.7):
        """The subscription key Plex just minted for this broadcast.

        Plex answers a create with the subscription body but no key we can rely
        on, so the key is read back from the scheduled operations, matched on
        the programme guid and the start time of the airing actually chosen.
        That pair names exactly one broadcast.

        Straight after a create the operation is not always listed yet, hence
        the retries: without them the key is lost and cancelling has to go
        looking for it again later.
        """
        for attempt in range(max(1, tries)):
            key = self._find_subscription_once(guid, begins_at)
            if key:
                return key
            if attempt + 1 < tries:
                time.sleep(wait)
        return None

    def _find_subscription_once(self, guid, begins_at):
        for op in self.scheduled():
            meta = op.get("Metadata") or op.get("Video") or {}
            if meta.get("guid") != guid:
                continue
            media = meta.get("Media")
            if isinstance(media, dict):
                media = [media]
            for m in (media or []):
                if int(m.get("beginsAt") or 0) == int(begins_at):
                    key = op.get("mediaSubscriptionID")
                    return str(key) if key is not None else None
        return None

    def delete_subscription(self, key):
        with self._client() as c:
            r = c.delete(f"{self.base}/media/subscriptions/{key}",
                         params={"X-Plex-Token": self.token})
        if r.status_code >= 400:
            raise PlexError(f"delete subscription {key} -> HTTP {r.status_code}")
        return True


def discover(plex: Plex):
    """Work out the EPG provider id and which sections hold Shows and Sports.

    These are per-DVR and per-server, so they are discovered rather than
    configured. Returns (provider, shows, sports, movies).

    Movies matter: a channel showing films has NO entries in the Shows section,
    so leaving it out makes those channels look blank in the guide.
    """
    dvrs = plex.dvrs()
    if not dvrs:
        raise PlexError("no DVR is configured on this Plex server")
    provider = dvrs[0].get("epgIdentifier")
    if not provider:
        raise PlexError("the DVR has no EPG identifier")
    shows = sports = movies = None
    for d in plex.epg_sections(provider):
        title = (d.get("title") or "").lower()
        if title == "sports":
            sports = d.get("key")
        elif title == "shows":
            shows = d.get("key")
        elif title == "movies":
            movies = d.get("key")
    return provider, shows, sports, movies
