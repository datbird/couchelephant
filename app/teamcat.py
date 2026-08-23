"""The shipped catalogue of teams, and how a Plex team is matched to it.

Plex only knows the teams playing in the guide it holds, about eleven days.
Measured on a real server that was 76 teams: whoever happens to be on this
week. You cannot follow your team in the off-season, and a college side whose
season starts in November is simply not there in August.

So `app/data/teams.json` ships 1,310 teams, and the list you pick from is the
union of that and whatever Plex currently knows. Rebuild it with
`scripts/build_teams.py`.

The catalogue is a way to find a team and say you want it. It is NOT what makes
a pass work: an airing carries Plex's own team ids, so a pass follows an id.
Until a catalogue team has been seen in the guide it has no id, and the pass
says so rather than looking like it is running.
"""
import json
import os
import re
import threading
import unicodedata

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "teams.json")
_lock = threading.Lock()
_cache = None


def norm(name: str) -> str:
    """The form two spellings of one team have in common.

    This must stay identical to `norm` in scripts/build_teams.py. A catalogue
    normalised one way and matched another is a catalogue that never matches,
    and `test_teamcat.py` compares the two so they cannot drift apart.
    """
    # Plex writes "San Jose State" and the catalogue "San Jose State" with an
    # accent. One team, two strings, until the accents come off.
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    # Club words carry no identity: "Club Tijuana" is "Tijuana" elsewhere.
    s = re.sub(r"\b(fc|sc|cf|afc|ac|cd|rcd|vfb|sv|bk|club|deportivo|"
               r"real|athletic|atletico)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _load():
    global _cache
    if _cache is not None:
        return _cache
    with _lock:
        if _cache is not None:
            return _cache
        try:
            with open(_PATH) as f:
                teams = json.load(f).get("teams") or []
        except (OSError, ValueError):
            # A missing catalogue is a smaller app, not a broken one. Plex's
            # own list still works.
            teams = []
        index = {}
        for t in teams:
            for spelling in [t["name"]] + (t.get("aliases") or []):
                index.setdefault(norm(spelling), t)
        _cache = (teams, index)
    return _cache


def all_teams() -> list[dict]:
    return _load()[0]


def find(name: str) -> dict | None:
    """The catalogue entry a Plex team name belongs to, or None."""
    return _load()[1].get(norm(name))


def leagues() -> list[str]:
    """Every league in the catalogue, in the order they should be offered."""
    order, seen = [], set()
    for t in all_teams():
        if t["league"] not in seen:
            seen.add(t["league"])
            order.append(t["league"])
    # The ones most people mean first, then the rest as they come.
    front = ["NFL", "NBA", "MLB", "NHL", "NCAA", "WNBA", "MLS", "NWSL"]
    return ([name for name in front if name in seen] +
            [name for name in order if name not in front])
