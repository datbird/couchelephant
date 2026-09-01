"""A followed show has to learn about episodes announced after you followed it.

A series pass got its expectations exactly once, from the announced search, at
the moment it was made. `tvmaze.search` answers one row per SHOW carrying its
premiere date, and nothing ever asked again. So a followed show held one row for
ever. It never learned about next season, and for a show that had already
premiered that single row was a date in the past whose only future was to be
swept as missed.

Nothing had to be bought. TVmaze's episode list is free and unkeyed, exactly
like the search already in use. `fill_series_passes` is the series half of
`fill_team_passes` and keeps the same three disciplines:

  * ask rarely, because TVmaze allows ~20 calls per 10 seconds per address
  * date the attempt BEFORE the call, or a show with no dated episodes is
    re-asked on every sync for ever
  * refuse an uncertain identity rather than fill a pass with another
    programme's episodes

And one of its own: only FUTURE episodes are kept. The source hands back the
whole run, hundreds of rows for an old show, and an episode that already aired
is not something anyone is waiting for.
"""
import time

import pytest

from app import db, expectations
from app.sources import tvmaze
from tests import fake_sources

SHOW_ID = "99999"
NOW = 1740000000          # 2025, before every dated episode in the fake


@pytest.fixture
def sources(monkeypatch):
    url = fake_sources.start()
    monkeypatch.setattr(tvmaze, "BASE", url)
    yield url
    fake_sources.stop()


def _series_pass(title="Gobiligook", show_id=None, asked_at=None, asked_for=None):
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO passes (kind, series_title, uid, enabled, created_at, "
            "tvmaze_show_id, tvmaze_asked_at, tvmaze_asked_for) "
            "VALUES ('series', ?, ?, 1, 1, ?, ?, ?)",
            (title, f"uid-{title}-{time.time()}", show_id, asked_at, asked_for))
        return cur.lastrowid


# ----------------------------------------------------------------- the source

def test_the_episode_endpoint_is_asked_at_all(sources):
    """The whole gap: nothing ever called this."""
    got = tvmaze.episodes(SHOW_ID)
    assert [e.source_id for e in got] == ["ep-700000", "ep-700001", "ep-700002"]


def test_an_airstamp_earns_a_time_and_a_bare_date_does_not(sources):
    got = {e.source_id: e for e in tvmaze.episodes(SHOW_ID)}
    assert got["ep-700001"].precision == "time"
    assert got["ep-700002"].precision == "day"


def test_an_undated_episode_is_dropped_rather_than_guessed_at(sources):
    """A row with no date can never match an airing. It would sit for ever."""
    assert "ep-700003" not in {e.source_id for e in tvmaze.episodes(SHOW_ID)}


def test_an_episode_is_labelled_by_season_and_number(sources):
    got = {e.source_id: e for e in tvmaze.episodes(SHOW_ID)}
    assert got["ep-700001"].subtitle == "S01E01 Pilot"


def test_an_episode_id_can_never_be_read_as_a_show_id(sources):
    """`fill_series_passes` recovers the show id from the pass's own rows, so
    the two must not share a namespace."""
    assert all(e.source_id.startswith("ep-") for e in tvmaze.episodes(SHOW_ID))


def test_no_show_id_asks_nobody_anything(sources):
    assert tvmaze.episodes("") == []


# ------------------------------------------------------------------- the fill

def test_it_fills_a_pass_that_already_knows_its_show_id(sources):
    pid = _series_pass(show_id=SHOW_ID)
    assert expectations.fill_series_passes(now=NOW) == 1
    subs = {e["subtitle"] for e in expectations.waiting(pid)}
    assert "S01E01 Pilot" in subs
    assert "S01E02 The Second One" in subs


def test_an_episode_that_already_aired_is_not_something_you_wait_for(sources):
    pid = _series_pass(show_id=SHOW_ID)
    expectations.fill_series_passes(now=NOW)
    assert "ep-700000" not in {e["source_id"] for e in expectations.waiting(pid)}


def test_the_episode_carries_the_programme_name(sources):
    """The source only knows the episode; the pass knows the show."""
    pid = _series_pass(show_id=SHOW_ID)
    expectations.fill_series_passes(now=NOW)
    assert {e["title"] for e in expectations.waiting(pid)} == {"Gobiligook"}


def test_it_recovers_the_show_id_from_the_pass_s_own_rows(sources):
    """A pass made through the announced search already stored it."""
    pid = _series_pass(show_id=None)
    expectations.store(pid, tvmaze.search("Gobiligook"), now=NOW)
    assert expectations.fill_series_passes(now=NOW) == 1
    assert db.one("SELECT tvmaze_show_id t FROM passes WHERE id = ?",
                  (pid,))["t"] == SHOW_ID


def test_the_premiere_row_gives_way_to_real_episodes(sources):
    """It was dated at the premiere. For a show already on air that date is in
    the past, and its only future was to be reported missing for ever."""
    pid = _series_pass(show_id=SHOW_ID)
    expectations.store(pid, tvmaze.search("Gobiligook"), now=NOW)
    assert "99999" in {e["source_id"] for e in expectations.waiting(pid)}
    expectations.fill_series_passes(now=NOW)
    left = {e["source_id"] for e in expectations.waiting(pid)}
    assert "99999" not in left
    assert left == {"ep-700001", "ep-700002"}


