# Future Passes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user follow a show or a team that the Plex guide does not know about yet, and promote that intention to a real recording pass the moment the guide catches up.

**Architecture:** External sources never book anything. They produce `Announcement` records, which are stored as rows in a new `expectations` table hanging off an ordinary pass. Each sync tries to match unmatched expectations against real guide airings. A match promotes; a date that passes with no match raises a warning. The guide tables are never written to by any of this.

**Tech Stack:** Python 3.12, FastAPI, SQLite, httpx, Jinja2, pytest, Playwright.

**Spec:** `docs/specs/2026-08-31-future-passes.md`

## Global Constraints

- The repo is public. No hostnames, IPs, tokens or personal call signs in code, tests or fixtures. Invented demo values only.
- No em dashes and no emoji in any user-facing string, comment or commit message.
- Every provider is faked over real HTTP in tests, in the style of `tests/fake_plex.py`. No test may reach a third party.
- New tables go in `SCHEMA` in `app/db.py`. New **columns** must ALSO be listed in `MIGRATIONS`, or `tests/test_conventions.py::test_every_migration_is_listed_rather_than_only_in_the_schema` fails.
- `precision` is one of exactly: `time`, `day`, `month`, `year`. Rendering must never show more precision than the value carries.
- Nothing outside the Plex guide may create a recording.
- Run the unit suite with `PYTHONPATH=. .venv/bin/pytest tests --ignore=tests/ui -q`.
- Run the browser suite with `PYTHONPATH=. COUCHELEPHANT_FAKE_ANCHOR=$(( $(date +%s)/1800*1800+1800 )) .venv/bin/pytest tests/ui -q`.
- Working tree is `~/gitrepos/couchelephant` on host `sirtoolio`. Commit to `main`.

---

### Task 1: The expectations table

**Files:**
- Modify: `app/db.py` (the `SCHEMA` string, and `MIGRATIONS` for the notices column added in Task 8; this task adds only the table)
- Create: `tests/test_expectations.py`

**Interfaces:**
- Consumes: `db.tx()`, `db.query()`, `db.one()` from `app/db.py`.
- Produces: table `expectations` with the columns below. Tasks 5, 6, 7 all read and write it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_expectations.py
"""What an expectation is, before anything knows how to make one."""
from app import db


def test_the_table_exists_and_holds_a_soft_date():
    with db.tx() as c:
        c.execute(
            "INSERT INTO expectations (pass_id, source, source_id, title, "
            "expected_at, precision, updated_at) VALUES (?,?,?,?,?,?,?)",
            (1, "tvmaze", "99999", "Gobiligook", 1804204800, "month", 1))
    row = db.one("SELECT * FROM expectations WHERE title = 'Gobiligook'")
    assert row["precision"] == "month"
    assert row["matched_guid"] is None
    assert row["missed_at"] is None


def test_the_same_thing_cannot_be_expected_twice_for_one_pass():
    """A re-run of a season import must update rows, not pile up duplicates."""
    import sqlite3
    import pytest
    for _ in range(1):
        with db.tx() as c:
            c.execute(
                "INSERT INTO expectations (pass_id, source, source_id, title, "
                "precision, updated_at) VALUES (?,?,?,?,?,?)",
                (7, "thesportsdb", "dup-1", "A at B", "time", 1))
    with pytest.raises(sqlite3.IntegrityError):
        with db.tx() as c:
            c.execute(
                "INSERT INTO expectations (pass_id, source, source_id, title, "
                "precision, updated_at) VALUES (?,?,?,?,?,?)",
                (7, "thesportsdb", "dup-1", "A at B", "time", 2))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py -q`
Expected: FAIL, `sqlite3.OperationalError: no such table: expectations`

- [ ] **Step 3: Add the table to the schema**

In `app/db.py`, inside the `SCHEMA` string, after the `notices` table:

```sql
-- Something the user has asked for that the guide does not carry yet: a
-- series announced for next spring, a game the league has scheduled but no
-- broadcaster has claimed. It is an intention, never a booking. Only a guide
-- airing can be recorded, because only the guide knows the channel.
--
-- Kept out of `programs` and `airings` on purpose. Those are read by every
-- query in the app, and mixing invented rows into them would mean auditing
-- all of those queries, forever, for a flag they could forget.
CREATE TABLE IF NOT EXISTS expectations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id       INTEGER NOT NULL,
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    title         TEXT NOT NULL,
    subtitle      TEXT,
    network       TEXT,
    expected_at   INTEGER,
    -- How much of `expected_at` is real: time, day, month or year. A league
    -- gives a time. An announcement often gives only a month. Rendering more
    -- precision than this says is inventing a fact.
    precision     TEXT NOT NULL,
    matched_guid  TEXT,
    matched_at    INTEGER,
    missed_at     INTEGER,
    updated_at    INTEGER,
    UNIQUE (source, source_id, pass_id)
);
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py -q`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/db.py tests/test_expectations.py
git commit -m "An expectation is a thing you want that the guide has not heard of"
```

---

### Task 2: The source interface, and TVmaze

**Files:**
- Create: `app/sources/__init__.py`
- Create: `app/sources/tvmaze.py`
- Create: `tests/fake_sources.py`
- Create: `tests/test_sources.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `sources.Announcement` dataclass with fields `source: str`, `source_id: str`, `title: str`, `subtitle: str | None`, `network: str | None`, `expected_at: int | None`, `precision: str`.
  - `sources.precision_of(date_text: str) -> tuple[int | None, str]` turning `"2027-03"` into `(epoch, "month")`.
  - `tvmaze.search(q: str, base: str | None = None) -> list[Announcement]`.
  - `tests/fake_sources.start()` returning a base URL, and `stop()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sources.py
"""The external sources, against a fake that answers like the real ones."""
import pytest

from app import sources
from app.sources import tvmaze
from tests import fake_sources


@pytest.fixture(scope="module")
def base():
    url = fake_sources.start()
    yield url
    fake_sources.stop()


def test_a_full_date_keeps_its_time():
    assert sources.precision_of("2027-03-14")[1] == "day"


def test_a_month_only_date_is_not_promoted_to_a_day():
    """TVmaze answers '2027-03' for a show with no announced day. Reading that
    as the first of March invents a fact the source did not give."""
    when, how = sources.precision_of("2027-03")
    assert how == "month"
    assert when is not None


def test_an_empty_date_is_not_a_date():
    assert sources.precision_of("") == (None, "year")
    assert sources.precision_of(None) == (None, "year")


def test_tvmaze_finds_an_announced_series(base):
    hits = tvmaze.search("Gobiligook", base=base)
    assert len(hits) == 1
    a = hits[0]
    assert a.source == "tvmaze"
    assert a.title == "Gobiligook"
    assert a.network == "ABC"
    assert a.precision == "month"


def test_tvmaze_returns_nothing_for_a_title_that_does_not_exist(base):
    assert tvmaze.search("Nothing By This Name", base=base) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_sources.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.sources'`

- [ ] **Step 3: Write the fake, the shared types, and the provider**

