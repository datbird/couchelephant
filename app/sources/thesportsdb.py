"""TheSportsDB: the games a league has already scheduled.

Sport is the exception to the three week guide ceiling. Leagues publish whole
seasons months ahead, so a team pass can be filled in for the year even though
no broadcaster has been named for any of it yet.

**What the free tier actually gives, measured 2026-08-31: one upcoming game
per team, not a season.** `eventsseason.php` on the public test key answered
five events for the whole NFL and none for the team asked about, while
`eventsnext.php` answered exactly one. A subscriber key is what unlocks a full
published season. The earlier claim here, that a key only raised rate limits,
was wrong and shipped that way.

So this source is honest but thin without a key. TVmaze needs no key and is
unaffected: following a series works for everyone either way.

`strTime` is missing on a game whose kickoff has not been set. That is a real
answer and not a gap to paper over, so it lands as day precision. Inventing
midnight would put a time on the screen that nobody published.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://www.thesportsdb.com"

# The documented public test key. Rate limited, and enough to be useful.
FREE_KEY = "3"


def season(team_name: str, league_id: str, key: str = "",
           base: str | None = None) -> list[Announcement]:
    """Every scheduled game this team plays, from the league's own calendar.

    The endpoint answers for the whole league, so the team is filtered here.
    Home or away both count: a pass follows the team, not the venue.
    """
    league_id = (league_id or "").strip()
    if not league_id:
        return []
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or '').strip() or FREE_KEY}/eventsseason.php")
    with httpx.Client(timeout=TIMEOUT) as http:
        response = http.get(url, params={"id": league_id})
        response.raise_for_status()
        events = (response.json() or {}).get("events") or []
    return _as_announcements(events, want=(team_name or "").strip().casefold(),
                             title=team_name)


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