def test_the_premiere_row_survives_when_no_episodes_are_dated(sources):
    """An unannounced show has none yet, and there the premiere IS the signal."""
    pid = _series_pass(title="Quorbis The Series", show_id="99998")
    expectations.store(pid, tvmaze.search("Quorbis"), now=NOW)
    expectations.fill_series_passes(now=NOW)
    assert "99998" in {e["source_id"] for e in expectations.waiting(pid)}


def test_a_premiere_row_already_bound_to_an_airing_is_never_dropped(sources):
    pid = _series_pass(show_id=SHOW_ID)
    expectations.store(pid, tvmaze.search("Gobiligook"), now=NOW)
    with db.tx() as c:
        c.execute("UPDATE expectations SET matched_guid = 'plex://x' "
                  "WHERE pass_id = ? AND source_id = '99999'", (pid,))
    expectations.fill_series_passes(now=NOW)
    assert db.one("SELECT COUNT(*) c FROM expectations WHERE pass_id = ? "
                  "AND source_id = '99999'", (pid,))["c"] == 1


def test_it_resolves_a_pass_made_from_the_guide_by_exact_title(sources):
    pid = _series_pass(title="Gobiligook")
    assert expectations.fill_series_passes(now=NOW) == 1
    assert db.one("SELECT tvmaze_show_id t FROM passes WHERE id = ?",
                  (pid,))["t"] == SHOW_ID


def test_a_near_match_fills_nothing(sources):
    """'Quorbis' returns 'Quorbis The Series'. Close is another programme."""
    pid = _series_pass(title="Quorbis")
    assert expectations.fill_series_passes(now=NOW) == 0
    assert expectations.waiting(pid) == []


def test_an_unknown_title_fills_nothing(sources):
    pid = _series_pass(title="Nothing By This Name")
    assert expectations.fill_series_passes(now=NOW) == 0
    assert expectations.waiting(pid) == []


# --------------------------------------------------- knowing when NOT to ask

def test_it_does_not_ask_again_within_a_day(sources):
    _series_pass(show_id=SHOW_ID, asked_at=NOW - 60, asked_for="Gobiligook")
    assert expectations.fill_series_passes(now=NOW) == 0


def test_it_asks_again_the_next_day(sources):
    _series_pass(show_id=SHOW_ID, asked_at=NOW - 86401, asked_for="Gobiligook")
    assert expectations.fill_series_passes(now=NOW) == 1


def test_a_rename_throws_away_the_stored_id(sources):
    """The id points at whoever the OLD name resolved to."""
    pid = _series_pass(title="Gobiligook", show_id="11111",
                       asked_at=NOW - 60, asked_for="Something Else")
    assert expectations.fill_series_passes(now=NOW) == 1
    assert db.one("SELECT tvmaze_show_id t FROM passes WHERE id = ?",
                  (pid,))["t"] == SHOW_ID


def test_the_attempt_is_dated_even_when_nothing_comes_back(sources):
    """Or an unknown show is looked up on every single sync, for ever."""
    pid = _series_pass(title="Nothing By This Name")
    expectations.fill_series_passes(now=NOW)
    assert db.one("SELECT tvmaze_asked_at a FROM passes WHERE id = ?",
                  (pid,))["a"] == NOW


def test_a_disabled_pass_is_left_alone(sources):
    pid = _series_pass(show_id=SHOW_ID)
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 0 WHERE id = ?", (pid,))
    assert expectations.fill_series_passes(now=NOW) == 0


def test_a_team_pass_is_never_filled_from_tvmaze(sources):
    """The sports source owns those, and a team is not a show."""
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_name, uid, enabled, created_at) "
                  "VALUES ('team', 'Gobiligook', 'uid-team-x', 1, 1)")
    assert expectations.fill_series_passes(now=NOW) == 0


def test_running_it_twice_refreshes_rather_than_piles_up(sources):
    pid = _series_pass(show_id=SHOW_ID)
    expectations.fill_series_passes(now=NOW)
    first = len(expectations.waiting(pid))
    with db.tx() as c:                       # let it ask again
        c.execute("UPDATE passes SET tvmaze_asked_at = NULL WHERE id = ?", (pid,))
    expectations.fill_series_passes(now=NOW)
    assert len(expectations.waiting(pid)) == first


def test_a_source_that_raises_does_not_date_itself_out_of_a_retry(sources,
                                                                  monkeypatch):
    """A provider having a bad day must not cost a whole day of silence.

    The attempt IS dated before the call, on purpose, so the guard against
    re-asking an unknown show holds. What must not happen is a crash reaching
    the sync.
    """
    pid = _series_pass(show_id=SHOW_ID)

    def boom(*a, **k):
        raise RuntimeError("tvmaze is down")

    monkeypatch.setattr(tvmaze, "episodes", boom)
    assert expectations.fill_series_passes(now=NOW) == 0
    assert expectations.waiting(pid) == []