```python
# tests/fake_sources.py
"""The external sources, answering the way the real ones do.

Served over real HTTP on localhost so the provider modules are exercised
through httpx rather than mocked away. The shapes here are copied from real
responses, including the parts that matter: TVmaze gives a month-only
premiere date for a show with no announced day, and names a network that may
be a streaming service rather than anything an antenna can reach.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

TVMAZE_HITS = {
    "gobiligook": [{
        "score": 0.9,
        "show": {"id": 99999, "name": "Gobiligook", "premiered": "2027-03",
                 "status": "In Development",
                 "network": {"name": "ABC"}, "webChannel": None},
    }],
}

SPORTSDB_EVENTS = [
    {"idEvent": "2000001", "strEvent": "Ravens vs Falcons",
     "strHomeTeam": "Ravens", "strAwayTeam": "Falcons",
     "dateEvent": "2027-01-10", "strTime": "20:15:00", "strTVStation": None},
    {"idEvent": "2000002", "strEvent": "Ravens vs Pilots",
     "strHomeTeam": "Ravens", "strAwayTeam": "Pilots",
     "dateEvent": "2027-01-17", "strTime": None, "strTVStation": None},
]

TMDB_HITS = [{"id": 55555, "title": "Quorbis Rising",
              "release_date": "2027-05-21", "overview": ""}]

_server = None
_thread = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        term = params.get("q", params.get("query", "")).replace("%20", " ").lower()
        if path == "/search/shows":
            self._send(TVMAZE_HITS.get(term, []))
        elif path == "/api/v1/json/3/eventsseason.php":
            self._send({"events": SPORTSDB_EVENTS})
        elif path == "/3/search/movie":
            self._send({"results": TMDB_HITS if term else []})
        else:
            self.send_response(404)
            self.end_headers()


def start() -> str:
    global _server, _thread
    _server = HTTPServer(("127.0.0.1", 0), _Handler)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()
    return f"http://127.0.0.1:{_server.server_address[1]}"


def stop() -> None:
    if _server:
        _server.shutdown()
```

```python
# app/sources/__init__.py
"""Where CouchElephant looks for things the Plex guide has not heard of.

No guide data anywhere reaches further than about three weeks, paid sources
included, because no broadcaster has decided that far ahead. What does exist
months out is announcements and published league schedules. These modules
fetch those. They never book anything: only a guide airing carries a channel.
"""
import calendar
import datetime
from dataclasses import dataclass

TIMEOUT = 15.0

# Ordered loosest to tightest. A caller comparing two answers keeps the tighter.
PRECISIONS = ("year", "month", "day", "time")


@dataclass(frozen=True)
class Announcement:
    """One thing a source says is coming, at whatever precision it knows."""
    source: str
    source_id: str
    title: str
    subtitle: str | None = None
    network: str | None = None
    expected_at: int | None = None
    precision: str = "year"


def precision_of(text: str | None) -> tuple[int | None, str]:
    """Turn a source's date string into an epoch and an honest precision.

    A source that says "2027-03" has not said the first of March. Reading it
    that way invents a day, and a reader takes an invented date for a real
    one. So the epoch is the start of the range and the precision says how
    much of it to believe.
    """
    text = (text or "").strip()
    if not text:
        return None, "year"
    for fmt, how in (("%Y-%m-%dT%H:%M:%S", "time"), ("%Y-%m-%d %H:%M:%S", "time"),
                     ("%Y-%m-%d", "day"), ("%Y-%m", "month"), ("%Y", "year")):
        try:
            dt = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return calendar.timegm(dt.timetuple()), how
    return None, "year"
```

```python
# app/sources/tvmaze.py
"""TVmaze: announced series, their premiere dates and their networks.

No API key and no account, which is why it is the default. Rate limited to
about 20 calls per 10 seconds per address, so this is called on a user action
and never in a loop.

The network it names is the producer, not your aerial. A show can come back
saying HBO Max, which no tuner can record. That is fine: an expectation only
becomes a recording when the Plex guide confirms a real airing on a real
channel.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://api.tvmaze.com"


def search(q: str, base: str | None = None) -> list[Announcement]:
    q = (q or "").strip()
    if not q:
        return []
    url = f"{(base or BASE).rstrip('/')}/search/shows"
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(url, params={"q": q})
        r.raise_for_status()
        hits = r.json() or []
    out = []
    for hit in hits:
        show = hit.get("show") or {}
        if not show.get("id") or not show.get("name"):
            continue
        when, how = precision_of(show.get("premiered"))
        channel = show.get("network") or show.get("webChannel") or {}
        out.append(Announcement(
            source="tvmaze", source_id=str(show["id"]), title=show["name"],
            network=(channel or {}).get("name"),
            expected_at=when, precision=how))
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_sources.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/sources tests/fake_sources.py tests/test_sources.py
git commit -m "TVmaze knows about a show months before the guide does"
```

---

### Task 3: TheSportsDB, for published league schedules

**Files:**
- Create: `app/sources/thesportsdb.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- Consumes: `sources.Announcement`, `sources.precision_of`.
- Produces: `thesportsdb.season(team_name: str, league_id: str, key: str = "3", base: str | None = None) -> list[Announcement]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sources.py`:

```python
from app.sources import thesportsdb


def test_a_scheduled_game_with_a_time_keeps_the_time(base):
    games = thesportsdb.season("Ravens", "4391", base=base)
    first = [g for g in games if g.source_id == "2000001"][0]
    assert first.precision == "time"
    assert first.subtitle == "Ravens vs Falcons"


