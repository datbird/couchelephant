"""TheSportsDB: the games a league has already scheduled.

Sport is the exception to the three week guide ceiling. Leagues publish whole
seasons months ahead, so a team pass can be filled in for the year even though
no broadcaster has been named for any of it yet.

The free tier answers on a public test key. A user's own key raises the rate
limits and nothing else, which is why the key is optional and never required.

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
    want = (team_name or "").strip().casefold()
    out = []
    for event in events:
        sides = ((event.get("strHomeTeam") or "").casefold(),
                 (event.get("strAwayTeam") or "").casefold())
        if want and want not in sides:
            continue
        stamp = event.get("dateEvent") or ""
        if stamp and event.get("strTime"):
            stamp = f"{stamp} {event['strTime']}"
        when, how = precision_of(stamp)
        source_id = str(event.get("idEvent") or "")
        if not source_id:
            continue
        out.append(Announcement(
            source="thesportsdb",
            source_id=source_id,
            title=team_name,
            subtitle=event.get("strEvent"),
            network=event.get("strTVStation"),
            expected_at=when,
            precision=how,
        ))
    return out
