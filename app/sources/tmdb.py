"""TMDB: films, and when they are released.

Free for non-commercial use with attribution, but it needs a key the user
registers themselves. So it is optional, and with no key set it answers nothing
rather than raising. Search carries on without it.

A note for anyone extending this. TMDB's terms forbid using their API in
connection with machine learning or artificial intelligence applications.
CouchElephant has none. Adding one would make this a licence problem rather
than a design choice, so it would have to be settled with TMDB first.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://api.themoviedb.org"


def search(q: str, key: str, base: str | None = None) -> list[Announcement]:
    """Films matching what was typed, released or not."""
    q = (q or "").strip()
    key = (key or "").strip()
    # No key is the ordinary case, not an error. Most installs will never set
    # one, and search has to work exactly as well for them.
    if not q or not key:
        return []
    url = f"{(base or BASE).rstrip('/')}/3/search/movie"
    with httpx.Client(timeout=TIMEOUT) as http:
        response = http.get(url, params={"query": q, "api_key": key})
        response.raise_for_status()
        results = (response.json() or {}).get("results") or []
    out = []
    for movie in results:
        if not movie.get("id") or not movie.get("title"):
            continue
        when, how = precision_of(movie.get("release_date"))
        out.append(Announcement(
            source="tmdb",
            source_id=str(movie["id"]),
            title=movie["title"],
            expected_at=when,
            precision=how,
        ))
    return out