def test_a_scheduled_game_with_no_time_is_only_a_day(base):
    """The league has announced the date but not the kickoff. Showing 12:00 AM
    would be a time nobody published."""
    games = thesportsdb.season("Ravens", "4391", base=base)
    second = [g for g in games if g.source_id == "2000002"][0]
    assert second.precision == "day"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_sources.py -q`
Expected: FAIL, `ImportError: cannot import name 'thesportsdb'`

- [ ] **Step 3: Write the provider**

```python
# app/sources/thesportsdb.py
"""TheSportsDB: the games a league has already scheduled.

Sport is the exception to the three week guide ceiling. Leagues publish whole
seasons months ahead, so a team pass can be filled in for the year even though
no broadcaster has been named yet.

The free tier answers on the public test key. A user's own key raises the
limits and nothing else, which is why the key is optional.

`strTime` is missing on a game whose kickoff has not been set. That is a real
answer, not a gap to paper over, so it lands as day precision.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://www.thesportsdb.com"
FREE_KEY = "3"


def season(team_name: str, league_id: str, key: str = "",
           base: str | None = None) -> list[Announcement]:
    league_id = (league_id or "").strip()
    if not league_id:
        return []
    url = (f"{(base or BASE).rstrip('/')}/api/v1/json/"
           f"{(key or FREE_KEY)}/eventsseason.php")
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(url, params={"id": league_id})
        r.raise_for_status()
        events = (r.json() or {}).get("events") or []
    want = (team_name or "").strip().casefold()
    out = []
    for e in events:
        home = (e.get("strHomeTeam") or "").casefold()
        away = (e.get("strAwayTeam") or "").casefold()
        if want and want not in (home, away):
            continue
        stamp = e.get("dateEvent") or ""
        if stamp and e.get("strTime"):
            stamp = f"{stamp} {e['strTime']}"
        when, how = precision_of(stamp)
        out.append(Announcement(
            source="thesportsdb", source_id=str(e.get("idEvent") or ""),
            title=team_name, subtitle=e.get("strEvent"),
            network=e.get("strTVStation"), expected_at=when, precision=how))
    return [a for a in out if a.source_id]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_sources.py -q`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/sources/thesportsdb.py tests/test_sources.py
git commit -m "A league publishes its season long before a broadcaster claims it"
```

---

### Task 4: TMDB, for films

**Files:**
- Create: `app/sources/tmdb.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- Consumes: `sources.Announcement`, `sources.precision_of`.
- Produces: `tmdb.search(q: str, key: str, base: str | None = None) -> list[Announcement]`. Returns `[]` when `key` is empty.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sources.py`:

```python
from app.sources import tmdb


def test_tmdb_is_silent_without_a_key(base):
    """It is optional. With no key it must return nothing rather than raise,
    so search still works for everyone else."""
    assert tmdb.search("Quorbis", key="", base=base) == []


def test_tmdb_finds_an_unreleased_film(base):
    hits = tmdb.search("Quorbis", key="demo-key", base=base)
    assert len(hits) == 1
    assert hits[0].title == "Quorbis Rising"
    assert hits[0].precision == "day"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_sources.py -q`
Expected: FAIL, `ImportError: cannot import name 'tmdb'`

- [ ] **Step 3: Write the provider**

```python
# app/sources/tmdb.py
"""TMDB: films, and when they are released.

Free for non-commercial use with attribution, but it needs a key the user
registers themselves, so it is optional. With no key set this answers nothing
rather than raising, and search carries on without it.

Note for anyone extending this: TMDB's terms forbid using their API in
connection with machine learning or AI applications. CouchElephant has none.
Adding one would make this a licence problem, not just a design choice.
"""
import httpx

from . import TIMEOUT, Announcement, precision_of

BASE = "https://api.themoviedb.org"


def search(q: str, key: str, base: str | None = None) -> list[Announcement]:
    q, key = (q or "").strip(), (key or "").strip()
    if not q or not key:
        return []
    url = f"{(base or BASE).rstrip('/')}/3/search/movie"
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.get(url, params={"query": q, "api_key": key})
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    out = []
    for m in results:
        if not m.get("id") or not m.get("title"):
            continue
        when, how = precision_of(m.get("release_date"))
        out.append(Announcement(
            source="tmdb", source_id=str(m["id"]), title=m["title"],
            expected_at=when, precision=how))
    return out
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_sources.py -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add app/sources/tmdb.py tests/test_sources.py
git commit -m "Films, when someone has supplied a TMDB key"
```

---

### Task 5: Storing what a pass is waiting for

**Files:**
- Create: `app/expectations.py`
- Modify: `tests/test_expectations.py`

**Interfaces:**
- Consumes: `sources.Announcement`, `db.tx`, `db.query`, `db.one`.
- Produces:
  - `expectations.store(pass_id: int, items: list[Announcement], now: int | None = None) -> int` returning how many rows were written or refreshed.
  - `expectations.waiting(pass_id: int | None = None) -> list[dict]` returning unmatched rows.
  - `expectations.render_when(expected_at: int | None, precision: str, tz: str) -> str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_expectations.py`:

```python
from app import expectations
from app.sources import Announcement


def _ann(**kw):
    base = dict(source="tvmaze", source_id="1", title="X", precision="day",
                expected_at=1804204800)
    base.update(kw)
    return Announcement(**base)


def test_storing_twice_updates_rather_than_duplicates():
    expectations.store(50, [_ann(source_id="s1")], now=10)
    expectations.store(50, [_ann(source_id="s1", title="X renamed")], now=20)
    rows = db.query("SELECT * FROM expectations WHERE pass_id = 50")
    assert len(rows) == 1
    assert rows[0]["title"] == "X renamed"
    assert rows[0]["updated_at"] == 20


def test_a_month_renders_as_a_month_and_not_as_a_midnight():
    """The whole point of `precision`. Anything that shows 12:00 AM for a date
    nobody published is telling the user something untrue."""
    when = expectations.render_when(1804204800, "month", "UTC")
    assert "12:00" not in when and "00:00" not in when
    assert "2027" in when


def test_a_time_renders_with_its_time():
    when = expectations.render_when(1804204800, "time", "UTC")
    assert ":" in when


def test_an_unknown_date_says_so():
    assert expectations.render_when(None, "year", "UTC") == "date not announced"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'app.expectations'`

- [ ] **Step 3: Write the module**

```python
# app/expectations.py
"""What a pass is still waiting for.

