"""TheSportsDB: the games a league has already scheduled.

Sport is the exception to the three week guide ceiling. Leagues publish whole
seasons months ahead, so a team pass can be filled in for the year even though
no broadcaster has been named for any of it yet.

**THE FREE TIER DOES GIVE A SEASON. It depends entirely which endpoint you
ask.** This docstring twice said otherwise, and both claims were wrong.

Measured live 2026-09-02, for the 2026 NFL season on league 4391:

    eventsnext.php   (team)             key 3: 1     key 123: 1
    eventsseason.php (league)           key 3: 5     key 123: 15
    eventsround.php  (league, per round) key 3: 5     key 123: 16   <- a full week

Sixteen is a complete NFL week. Walking the 18 rounds returns 272 events
holding all 17 Kansas City Chiefs games, 2026-09-15 to 2027-01-10, on a free
key. So `season()` walks rounds, and a subscriber key buys rate limit and
depth rather than the feature itself.

THE LESSON, because it cost a whole provider. The endpoint named "season"
answered thinly, and that was read as "this database has no seasons". A second
source was written against ESPN and then deleted once `eventsround.php` was
tried. An endpoint answering thinly is not the database lacking the data, and
one endpoint is not an API.

TVmaze needs no key either: following a series works for everyone.

**And it was asking for the wrong season, measured live 2026-09-01.** The
season call sent no `s` parameter at all. That does not mean "the current
season": the API answers with its EARLIEST, so `eventsseason.php?id=4391`
returned NFL games from 2007-09-06. The free tier's five-row answer was never
this year's, and a subscriber key would have bought a season of the wrong
decade. `current_season()` now reads the league's own `strCurrentSeason`,
because the format is per league ("2026" for the NFL, "2025-2026" for a
football league) and cannot be composed from the clock.

`strTime` is missing on a game whose kickoff has not been set. That is a real
answer and not a gap to paper over, so it lands as day precision. Inventing
midnight would put a time on the screen that nobody published.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://www.thesportsdb.com"

# The documented public test key. "3" is also documented and is MORE limited:
# on `eventsround.php` it returns 5 rows where "123" returns the full 16-game
# NFL week. Measured live 2026-09-02. Rate limited either way.
FREE_KEY = "123"

# How far to walk, and when to stop. A round count is per sport, so the walk
# stops on consecutive empty answers rather than at a number that would be
# wrong for every league but one. The cap is the backstop against a league that
# answers for ever.
_MAX_ROUNDS = 45
_QUIET_ROUNDS = 3


def season(team_name: str, league_id: str, key: str = "",
           base: str | None = None, season: str | None = None) -> list[Announcement]:
    """Every scheduled game this team plays, walked round by round.

    THE ENDPOINT THAT ACTUALLY ANSWERS. `eventsseason.php` is the obvious one
    and it is capped: measured live 2026-09-02 it returned 5 rows on key "3"
    and 15 on key "123", against an NFL season of 272 games, and none of them
    were the team asked about. Reading that as "the free tier does not have
    seasons" was wrong, and it nearly cost this project a second provider.

    `eventsround.php` is not capped the same way. Walked over the rounds it
    returns the whole thing: 272 events across 18 NFL rounds, containing all 17
    Kansas City Chiefs games from 2026-09-15 to 2027-01-10. Free, documented,
    and no key.

    The walk stops on `_QUIET_ROUNDS` empty answers in a row rather than at a
    fixed count, because a round count is per sport: 18 for the NFL, 38 for a
    football league, and a competition may number its rounds past its own
    fixtures. `_MAX_ROUNDS` is the backstop so a league that answers for ever
    cannot spin.

    The endpoint answers for the whole league, so the team is filtered here.
    Home or away both count: a pass follows the team, not the venue.
    """
    league_id = (league_id or "").strip()
    if not league_id:
        return []
    if season is None:
        season = current_season(league_id, key=key, base=base)
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or '').strip() or FREE_KEY}/eventsround.php")
    events, quiet = [], 0
    with httpx.Client(timeout=TIMEOUT) as http:
        for rnd in range(1, _MAX_ROUNDS + 1):
            params = {"id": league_id, "r": rnd}
            if season:
                params["s"] = season
            try:
                response = http.get(url, params=params)
                response.raise_for_status()
                found = (response.json() or {}).get("events") or []
            except Exception:            # noqa: BLE001 — one bad round is not the season
                found = []
            if not found:
                quiet += 1
                if quiet >= _QUIET_ROUNDS:
                    break
                continue
            quiet = 0
            events.extend(found)
    return _as_announcements(events, want=(team_name or "").strip().casefold(),
                             title=team_name)


def league_info(league_id: str, key: str = "",
                base: str | None = None) -> dict:
    """`{"name": ..., "season": ...}` for a league, or empty strings.

    One call answering both, because both callers want it at the same moment:
    the season call needs `s`, and ESPN needs the league's NAME to find its own
    path for it. Asking twice for one row would double the requests against a
    rate-limited free tier for nothing.
    """
    league_id = (league_id or "").strip()
    if not league_id:
        return {"name": "", "season": ""}
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or '').strip() or FREE_KEY}/lookupleague.php")
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            response = http.get(url, params={"id": league_id})
            response.raise_for_status()
            leagues = (response.json() or {}).get("leagues") or []
    except Exception:                    # noqa: BLE001 — a miss is not a league
        return {"name": "", "season": ""}
    row = leagues[0] if leagues else {}
    return {"name": str(row.get("strLeague") or "").strip(),
            "season": str(row.get("strCurrentSeason") or "").strip()}


def current_season(league_id: str, key: str = "",
                   base: str | None = None) -> str:
    """What the league itself calls the season on now, or "".

    The season string is not a year and cannot be composed from the clock. The
    NFL says "2026"; a football league says "2025-2026". `lookupleague.php`
    states it, is free, and is right for every league without a table of
    formats here that would drift.

    An empty answer is not guessed at. The caller then asks without a season,
    which is what it did before this existed.
    """
    league_id = (league_id or "").strip()
    if not league_id:
        return ""
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or '').strip() or FREE_KEY}/lookupleague.php")
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            response = http.get(url, params={"id": league_id})
            response.raise_for_status()
            leagues = (response.json() or {}).get("leagues") or []
    except Exception:                    # noqa: BLE001 — a miss is not a season
        return ""
    return str((leagues[0] if leagues else {}).get("strCurrentSeason") or "").strip()


def _as_announcements(events, want=None, title="") -> list[Announcement]:
    """Turn league events into announcements, keeping only one team's games.

    `want` of None keeps everything, which is what the per-team endpoint needs:
    it has already filtered.
    """
    out = []
    for event in events:
        sides = ((event.get("strHomeTeam") or "").casefold(),
                 (event.get("strAwayTeam") or "").casefold())
        if want and want not in sides:
            continue
        stamp = event.get("dateEvent") or ""
        # Missing strTime is a real answer: the kickoff is not set. It lands as
        # day precision rather than an invented midnight.
        if stamp and event.get("strTime"):
            stamp = f"{stamp} {event['strTime']}"
        when, how = precision_of(stamp)
        source_id = str(event.get("idEvent") or "")
        if not source_id:
            continue
        out.append(Announcement(
            source="thesportsdb",
            source_id=source_id,
            title=title or event.get("strHomeTeam") or "",
            subtitle=event.get("strEvent"),
            network=event.get("strTVStation"),
            expected_at=when,
            precision=how,
        ))
    return out


def team(name: str, key: str = "", base: str | None = None) -> dict | None:
    """Resolve a team name to its ids, or nothing.

    A pass knows a team by name. Both the season and the next-game endpoints
    want an id, so this is the step in between. Answered once and cached on the
    pass: the ids do not change.

    An unknown name resolves to nothing rather than to the closest match. A
    wrong team would fill a pass with somebody else's games.
    """
    name = (name or "").strip()
    if not name:
        return None
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or '').strip() or FREE_KEY}/searchteams.php")
    with httpx.Client(timeout=TIMEOUT) as http:
        response = http.get(url, params={"t": name})
        response.raise_for_status()
        teams = (response.json() or {}).get("teams") or []
    if not teams:
        return None
    first = teams[0]
    if not first.get("idTeam"):
        return None
    return {
        "team_id": str(first["idTeam"]),
        "league_id": str(first.get("idLeague") or ""),
        "team_name": first.get("strTeam") or name,
        "league_name": first.get("strLeague") or "",
    }


def upcoming(team_id: str, key: str = "", base: str | None = None) -> list[Announcement]:
    """The next scheduled games for one team.

    This is what somebody with no key actually gets: one game, sometimes a few.
    Thin, but it is the honest free answer, and it is the only part of this
    source that works for everybody.
    """
    team_id = (team_id or "").strip()
    if not team_id:
        return []
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or '').strip() or FREE_KEY}/eventsnext.php")
    with httpx.Client(timeout=TIMEOUT) as http:
        response = http.get(url, params={"id": team_id})
        response.raise_for_status()
        events = (response.json() or {}).get("events") or []
    return _as_announcements(events, want=None)
