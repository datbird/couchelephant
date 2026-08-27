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


def _strip_marks(name: str) -> str:
    """Accents off Latin letters, and nothing else touched.

    A combining mark is only decoration where the base letter is Latin. Beyond
    that it carries meaning: the Japanese dakuten is the difference between
    KA and GA, so folding it away turns the Hanshin Tigers into a word that is
    not "tigers". Hebrew niqqud and Arabic harakat are the same shape of
    mistake. So a mark is dropped only when the letter it sits on is ASCII.
    """
    out = []
    for c in unicodedata.normalize("NFKD", name or ""):
        if unicodedata.combining(c):
            if out and out[-1].isascii():
                continue
            out.append(c)
        else:
            out.append(c)
    # Recomposed, so a mark that survived is one character again. A bare
    # combining mark is not alphanumeric, and the separator pass below would
    # drop it right back out.
    return unicodedata.normalize("NFC", "".join(out))


def _fold(name: str) -> str:
    """Case, accents and punctuation removed. Every letter kept.

    The separator pass keeps anything Unicode calls alphanumeric rather than
    keeping `a-z0-9`. That distinction is the whole of this function: an
    `[^a-z0-9]` filter does not narrow a Cyrillic, Greek, Japanese, Hebrew or
    Arabic name, it deletes it. An empty string is not a miss. It is a key, and
    every team written in one of those scripts arrived at the same one.
    """
    out = _strip_marks(name).lower().replace("&", " and ")
    out = "".join(c if c.isalnum() else " " for c in out)
    return " ".join(out.split())


def ident(name: str) -> str:
    """A team's name folded only as far as spelling, never as far as identity.

    `norm` also drops club words, which is right for finding a team in the
    catalogue and wrong for deciding what to record: it folds "Real Madrid" and
    "Atletico Madrid" both to "madrid", and does the same to five real pairs in
    the shipped catalogue, among them Cincinnati and FC Cincinnati.

    This keeps every word and removes only the things that are not identity:
    case, accents and punctuation. Two spellings of one team still meet here.
    Two different teams never do.
    """
    return _fold(name)


def norm(name: str) -> str:
    """The form two spellings of one team have in common.

    This must stay identical to `norm` in scripts/build_teams.py. A catalogue
    normalised one way and matched another is a catalogue that never matches,
    and `test_teamcat.py` compares the two so they cannot drift apart.
    """
    # Plex writes "San Jose State" and the catalogue "San Jose State" with an
    # accent. One team, two strings, until the accents come off.
    base = _fold(name)
    # Club words carry no identity: "Club Tijuana" is "Tijuana" elsewhere.
    s = re.sub(r"\b(fc|sc|cf|afc|ac|cd|rcd|vfb|sv|bk|club|deportivo|"
               r"real|athletic|atletico)\b", " ", base)
    s = " ".join(s.split())
    # "Athletic Club" is a real side and every word in it is on that list.
    # Stripping to nothing would hand back a key that everything else with no
    # Latin letters also arrives at, so the unstripped form stands instead.
    return s or base


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
    """The catalogue entry a Plex team name belongs to, or None.

    An empty key is refused rather than looked up. A name with nothing
    alphanumeric in it has told us nothing, and answering it with whatever
    entry happens to sit at that key is the fail-open shape: a miss that comes
    back looking like a hit. It did exactly that, answering VfB Stuttgart for
    every name written in Cyrillic or Japanese.
    """
    key = norm(name)
    if not key:
        return None
    return _load()[1].get(key)


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