An expectation is an intention, never a booking. It says "you asked to follow
this, and here is when a source outside Plex thinks it happens". Only a guide
airing can be recorded, because only the guide knows the channel.
"""
import datetime
import time
import zoneinfo

from . import db

WHEN_UNKNOWN = "date not announced"

# What each precision is allowed to show. Anything more is invented.
_FORMATS = {
    "time": "%a %b %-d, %Y at %-I:%M %p",
    "day": "%a %b %-d, %Y",
    "month": "%B %Y",
    "year": "%Y",
}


def store(pass_id: int, items, now: int | None = None) -> int:
    """Write or refresh what this pass is waiting for."""
    now = int(now if now is not None else time.time())
    written = 0
    with db.tx() as c:
        for a in items:
            c.execute(
                """INSERT INTO expectations (pass_id, source, source_id, title,
                       subtitle, network, expected_at, precision, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, source_id, pass_id) DO UPDATE SET
                     title=excluded.title, subtitle=excluded.subtitle,
                     network=excluded.network, expected_at=excluded.expected_at,
                     precision=excluded.precision, updated_at=excluded.updated_at""",
                (pass_id, a.source, a.source_id, a.title, a.subtitle, a.network,
                 a.expected_at, a.precision, now))
            written += 1
    return written


def waiting(pass_id: int | None = None) -> list[dict]:
    """Expectations the guide has not confirmed yet."""
    sql = ("SELECT * FROM expectations WHERE matched_guid IS NULL"
           "{extra} ORDER BY COALESCE(expected_at, 1 << 40), title")
    if pass_id is None:
        return [dict(r) for r in db.query(sql.format(extra=""))]
    return [dict(r) for r in db.query(sql.format(extra=" AND pass_id = ?"),
                                      (pass_id,))]


def render_when(expected_at: int | None, precision: str, tz: str) -> str:
    """Say the date at exactly the precision the source gave, and no more.

    A source that said "2027-03" did not say the first of March at midnight.
    Formatting it that way hands the reader a fact nobody published.
    """
    if not expected_at:
        return WHEN_UNKNOWN
    fmt = _FORMATS.get(precision) or _FORMATS["year"]
    try:
        zone = zoneinfo.ZoneInfo(tz or "UTC")
    except Exception:
        zone = zoneinfo.ZoneInfo("UTC")
    when = datetime.datetime.fromtimestamp(expected_at, zone)
    try:
        return when.strftime(fmt)
    except ValueError:
        # Windows and some libc builds reject the %-d dash-padding form.
        return when.strftime(fmt.replace("%-", "%"))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py -q`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add app/expectations.py tests/test_expectations.py
git commit -m "Remember what a pass is waiting for, at the precision it was told"
```

---

### Task 6: Promotion, when the guide catches up

**Files:**
- Modify: `app/expectations.py`
- Modify: `app/sync.py` (call the sweep inside `full_sync`, beside the existing `check_team_passes` call around line 396)
- Modify: `tests/test_expectations.py`

**Interfaces:**
- Consumes: `expectations.waiting`, the `airings` and `programs` tables.
- Produces: `expectations.promote(now: int | None = None) -> int` returning how many were matched.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_expectations.py`:

```python
def test_promotion_matches_the_guide_and_records_the_guid():
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, section) "
                  "VALUES ('plex://x/promote-1', 'Gobiligook', 'shows')")
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, begins_at, "
                  "channel_vcn) VALUES ('a-promote-1', 'plex://x/promote-1', "
                  "?, '9.1')", (1804204800 + 3600,))
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (60, 'tvmaze', 'p1', 'Gobiligook', ?, 'day', 1)",
                  (1804204800,))
    assert expectations.promote(now=1804204800) >= 1
    row = db.one("SELECT * FROM expectations WHERE source_id = 'p1'")
    assert row["matched_guid"] == "plex://x/promote-1"
    assert row["matched_at"] is not None


def test_promotion_does_not_match_a_different_show_with_a_near_date():
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, section) "
                  "VALUES ('plex://x/other-1', 'Something Else', 'shows')")
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, begins_at, "
                  "channel_vcn) VALUES ('a-other-1', 'plex://x/other-1', ?, '9.1')",
                  (1804204800 + 3600,))
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (61, 'tvmaze', 'p2', 'Not In The Guide', ?, 'day', 1)",
                  (1804204800,))
    expectations.promote(now=1804204800)
    assert db.one("SELECT * FROM expectations WHERE source_id = 'p2'")["matched_guid"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py -q`
Expected: FAIL, `AttributeError: module 'app.expectations' has no attribute 'promote'`

- [ ] **Step 3: Write promotion and call it from the sync**

Append to `app/expectations.py`:

```python
# How far either side of an expected date a guide airing may sit and still be
# the same thing. A month-precision guess is a whole month wide; a published
# kickoff should be within a day of what the league said.
_WINDOW = {"time": 86400, "day": 2 * 86400, "month": 31 * 86400,
           "year": 366 * 86400}


def promote(now: int | None = None) -> int:
    """Bind an expectation to a real guide airing, once one exists.

    From that moment the pass behaves like every other pass and books through
    the path that already exists. Nothing here books anything.
    """
    now = int(now if now is not None else time.time())
    matched = 0
    for e in waiting():
        if not e["expected_at"]:
            continue
        span = _WINDOW.get(e["precision"], _WINDOW["year"])
        row = db.one(
            """SELECT p.guid FROM airings a JOIN programs p ON p.guid = a.program_guid
               WHERE ulower(COALESCE(NULLIF(p.grandparent_title,''), p.title))
                     = ulower(?)
                 AND a.begins_at BETWEEN ? AND ?
               ORDER BY a.begins_at LIMIT 1""",
            (e["title"], e["expected_at"] - span, e["expected_at"] + span))
        if not row:
            continue
        with db.tx() as c:
            c.execute("UPDATE expectations SET matched_guid = ?, matched_at = ?, "
                      "missed_at = NULL WHERE id = ?", (row["guid"], now, e["id"]))
        matched += 1
    return matched
```

In `app/sync.py`, inside `full_sync`, immediately after the existing
`health.record(raised, now, owns=health.TEAM_CODES)` call:

```python
    # The guide may have just reached something a pass has been waiting months
    # for. Bind it before the pass engine runs, so it books on this same sync.
    expectations.promote(now)
```

Add `from . import expectations` to the imports at the top of `app/sync.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py tests/test_sync.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add app/expectations.py app/sync.py tests/test_expectations.py
git commit -m "When the guide finally carries it, the waiting stops and the pass takes over"
```

---

### Task 7: Saying so when it never arrives

**Files:**
- Modify: `app/health.py` (add the code and the codes set)
- Modify: `app/expectations.py` (add `sweep_misses`)
- Modify: `app/sync.py` (record the notice)
- Modify: `tests/test_expectations.py`

**Interfaces:**
- Consumes: `health.record`, `expectations.waiting`.
- Produces:
  - `health.EXPECTATION_MISSED = "expectation_missed"`, `health.EXPECT_CODES = frozenset((EXPECTATION_MISSED,))`.
  - `expectations.sweep_misses(guide_ends_at: int | None, now: int | None = None) -> list[dict]` returning notice dicts.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_expectations.py`:

```python
def test_a_show_the_guide_reached_and_never_carried_is_reported():
    with db.tx() as c:
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (70, 'tvmaze', 'm1', 'Never Aired', 1804204800, 'day', 1)")
    raised = expectations.sweep_misses(guide_ends_at=1804204800 + 5 * 86400,
                                       now=1804204800 + 5 * 86400)
    assert any("Never Aired" in n["detail"] for n in raised)
    assert db.one("SELECT * FROM expectations WHERE source_id = 'm1'")["missed_at"]


def test_a_miss_keeps_looking_rather_than_being_deleted():
    """A show can slip. Deleting the expectation would silently give up on it."""
    expectations.sweep_misses(guide_ends_at=1804204800 + 5 * 86400,
                             now=1804204800 + 5 * 86400)
    assert db.one("SELECT * FROM expectations WHERE source_id = 'm1'") is not None
    assert any(e["source_id"] == "m1" for e in expectations.waiting())


def test_nothing_is_reported_while_the_guide_has_not_got_there_yet():
    with db.tx() as c:
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (71, 'tvmaze', 'm2', 'Still Coming', 1904204800, 'day', 1)")
    raised = expectations.sweep_misses(guide_ends_at=1804204800, now=1804204800)
    assert not any("Still Coming" in n["detail"] for n in raised)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py -q`
Expected: FAIL, `AttributeError: module 'app.expectations' has no attribute 'sweep_misses'`

- [ ] **Step 3: Write the sweep**

In `app/health.py`, beside the other codes near line 27:

```python
EXPECTATION_MISSED = "expectation_missed"
```

and beside the other sets near line 37:

```python
EXPECT_CODES = frozenset((EXPECTATION_MISSED,))
```

Append to `app/expectations.py`:

```python
def sweep_misses(guide_ends_at: int | None, now: int | None = None) -> list[dict]:
    """Report anything the guide has now reached past and never carried.

    Only judged once the guide actually extends beyond the expected date.
    Before that, silence is the guide being short, not the show being missing.

    A miss is a warning and never a deletion. A show can slip a week, and
    throwing the expectation away would be giving up on it quietly.
    """
    now = int(now if now is not None else time.time())
    if not guide_ends_at:
        return []
    late = [e for e in waiting()
            if e["expected_at"] and e["expected_at"] < guide_ends_at]
    if not late:
        return []
    with db.tx() as c:
        for e in late:
            c.execute("UPDATE expectations SET missed_at = ? WHERE id = ?",
                      (now, e["id"]))
    names = sorted({e["title"] for e in late})
    shown = ", ".join(names[:3]) + (" and others" if len(names) > 3 else "")
    return [{
        "code": "expectation_missed",
        "severity": "warn",
        "title": "Something you are waiting for did not reach the guide",
        "detail": (f"The guide now runs past the date announced for {shown}, "
                   f"and no airing matched. The date may have moved, the title "
                   f"may be spelled differently in the guide, or it may not be "
                   f"carried on any channel you receive."),
        "hint": ("CouchElephant keeps looking. Check the title against the "
                 "guide, or remove the pass if the show is not coming."),
    }]
```

In `app/sync.py`, immediately after the `expectations.promote(now)` line added in Task 6:

```python
    health.record(expectations.sweep_misses(guide_ends_at, now), now,
                  owns=health.EXPECT_CODES)
```

Use the same `guide_ends_at` value the sync already writes to `sync_log`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_expectations.py tests/test_health.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add app/health.py app/expectations.py app/sync.py tests/test_expectations.py
git commit -m "Say so when a thing you were waiting for never reached the guide"
```

---

### Task 8: A notice you are allowed to dismiss

**Files:**
- Modify: `app/db.py` (`SCHEMA` notices table, and `MIGRATIONS`)
- Modify: `app/health.py` (`open_notices`, and a `dismiss` function)
- Create: `app/routes/notices.py`
- Modify: `app/web.py` (register the router)
- Modify: `tests/test_health.py`

**Interfaces:**
- Consumes: `db.tx`, `db.one`.
- Produces:
  - `health.TIP = "tip"`.
  - `health.dismiss(code: str) -> bool`, returning False and changing nothing for any severity other than `tip`.
  - Route `POST /api/notices/{code}/dismiss`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health.py`:

```python
def test_a_tip_can_be_dismissed():
    health.record([{"code": "keys_available", "severity": health.TIP,
                    "title": "T", "detail": "D", "hint": None}], 100,
                  owns=frozenset({"keys_available"}))
    assert health.dismiss("keys_available") is True
    assert "keys_available" not in {n["code"] for n in health.open_notices()}


def test_a_health_problem_cannot_be_dismissed():
    """The rule the notices were built on: a health problem you can click away
    is a health problem you forget about. A dismissible tip must not become a
    way around that.

    Mutation-check this one. Remove the severity guard in `health.dismiss` and
    this test must fail.
    """
    health.record([{"code": "epg_stale", "severity": "bad", "title": "T",
                    "detail": "D", "hint": None}], 100,
                  owns=frozenset({"epg_stale"}))
    assert health.dismiss("epg_stale") is False
    assert "epg_stale" in {n["code"] for n in health.open_notices()}


def test_a_dismissed_tip_does_not_come_back_when_it_is_raised_again():
    health.record([{"code": "keys_available", "severity": health.TIP,
                    "title": "T", "detail": "D", "hint": None}], 200,
                  owns=frozenset({"keys_available"}))
    assert "keys_available" not in {n["code"] for n in health.open_notices()}


def test_a_real_fault_sorts_above_a_tip():
    health.record([{"code": "epg_stale", "severity": "bad", "title": "T",
                    "detail": "D", "hint": None},
                   {"code": "guide_short", "severity": "warn", "title": "T",
                    "detail": "D", "hint": None}], 300,
                  owns=frozenset({"epg_stale", "guide_short"}))
    order = [n["severity"] for n in health.open_notices()]
    assert order == sorted(order, key=lambda s: {"bad": 0, "warn": 1, "tip": 2}[s])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_health.py -q`
Expected: FAIL, `AttributeError: module 'app.health' has no attribute 'TIP'`

- [ ] **Step 3: Add the column, the severity and the guard**

In `app/db.py`, add to the `notices` table in `SCHEMA`:

```sql
    -- Only a `tip` may ever be set. A tip is a suggestion, and asking twice is
    -- nagging. A health problem is never dismissible: one you can click away
    -- is one you forget about.
    dismissed_at INTEGER
```

and to `MIGRATIONS`:

```python
    # A suggestion the user has waved off. Health problems can never set this;
    # `health.dismiss` refuses any severity but `tip`.
    ("notices", "dismissed_at", "INTEGER"),
```

In `app/health.py`:

```python
# A suggestion rather than a fault: something optional that would work better
# if it were set up. The only severity that may ever be dismissed.
TIP = "tip"

_SEVERITY_ORDER = {"bad": 0, "warn": 1, TIP: 2}


def dismiss(code: str) -> bool:
    """Wave off a suggestion. Refuses anything that is not a suggestion.

    The notices exist because a problem you can click away is a problem you
    forget about. A tip is not a problem, so it may go. Everything else stays,
    and this guard is what keeps the two apart.
    """
    row = db.one("SELECT severity FROM notices WHERE code = ?", (code,))
    if not row or row["severity"] != TIP:
        return False
    with db.tx() as c:
        c.execute("UPDATE notices SET dismissed_at = ? WHERE code = ?",
                  (int(time.time()), code))
    return True
```

Change `open_notices` to hide dismissed rows and sort tips last:

```python
def open_notices() -> list[dict]:
    """What is wrong right now, worst first. A dismissed tip is not shown."""
    rows = db.query(
        "SELECT * FROM notices WHERE resolved_at IS NULL AND dismissed_at IS NULL "
        "ORDER BY CASE severity WHEN 'bad' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
        "first_seen")
    return [dict(r) for r in rows]
```

Add `import time` to `app/health.py` if it is not already imported.

```python
# app/routes/notices.py
"""Waving off a suggestion."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import health

