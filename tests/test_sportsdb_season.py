"""The season call has to name a season, or it gets the wrong decade.

`season()` sent `eventsseason.php?id=<league>` and no `s` at all. That does not
mean "the current season". The real API answers with its EARLIEST: measured live
on 2026-09-01, `eventsseason.php?id=4391` came back with NFL games from
2007-09-06.

It hid behind the free tier. Free caps that endpoint at five league-wide rows,
and after filtering to one team the answer was usually nothing either way, so
the wrong year looked exactly like the thin free answer. The cost lands on
whoever buys a subscriber key: they would have paid for a season and been handed
2007.

The season string cannot be composed from a clock. The NFL calls it "2026"; a
football league calls it "2025-2026". The league states its own in
`strCurrentSeason`, which is free to read, so that is what gets asked for.
"""
import pytest

from app.sources import thesportsdb
from tests import fake_sources

NFL = "4391"


@pytest.fixture
def api(monkeypatch):
    url = fake_sources.start()
    monkeypatch.setattr(thesportsdb, "BASE", url)
    yield url
    fake_sources.stop()


def test_it_reads_the_league_s_own_season(api):
    assert thesportsdb.current_season(NFL) == "2027"


def test_an_unknown_league_yields_no_season_rather_than_a_guess(api):
    assert thesportsdb.current_season("999999") == ""


def test_no_league_asks_nobody_anything(api):
    assert thesportsdb.current_season("") == ""


def test_the_season_call_actually_sends_one(api):
    """The whole bug in one assertion. It used to send nothing here."""
    thesportsdb.season("Ravens", NFL, key="paid")
    assert ("eventsround", "1", "2027") in fake_sources.ASKED


def test_it_still_asks_when_the_league_will_not_say(api):
    """An unknown season is not a reason to fetch nothing at all. Asking
    without one is exactly what it did before, which is no worse."""
    thesportsdb.season("Ravens", "999999", key="paid")
    assert ("eventsround", "1", "") in fake_sources.ASKED


def test_a_season_answer_is_still_filtered_to_the_team(api):
    got = thesportsdb.season("Ravens", NFL, key="paid")
    assert {a.source_id for a in got} == {"2000001", "2000002"}


def test_it_walks_past_the_first_round(api):
    """`eventsseason.php` is capped and `eventsround.php` is not, so the season
    is the walk. Stopping at round one would put the cap back by hand."""
    thesportsdb.season("Ravens", NFL, key="paid")
    rounds = [r for r in fake_sources.ASKED if r[0] == "eventsround"]
    assert [r[1] for r in rounds[:3]] == ["1", "2", "3"]


def test_the_walk_stops_after_consecutive_empty_rounds(api):
    """A round count is per sport, so the stop rule is silence, not a number.
    Without it every league would cost the full backstop in requests."""
    thesportsdb.season("Ravens", NFL, key="paid")
    rounds = [int(r[1]) for r in fake_sources.ASKED if r[0] == "eventsround"]
    # Three events in the fixture, then _QUIET_ROUNDS empties, and stop.
    assert max(rounds) == len(fake_sources.SPORTSDB_EVENTS) + thesportsdb._QUIET_ROUNDS
    assert max(rounds) < thesportsdb._MAX_ROUNDS


def test_the_free_key_is_the_one_that_answers(api):
    """"3" is documented too and returns 5 rows where "123" returns a full
    16-game NFL week. Measured live; do not "tidy" this back to 3."""
    assert thesportsdb.FREE_KEY == "123"


def test_a_league_lookup_that_fails_does_not_take_the_season_with_it(api,
                                                                    monkeypatch):
    """A miss is not a season, and it must not be an exception either."""
    monkeypatch.setattr(thesportsdb, "BASE", "http://127.0.0.1:1")
    assert thesportsdb.current_season(NFL) == ""
