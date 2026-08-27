#!/usr/bin/env python3
"""Rebuild app/data/teams.json, the catalogue of teams you can follow.

WHY THIS FILE EXISTS. Plex only knows the teams playing in the guide it holds,
which is about eleven days. On a real server that was 76 teams: whoever happens
to be on this week. You cannot follow your team in the off-season, and a
college side that plays in November is simply absent in August.

So CouchElephant ships its own catalogue and matches it to Plex's ids as teams
appear in the guide. The catalogue is names, leagues and sports. It carries no
schedule, no scores and no ids of anyone else's, and it is generated once and
committed, so the running app never calls out to anything.

    python3 scripts/build_teams.py

Names come from ESPN's public team endpoints, which is the only source checked
that spells a college side the way Plex does: Plex says "Alabama A&M", and so
does ESPN's `location`. Wikidata gives "Alabama Agricultural and Mechanical
University", which matches nothing.
"""
import json
import os
import re
import sys
import unicodedata
import urllib.request

BASE = "https://site.api.espn.com/apis/site/v2/sports/{path}/teams?limit=1000"

# path, league label, sport label, whether the short "location" is the name
# Plex uses. Professional sides are named in full; college sides are named by
# the school alone, with no mascot.
LEAGUES = [
    ("football/nfl", "NFL", "Football", False),
    ("basketball/nba", "NBA", "Basketball", False),
    ("basketball/wnba", "WNBA", "Basketball", False),
    ("baseball/mlb", "MLB", "Baseball", False),
    ("hockey/nhl", "NHL", "Hockey", False),
    ("soccer/usa.1", "MLS", "Soccer", False),
    ("soccer/usa.nwsl", "NWSL", "Soccer", False),
    ("football/college-football", "NCAA", "Football", True),
    ("basketball/mens-college-basketball", "NCAA", "Basketball", True),
    ("basketball/womens-college-basketball", "NCAA", "Basketball (W)", True),
    ("baseball/college-baseball", "NCAA", "Baseball", True),
    ("hockey/mens-college-hockey", "NCAA", "Hockey", True),
    ("soccer/eng.1", "Premier League", "Soccer", False),
    ("soccer/esp.1", "La Liga", "Soccer", False),
    ("soccer/ita.1", "Serie A", "Soccer", False),
    ("soccer/ger.1", "Bundesliga", "Soccer", False),
    ("soccer/fra.1", "Ligue 1", "Soccer", False),
    ("soccer/mex.1", "Liga MX", "Soccer", False),
    # Second tiers, because a guide carries them and a relegated side is still
    # somebody's team.
    ("soccer/eng.2", "EFL Championship", "Soccer", False),
    ("soccer/ger.2", "2. Bundesliga", "Soccer", False),
    ("soccer/esp.2", "La Liga 2", "Soccer", False),
    ("soccer/ita.2", "Serie B", "Soccer", False),
]


# Spellings a Plex guide uses that no ESPN feed does. Kept short and explicit:
# a long list of these means the normaliser is wrong, not that the world is.
EXTRA_ALIASES = {
    "Bayern Munich": ["FC Bayern Munchen", "FC Bayern Munich", "Bayern Munchen"],
    "Hamburg SV": ["Hamburger SV"],
    "Inter Milan": ["Internazionale"],
    "Monchengladbach": ["Borussia Monchengladbach"],
}


def norm(s):
    """The form two spellings of one team have in common.

    This must stay identical to `teamcat.norm` in the app. A catalogue that
    normalises differently from the matcher is a catalogue that never matches,
    so the app imports nothing from here and this copies nothing from there;
    a test compares the two.
    """
    # Plex writes "San Jose State" and ESPN writes "San Jose State" with an
    # accent, which is one team and two strings until the accents come off.
    # A combining mark is decoration on a Latin letter and meaning elsewhere:
    # the Japanese dakuten is the difference between KA and GA. So a mark comes
    # off only when the letter it sits on is ASCII.
    out = []
    for c in unicodedata.normalize("NFKD", s or ""):
        if unicodedata.combining(c) and out and out[-1].isascii():
            continue
        out.append(c)
    # Recomposed, so a surviving mark is one character again: a bare combining
    # mark is not alphanumeric and the separator pass would drop it.
    s = unicodedata.normalize("NFC", "".join(out)).lower().replace("&", " and ")
    # Anything Unicode calls alphanumeric, not `a-z0-9`. The latter does not
    # narrow a Cyrillic or Japanese name, it deletes it, and every such team
    # then shares one key.
    base = " ".join("".join(c if c.isalnum() else " " for c in s).split())
    # Club prefixes and suffixes carry no identity. Plex says "Club Tijuana"
    # and "FC Bayern Munchen"; the same sides are "Tijuana" and "Bayern"
    # elsewhere.
    s = re.sub(r"\b(fc|sc|cf|afc|ac|cd|rcd|vfb|sv|bk|club|deportivo|"
               r"real|athletic|atletico)\b", " ", base)
    s = " ".join(s.split())
    # "Athletic Club" is a real side made entirely of words on that list.
    return s or base


def fetch(path):
    # A descriptive User-Agent is answered 403 here. A plain one is not.
    req = urllib.request.Request(
        BASE.format(path=path),
        headers={"User-Agent": "curl/8.5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return data["sports"][0]["leagues"][0]["teams"]


def main():
    out = {}
    for path, league, sport, college in LEAGUES:
        try:
            rows = fetch(path)
        except Exception as e:
            print(f"  {path}: FAILED {e}", file=sys.stderr)
            return 1
        for row in rows:
            t = row.get("team") or {}
            location = (t.get("location") or "").strip()
            display = (t.get("displayName") or "").strip()
            short = (t.get("shortDisplayName") or "").strip()
            name = location if college else (display or location)
            if not name:
                continue

            # A college plays several sports under one name. One entry, with
            # the sports listed, rather than the same school five times.
            key = (norm(name), league)
            entry = out.setdefault(key, {
                "name": name, "league": league, "sports": [], "aliases": [],
            })
            if sport not in entry["sports"]:
                entry["sports"].append(sport)
            for alias in (display, location, short, t.get("nickname"),
                          t.get("abbreviation")):
                alias = (alias or "").strip()
                if alias and alias != name and alias not in entry["aliases"]:
                    entry["aliases"].append(alias)
        print(f"  {path}: {len(rows)}", file=sys.stderr)

    for entry in out.values():
        for alias in EXTRA_ALIASES.get(entry["name"], []):
            if alias not in entry["aliases"]:
                entry["aliases"].append(alias)

    teams = sorted(out.values(), key=lambda t: (t["league"], t["name"]))
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = os.path.join(here, "app", "data", "teams.json")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        json.dump({"teams": teams}, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"{len(teams)} teams -> {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
