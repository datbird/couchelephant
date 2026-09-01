"""The outside sources, against a fake that answers like the real ones."""
import pytest

from app import sources
from app.sources import thesportsdb, tmdb, tvmaze
from tests import fake_sources


@pytest.fixture(scope="module")
def base():
    url = fake_sources.start()
    yield url
    fake_sources.stop()


def test_a_full_date_keeps_its_day():
    assert sources.precision_of("2027-03-14")[1] == "day"


def test_a_month_only_date_is_not_promoted_to_a_day():
    """TVmaze answers "2027-03" for a show with no announced day. Reading that
    as the first of March invents a fact the source did not give."""
    when, how = sources.precision_of("2027-03")
    assert how == "month"
    assert when is not None


def test_a_datetime_keeps_its_time():
    assert sources.precision_of("2027-01-10 20:15:00")[1] == "time"


def test_an_absent_date_is_not_a_date():
    assert sources.precision_of("") == (None, "year")
    assert sources.precision_of(None) == (None, "year")


def test_a_date_nobody_can_parse_is_not_guessed_at():
    assert sources.precision_of("next spring sometime") == (None, "year")


def test_tvmaze_finds_an_announced_series(base):
    hits = tvmaze.search("Gobiligook", base=base)
    assert len(hits) == 1
    found = hits[0]
    assert found.source == "tvmaze"
    assert found.source_id == "99999"
    assert found.title == "Gobiligook"
    assert found.network == "ABC"
    assert found.precision == "month"


def test_tvmaze_falls_back_to_the_web_channel_for_a_streaming_show(base):
    """The network it names is the producer and not your aerial. That is fine:
    an expectation only records once the guide confirms a real airing."""
    found = tvmaze.search("Quorbis", base=base)[0]
    assert found.network == "A Streamer"


def test_tvmaze_returns_nothing_for_a_title_that_does_not_exist(base):
    assert tvmaze.search("Nothing By This Name", base=base) == []


def test_tvmaze_does_not_call_out_for_an_empty_query(base):
    assert tvmaze.search("   ", base=base) == []


def test_a_scheduled_game_with_a_kickoff_keeps_the_time(base):
    games = thesportsdb.season("Ravens", "4391", key="sub-key", base=base)
    first = [g for g in games if g.source_id == "2000001"][0]
    assert first.precision == "time"
    assert first.subtitle == "Ravens vs Falcons"
    assert first.title == "Ravens"


def test_a_scheduled_game_with_no_kickoff_is_only_a_day(base):
    """The league has announced the date but not the kickoff. Showing 12:00 AM
    would be a time nobody published."""
    games = thesportsdb.season("Ravens", "4391", key="sub-key", base=base)
    second = [g for g in games if g.source_id == "2000002"][0]
    assert second.precision == "day"


def test_only_this_team_is_returned(base):
    """The season endpoint answers the whole league. A pass follows one team."""
    ids = {g.source_id for g in thesportsdb.season("Ravens", "4391", key="sub-key", base=base)}
    assert ids == {"2000001", "2000002"}


def test_the_away_side_counts_as_the_team_playing(base):
    ids = {g.source_id for g in thesportsdb.season("Pilots", "4391", key="sub-key", base=base)}
    assert ids == {"2000002", "2000003"}


def test_no_league_means_no_call(base):
    assert thesportsdb.season("Ravens", "", key="sub-key", base=base) == []


def test_tmdb_is_silent_without_a_key(base):
    """It is optional. With no key it answers nothing rather than raising, so
    search still works for everyone who never set one."""
    assert tmdb.search("Quorbis", key="", base=base) == []
    assert tmdb.search("Quorbis", key="   ", base=base) == []


def test_tmdb_finds_an_unreleased_film(base):
    hits = tmdb.search("Quorbis", key="demo-key", base=base)
    assert len(hits) == 1
    assert hits[0].source == "tmdb"
    assert hits[0].title == "Quorbis Rising"
    assert hits[0].precision == "day"


def test_the_free_tier_gives_no_season_at_all(base):
    """Measured against the live API on 2026-08-31. `eventsseason` on the
    public test key answered five events for the whole league and none for the
    team asked about. A subscriber key is what buys a season, and the docs and
    the settings copy now say so."""
    assert thesportsdb.season("Ravens", "4391", base=base) == []


def test_the_free_tier_still_gives_the_next_game(base):
    """Thin, but not nothing. This is what a user with no key actually gets."""
    games = thesportsdb.upcoming("134931", base=base)
    assert len(games) == 1
    assert games[0].subtitle == "Ravens vs Falcons"
    assert games[0].precision == "time"


def test_a_team_can_be_resolved_to_its_league(base):
    """A pass knows a team name. The season endpoint wants a league id."""
    found = thesportsdb.team("Ravens", base=base)
    assert found["team_id"] == "134931"
    assert found["league_id"] == "4391"


def test_an_unknown_team_resolves_to_nothing_rather_than_guessing(base):
    assert thesportsdb.team("Not A Real Team", base=base) is None
    assert thesportsdb.team("", base=base) is None