router = APIRouter()


@router.post("/api/notices/{code}/dismiss")
def api_dismiss(code: str):
    """Only a tip can be dismissed. A health problem is answered by fixing it."""
    if health.dismiss(code):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "That notice cannot be dismissed."},
                        status_code=400)
```

Register it in `app/web.py` beside the other routers.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_health.py tests/test_conventions.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add app/db.py app/health.py app/routes/notices.py app/web.py tests/test_health.py
git commit -m "A suggestion can be waved off. A health problem still cannot."
```

---

### Task 9: The keys tip, and where to get them

**Files:**
- Modify: `app/health.py` (`KEYS_AVAILABLE`, `TIP_CODES`, `keys_tip`)
- Modify: `app/sync.py` (record it)
- Modify: `app/templates/_settings.html` (a Sources section)
- Modify: `app/routes/_shared.py` or the settings route, to save the two keys
- Modify: `tests/test_health.py`

**Interfaces:**
- Consumes: `db.get_setting`, `db.query`.
- Produces:
  - `health.KEYS_AVAILABLE = "keys_available"`, `health.TIP_CODES = frozenset((KEYS_AVAILABLE,))`.
  - `health.keys_tip(has_tmdb: bool, has_sportsdb: bool, film_passes: int, team_passes: int) -> list[dict]`.
  - Settings keys `tmdb_key` and `sportsdb_key`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_health.py`:

```python
def test_no_tip_when_nothing_would_be_improved():
    """An install that only follows broadcast series gains nothing from either
    key, so it must never be nagged about them."""
    assert health.keys_tip(has_tmdb=False, has_sportsdb=False,
                           film_passes=0, team_passes=0) == []


