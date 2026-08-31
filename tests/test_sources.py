"""The outside sources, against a fake that answers like the real ones."""
import pytest

from app import sources
from app.sources import tvmaze
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
