"""TVmaze: announced series, their premiere dates and their networks.

No API key and no account, which is the whole reason it is the default. The
feature works the moment the container starts, with nothing to configure.

Rate limited to roughly 20 calls per 10 seconds per address, so this is called
on a user action and never in a loop.

The network it names is the producer, not your aerial. A show can come back
saying a streaming service, which no tuner can record. That is fine. An
expectation only becomes a recording when the Plex guide confirms a real airing
on a real channel.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://api.tvmaze.com"


def search(q: str, base: str | None = None) -> list[Announcement]:
    """Series matching what was typed, whether or not they have aired."""
    q = (q or "").strip()
    if not q:
        return []
    url = f"{(base or BASE).rstrip('/')}/search/shows"
    with httpx.Client(timeout=TIMEOUT) as http:
        response = http.get(url, params={"q": q})
        response.raise_for_status()
        hits = response.json() or []
    out = []
    for hit in hits:
        show = hit.get("show") or {}
        if not show.get("id") or not show.get("name"):
            continue
        when, how = precision_of(show.get("premiered"))
        # A broadcast show has `network`. A streaming one has `webChannel` and
        # a null network, so falling through to it is what names the thing.
        channel = show.get("network") or show.get("webChannel") or {}
        out.append(Announcement(
            source="tvmaze",
            source_id=str(show["id"]),
            title=show["name"],
            network=channel.get("name"),
            expected_at=when,
            precision=how,
        ))
    return out


def episodes(show_id: str, base: str | None = None) -> list[Announcement]:
    """Every episode TVmaze has dated for one show, soonest first.

    THE HALF THIS MODULE NEVER ASKED FOR. `search` answers one row per SHOW,
    carrying its premiere date, so a series pass held exactly one expectation
    for ever and never learned about an episode announced later. The endpoint
    is free and unkeyed, same as the search, so there was nothing to buy.

    Every episode, past ones included: deciding what is still ahead needs a
    clock, and a source module has no business owning one. `fill_series_passes`
    filters. That also keeps this testable without freezing time.

    `airstamp` is an ISO instant with a zone and is what the broadcaster
    published, so it earns `time` precision. `airdate` is a bare date and earns
    `day`. An episode with neither is dropped rather than dated by guesswork: a
    row with no date can never match an airing and would sit in the plan card
    for ever.
    """
    show_id = str(show_id or "").strip()
    if not show_id:
        return []
    url = f"{(base or BASE).rstrip('/')}/shows/{show_id}/episodes"
    with httpx.Client(timeout=TIMEOUT) as http:
        response = http.get(url)
        response.raise_for_status()
        rows = response.json() or []
    out = []
    for row in rows:
        if not row.get("id"):
            continue
        when, how = precision_of(row.get("airstamp") or row.get("airdate"))
        if not when:
            continue
        season, number = row.get("season"), row.get("number")
        label = ""
        if season and number:
            label = f"S{int(season):02d}E{int(number):02d}"
        name = (row.get("name") or "").strip()
        out.append(Announcement(
            source="tvmaze",
            source_id=f"ep-{row['id']}",
            title="",              # filled by the caller, which knows the show
            subtitle=" ".join(p for p in (label, name) if p) or None,
            expected_at=when,
            precision=how,
        ))
    out.sort(key=lambda a: a.expected_at)
    return out