def test_the_tip_names_the_source_and_what_it_buys():
    raised = health.keys_tip(has_tmdb=False, has_sportsdb=False,
                             film_passes=0, team_passes=2)
    assert len(raised) == 1
    assert raised[0]["severity"] == health.TIP
    text = raised[0]["detail"] + raised[0]["hint"]
    assert "TheSportsDB" in text
    assert "thesportsdb.com" in text


def test_no_tip_once_the_keys_are_set():
    assert health.keys_tip(has_tmdb=True, has_sportsdb=True,
                           film_passes=3, team_passes=3) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_health.py -q`
Expected: FAIL, `AttributeError: module 'app.health' has no attribute 'keys_tip'`

- [ ] **Step 3: Write the tip**

In `app/health.py`:

```python
KEYS_AVAILABLE = "keys_available"
TIP_CODES = frozenset((KEYS_AVAILABLE,))


def keys_tip(has_tmdb: bool, has_sportsdb: bool,
             film_passes: int, team_passes: int) -> list[dict]:
    """Offer a key only where one would actually add something.

    TVmaze needs no key at all, so series always work. This is only about the
    two optional ones, and only for someone already following the kind of
    thing they help with. Anyone else is not told about them.
    """
    wants = []
    if team_passes and not has_sportsdb:
        wants.append("TheSportsDB, for the rest of a team's published season, "
                     "free at thesportsdb.com")
    if film_passes and not has_tmdb:
        wants.append("TMDB, for films and their release dates, free at "
                     "themoviedb.org/settings/api")
    if not wants:
        return []
    return [{
        "code": KEYS_AVAILABLE,
        "severity": TIP,
        "title": "Two optional keys would fill in more of what you follow",
        "detail": ("CouchElephant already looks beyond the Plex guide using "
                   "TVmaze, which needs no key. " + " Also useful here: "
                   + "; ".join(wants) + "."),
        "hint": "Settings, then Sources. Both are free, and neither is required.",
    }]
```

In `app/sync.py`, after the miss sweep added in Task 7:

```python
    health.record(
        health.keys_tip(
            has_tmdb=bool(db.get_setting("tmdb_key")),
            has_sportsdb=bool(db.get_setting("sportsdb_key")),
            film_passes=db.one("SELECT COUNT(*) c FROM expectations "
                               "WHERE source = 'tmdb'")["c"],
            team_passes=db.one("SELECT COUNT(*) c FROM passes "
                               "WHERE kind = 'team'")["c"]),
        now, owns=health.TIP_CODES)
```

Add a **separate route**, not new fields on `settings_save`. `settings_save` at
`app/routes/settings.py:51` writes `plex_url`, `timezone`, `sync_minutes` and
`dry_run` unconditionally, so a form that posts to it without carrying every
one of those as a hidden field blanks them. `/settings/auth` already exists as
its own route for exactly this reason. Follow it.

In `app/routes/settings.py`, beside `settings_auth`:

```python
@router.post("/settings/sources")
def settings_sources(sportsdb_key: str = Form(""), tmdb_key: str = Form("")):
    """The two optional keys, on their own route.

    Not fields on `settings_save`: that one writes every Plex setting it is
    given, so a form posting to it without carrying them all as hidden fields
    would blank them.
    """
    db.set_setting("sportsdb_key", sportsdb_key.strip())
    # Masked on the way out, so a form echoing back asterisks must not be
    # saved over the real key. Same rule as `plex_token`.
    if tmdb_key.strip() and not tmdb_key.startswith("*"):
        db.set_setting("tmdb_key", tmdb_key.strip())
    return RedirectResponse("/settings", status_code=303)
```

In `app/templates/_settings.html`, add a section following the shape of the
existing `data-sec` sections:

```html
      <section data-sec="plex" data-tab="sources" data-tab-label="Sources" hidden>
        <h2>Extra sources</h2>
        <p class="sub">
          The Plex guide reaches about twelve days ahead, and no guide source
          anywhere reaches much further, because no broadcaster has decided that
          far out. These fill in what has been announced beyond it. TVmaze is
          always on and needs nothing. Both keys below are free and optional.
        </p>
        <form method="post" action="/settings/sources">
          <label>TheSportsDB key
            <input name="sportsdb_key" value="{{ settings.sportsdb_key or '' }}"
                   placeholder="optional">
          </label>
          <p class="sub">Raises the rate limits on published league schedules.
             Free at thesportsdb.com.</p>
          <label>TMDB key
            <input name="tmdb_key" type="password"
                   value="{{ '*' * 12 if settings.tmdb_key else '' }}"
                   placeholder="optional">
          </label>
          <p class="sub">Adds films and their release dates.
             Free at themoviedb.org/settings/api.</p>
          <button type="submit" class="primary">Save</button>
        </form>
      </section>
