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