```

Add to `DEFAULTS` in `app/db.py`:

```python
    "sportsdb_key": "",
    "tmdb_key": "",
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests --ignore=tests/ui -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add app/health.py app/sync.py app/db.py app/templates/_settings.html
git commit -m "Offer the two optional keys, once, only where they would help"
```

---

### Task 10: Searching beyond the guide, and following what you find

**Files:**
- Modify: `app/routes/passes.py` (add `GET /api/announced` and `POST /api/announced/follow`)
- Create: `tests/test_announced.py`

**Interfaces:**
- Consumes: `tvmaze.search`, `thesportsdb.season`, `tmdb.search`, `expectations.store`, `expectations.render_when`, `db.get_setting`.
- Produces:
  - `GET /api/announced?q=` returning `{"ok": true, "announced": [...]}`, each item carrying `source`, `source_id`, `title`, `subtitle`, `network`, `when` and `precision`.
  - `POST /api/announced/follow` taking form fields `source`, `source_id`, `title`, and returning `{"ok": true, "pass_id": N}`.

**Why this is a second request and not part of `/api/series`:** the guide search
must answer at the speed it does today. A third party being slow, or down, must
never make the existing search slow or fail. So the page asks for guide results
first and asks for these separately.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_announced.py
"""Finding a thing the guide has never heard of, and following it."""
import pytest
from fastapi.testclient import TestClient

from app import db, expectations
from app.web import app
from tests import fake_sources


@pytest.fixture(scope="module")
def base():
    url = fake_sources.start()
    yield url
    fake_sources.stop()


@pytest.fixture
def client(base, monkeypatch):
    from app.sources import thesportsdb, tmdb, tvmaze
    monkeypatch.setattr(tvmaze, "BASE", base)
    monkeypatch.setattr(thesportsdb, "BASE", base)
    monkeypatch.setattr(tmdb, "BASE", base)
    return TestClient(app)


def test_search_finds_a_show_that_is_not_in_the_guide(client):
    body = client.get("/api/announced?q=Gobiligook").json()
    assert body["ok"] is True
    titles = [a["title"] for a in body["announced"]]
    assert "Gobiligook" in titles


def test_a_month_only_result_is_not_given_a_time(client):
    a = [x for x in client.get("/api/announced?q=Gobiligook").json()["announced"]
         if x["title"] == "Gobiligook"][0]
    assert a["precision"] == "month"
    assert "12:00" not in a["when"] and "00:00" not in a["when"]


def test_tmdb_is_skipped_with_no_key(client):
    db.set_setting("tmdb_key", "")
    body = client.get("/api/announced?q=Quorbis").json()
    assert body["ok"] is True
    assert not [a for a in body["announced"] if a["source"] == "tmdb"]


def test_following_a_result_makes_a_pass_and_an_expectation(client):
    r = client.post("/api/announced/follow",
                    data={"source": "tvmaze", "source_id": "99999",
                          "title": "Gobiligook"})
    body = r.json()
    assert body["ok"] is True
    pass_id = body["pass_id"]
    assert db.one("SELECT * FROM passes WHERE id = ?", (pass_id,))
    waiting = expectations.waiting(pass_id)
    assert [e["title"] for e in waiting] == ["Gobiligook"]


def test_following_the_same_thing_twice_does_not_pile_up(client):
    first = client.post("/api/announced/follow",
                        data={"source": "tvmaze", "source_id": "99999",
                              "title": "Gobiligook"}).json()["pass_id"]
    second = client.post("/api/announced/follow",
                         data={"source": "tvmaze", "source_id": "99999",
                               "title": "Gobiligook"}).json()["pass_id"]
    assert first == second
    assert len(expectations.waiting(first)) == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_announced.py -q`
Expected: FAIL, 404 on `/api/announced`

- [ ] **Step 3: Write the routes**

In `app/routes/passes.py`, after `api_series`:

```python
@router.get("/api/announced")
def api_announced(q: str = ""):
    """Things outside the Plex guide that match what was typed.

    A separate request from `/api/series` on purpose. The guide search has to
    answer as fast as it does today, and a third party being slow or down must
    never slow it down or break it. Each source is tried on its own, and one
    that fails is skipped rather than failing the lot.
    """
    q = (q or "").strip()
    if not q:
        return JSONResponse({"ok": True, "announced": []})
    tz = db.get_setting("timezone") or "UTC"
    found = []
    for call in (lambda: tvmaze.search(q),
                 lambda: tmdb.search(q, key=db.get_setting("tmdb_key") or "")):
        try:
            found.extend(call())
        except Exception:
            # One source being unreachable is not an error the user can act
            # on, and the others may still have the answer.
            continue
    return JSONResponse({"ok": True, "announced": [{
        "source": a.source, "source_id": a.source_id, "title": a.title,
        "subtitle": a.subtitle, "network": a.network, "precision": a.precision,
        "when": expectations.render_when(a.expected_at, a.precision, tz),
    } for a in found]})


@router.post("/api/announced/follow")
def api_announced_follow(source: str = Form(...), source_id: str = Form(...),
                         title: str = Form(...)):
    """Follow something the guide has not heard of yet.

    This creates an ordinary pass. It is the same object the rest of the app
    already understands, so once the guide carries the show nothing special
    has to happen for it to record.
    """
    title = title.strip()
    if not title:
        return JSONResponse({"ok": False, "error": "A title is required."},
                            status_code=400)
    existing = db.one("SELECT id FROM passes WHERE kind = 'series' "
                      "AND series_title = ?", (title,))
    if existing:
        pass_id = existing["id"]
    else:
        with db.tx() as c:
            cur = c.execute(
                "INSERT INTO passes (kind, series_title, uid, enabled, created_at) "
                "VALUES ('series', ?, ?, 1, ?)",
                (title, uuid.uuid4().hex, int(time.time())))
            pass_id = cur.lastrowid
    tz = db.get_setting("timezone") or "UTC"
    if source == "thesportsdb":
        items = thesportsdb.season(title, source_id,
                                   key=db.get_setting("sportsdb_key") or "")
    else:
        items = [a for a in tvmaze.search(title) if a.source_id == source_id] \
            or [a for a in tmdb.search(title, key=db.get_setting("tmdb_key") or "")
                if a.source_id == source_id]
    expectations.store(pass_id, items)
    return JSONResponse({"ok": True, "pass_id": pass_id, "tz": tz,
                         "waiting": len(expectations.waiting(pass_id))})
```

Add to the imports at the top of `app/routes/passes.py`:

```python
import uuid

from .. import expectations
from ..sources import thesportsdb, tmdb, tvmaze
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONPATH=. .venv/bin/pytest tests/test_announced.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add app/routes/passes.py tests/test_announced.py
git commit -m "Search past the end of the guide, and follow what turns up"
```

---

### Task 11: Seeing it, in the browser

**Files:**
- Modify: `app/routes/passes.py` (add `GET /api/expectations`)
- Modify: `app/templates/recordings.html`
- Modify: `app/static/css/app.css`
- Modify: `app/static/js/app.js` (render the plans, and the dismiss button)
- Modify: `app/templates/base.html` (dismiss control inside `.notice-menu`, tips only)
- Create: `tests/ui/test_expectations.py`

**Interfaces:**
- Consumes: `expectations.waiting`, `expectations.render_when`, `POST /api/notices/{code}/dismiss`.
- Produces: `GET /api/expectations` returning `{"ok": true, "rows": [...]}` with `when` already rendered.

**Careful:** `/recordings` is a **shell**. Its route returns `page(request,
"recordings.html")` and nothing else. Everything on that page is fetched from
`/api/schedule` and `/api/rules` by the browser. Adding template context to the
route would render nothing. Follow the pattern that is there.

- [ ] **Step 1: Write the failing test**

```python
# tests/ui/test_expectations.py
"""A thing you are waiting for has to look different from a thing you booked."""
import pytest

from app import db, expectations


@pytest.fixture
def waiting(page):
    with db.tx() as c:
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (1, 'tvmaze', 'ui-1', 'Gobiligook', 1804204800, "
                  "'month', 1)")
    page.goto("/recordings")
    page.wait_for_selector("header")
    yield page


def test_it_shows_as_a_plan_not_as_a_booking(waiting):
    row = waiting.locator('[data-expectation="ui-1"]')
    assert row.count() == 1
    assert "Gobiligook" in row.inner_text()


def test_a_month_is_shown_as_a_month(waiting):
    """The user must not read an invented midnight as a real broadcast time."""
    text = waiting.locator('[data-expectation="ui-1"]').inner_text()
    assert "2027" in text
    assert "12:00" not in text and "00:00" not in text


def test_a_tip_can_be_waved_off_but_a_fault_cannot(page):
    from app import health
    health.record([{"code": "keys_available", "severity": health.TIP,
                    "title": "Keys", "detail": "D", "hint": "H"},
                   {"code": "epg_stale", "severity": "bad", "title": "Stale",
                    "detail": "D", "hint": "H"}], 100,
                  owns=frozenset({"keys_available", "epg_stale"}))
    page.goto("/")
    page.wait_for_selector("#noticebtn")
    page.click("#noticebtn")
    page.wait_for_selector("#noticemenu.open")
    assert page.locator('[data-dismiss="keys_available"]').count() == 1
    assert page.locator('[data-dismiss="epg_stale"]').count() == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. COUCHELEPHANT_FAKE_ANCHOR=$(( $(date +%s)/1800*1800+1800 )) .venv/bin/pytest tests/ui/test_expectations.py -q`
Expected: FAIL, no element matching `[data-expectation="ui-1"]`

- [ ] **Step 3: Serve them, then render them client side**

In `app/routes/passes.py`, beside `api_schedule`:

```python
@router.get("/api/expectations")
def api_expectations():
    """What every pass is still waiting for, dates already rendered.

    Rendered here rather than in the browser because `precision` decides the
    format, and a client that guessed would invent a time nobody published.
    """
    tz = db.get_setting("timezone") or "UTC"
    return JSONResponse({"ok": True, "rows": [
        dict(e, when=expectations.render_when(e["expected_at"], e["precision"], tz))
        for e in expectations.waiting()]})
```

In `app/templates/recordings.html`, add an empty container above the schedule
card. It is filled by script, the way the rest of this page already is:

```html
<div class="card" id="plancard" hidden>
  <h2>Waiting for the guide</h2>
  <p class="sub">
    Announced, but not in the guide yet. Nothing here is booked. Each one
    becomes a real recording the moment Plex carries an airing for it.
  </p>
  <div id="planlist"></div>
</div>
```

In `app/static/js/app.js`:

```js
// The plans: things a pass is waiting for that the guide has not reached.
// Drawn from /api/expectations because /recordings is a shell, the same way
// the schedule and the rules on this page are.
(function () {
  var card = document.getElementById('plancard'),
      list = document.getElementById('planlist');
  if (!card || !list) return;
  fetch('/api/expectations')
    .then(function (r) { return r.json(); })
    .then(function (body) {
      var rows = (body && body.rows) || [];
      if (!rows.length) return;
      rows.forEach(function (e) {
        var el = document.createElement('div');
        el.className = 'plan';
        el.setAttribute('data-expectation', e.source_id);
        var bits = ['<span class="plan-title"></span>'];
        if (e.subtitle) bits.push('<span class="plan-sub"></span>');
        bits.push('<span class="plan-when"></span>',
                  '<span class="plan-src"></span>');
        if (e.missed_at) bits.push('<span class="pill warn">not in the guide</span>');
        el.innerHTML = bits.join('');
        // textContent, never innerHTML, for anything a third party sent us.
        el.querySelector('.plan-title').textContent = e.title;
        if (e.subtitle) el.querySelector('.plan-sub').textContent = e.subtitle;
        el.querySelector('.plan-when').textContent = e.when;
        el.querySelector('.plan-src').textContent = e.source;
        list.appendChild(el);
      });
      card.hidden = false;
    })
    .catch(function () { /* the page is still useful without this card */ });
})();
```

In `app/static/css/app.css`, using existing tokens only:

```css
/* A plan is not a booking. It is dashed and dimmer on purpose: nothing here
   will record until the guide carries it. */
.plan{display:flex;align-items:center;gap:10px;padding:8px 10px;margin:6px 0;
  border:1px dashed var(--line);border-radius:8px;color:var(--dim);font-size:13px}
.plan-title{color:var(--text);font-weight:600}
.plan-when{margin-left:auto}
.plan-src{font-size:11px;text-transform:uppercase;letter-spacing:.04em}
```

In `app/templates/base.html`, inside the `{% for n in notices %}` loop:

```html
{% if n.severity == 'tip' %}
  <button type="button" class="nm-dismiss" data-dismiss="{{ n.code }}">
    Not now
  </button>
{% endif %}
```

In `app/static/js/app.js`:

```js
// Only a tip carries a dismiss button. A health problem is answered by fixing
// it, so the server refuses to dismiss one even if a button appeared here.
(function () {
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-dismiss]');
    if (!b) return;
    e.preventDefault();
    e.stopPropagation();
    b.disabled = true;
    fetch('/api/notices/' + encodeURIComponent(b.dataset.dismiss) + '/dismiss',
          {method: 'POST'})
      .then(function (r) { if (r.ok) location.reload(); else b.disabled = false; })
      .catch(function () { b.disabled = false; });
  });
})();
```

And in the notices block of `app/static/css/app.css`, beside `.nm-syncbtn`:

```css
/* Only a tip carries this. It is quiet on purpose: waving off a suggestion
   should not look like the main thing the panel is for. */
.nm-dismiss{margin-top:6px;font:inherit;font-size:11px;padding:3px 8px;
  border-radius:6px;border:1px solid var(--line);background:none;
  color:var(--dim);cursor:pointer}
.nm-dismiss:hover{color:var(--text);border-color:var(--accent)}
.nm-dismiss:disabled{opacity:.55;cursor:default}
```

The `tip` severity also needs its own colours, beside `.nm-item.warn`:

```css
.nm-item.tip{border-left-color:var(--warn-bd)}
.nm-item.tip .nm-title{color:var(--warn-fg)}
.sync-badge.tip{background:var(--warn-fg)}
```

- [ ] **Step 4: Run both suites to verify they pass**

Run: `PYTHONPATH=. .venv/bin/pytest tests --ignore=tests/ui -q`
Run: `PYTHONPATH=. COUCHELEPHANT_FAKE_ANCHOR=$(( $(date +%s)/1800*1800+1800 )) .venv/bin/pytest tests/ui -q`
Expected: all passed in both

- [ ] **Step 5: Commit**

```bash
git add app/routes/passes.py app/templates/recordings.html app/templates/base.html \
        app/static/css/app.css app/static/js/app.js tests/ui/test_expectations.py
git commit -m "A plan looks like a plan, and only a suggestion can be waved off"
```

---

## After the last task

- [ ] Run `ruff check .` and both suites one final time.
- [ ] Update `CHANGELOG.md` under `## Unreleased`.
- [ ] Update `docs/ARCHITECTURE.md` with `app/sources/` and `app/expectations.py`.
- [ ] Deploy with `./deploy.sh` and confirm the Sources tab renders and the tip can be dismissed.
