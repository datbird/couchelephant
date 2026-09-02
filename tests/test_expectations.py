"""What a pass is still waiting for.

An expectation is an intention, never a booking. Only a guide airing carries a
channel, so only a guide airing can be recorded. These tests hold that line.
"""
import sqlite3

import pytest

from app import db, expectations


def test_the_table_holds_a_soft_date():
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
    """Re-importing a season must refresh its rows, not pile up copies."""
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


def test_two_passes_may_wait_on_the_same_thing():
    """The constraint is per pass. Two people following the same game is not a
    duplicate, it is two passes."""
    with db.tx() as c:
        for pass_id in (8, 9):
            c.execute(
                "INSERT INTO expectations (pass_id, source, source_id, title, "
                "precision, updated_at) VALUES (?,?,?,?,?,?)",
                (pass_id, "thesportsdb", "shared-1", "A at B", "time", 1))
    assert len(db.query("SELECT 1 FROM expectations WHERE source_id = 'shared-1'")) == 2


def _ann(**kw):
    from app.sources import Announcement
    base = dict(source="tvmaze", source_id="1", title="X", precision="day",
                expected_at=1804204800)
    base.update(kw)
    return Announcement(**base)


def test_storing_twice_updates_rather_than_duplicating():
    _ensure_pass(50)
    expectations.store(50, [_ann(source_id="s1")], now=10)
    _ensure_pass(50)
    expectations.store(50, [_ann(source_id="s1", title="X renamed")], now=20)
    rows = db.query("SELECT * FROM expectations WHERE pass_id = 50")
    assert len(rows) == 1
    assert rows[0]["title"] == "X renamed"
    assert rows[0]["updated_at"] == 20


def test_waiting_is_only_what_the_guide_has_not_confirmed():
    _ensure_pass(51)
    expectations.store(51, [_ann(source_id="w1"), _ann(source_id="w2")], now=1)
    with db.tx() as c:
        c.execute("UPDATE expectations SET matched_guid = 'plex://x' "
                  "WHERE source_id = 'w1'")
    assert [e["source_id"] for e in expectations.waiting(51)] == ["w2"]


def test_waiting_covers_every_pass_when_none_is_named():
    _ensure_pass(52)
    expectations.store(52, [_ann(source_id="a1")], now=1)
    _ensure_pass(53)
    expectations.store(53, [_ann(source_id="a2")], now=1)
    assert len(expectations.waiting()) == 2


def test_a_month_renders_as_a_month_and_never_as_a_midnight():
    """The whole point of precision. Anything showing 12:00 AM for a date
    nobody published is telling the user something untrue."""
    when = expectations.render_when(1804204800, "month", "UTC")
    assert "2027" in when
    assert "12:00" not in when
    assert "00:00" not in when
    assert ":" not in when


def test_a_kickoff_renders_with_its_time():
    assert ":" in expectations.render_when(1804204800, "time", "UTC")


def test_a_day_renders_without_a_time():
    when = expectations.render_when(1804204800, "day", "UTC")
    assert ":" not in when
    assert "2027" in when


def test_no_date_says_so_rather_than_showing_the_epoch():
    assert expectations.render_when(None, "year", "UTC") == "date not announced"
    assert expectations.render_when(0, "day", "UTC") == "date not announced"


def test_a_broken_timezone_falls_back_rather_than_raising():
    """A bad setting must not take the page down."""
    assert expectations.render_when(1804204800, "day", "Not/AZone")


def _guide_row(guid, title, airing_id, begins_at):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, section) "
                  "VALUES (?,?,'shows')", (guid, title))
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, begins_at, "
                  "channel_vcn) VALUES (?,?,?,'9.1')",
                  (airing_id, guid, begins_at))


def _ensure_pass(pass_id):
    """An expectation is only live while its pass is. Tests that pick a
    pass_id have to make the pass, the way the app does."""
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO passes (id, kind, series_title, uid, "
                  "enabled, created_at) VALUES (?, 'series', ?, ?, 1, 1)",
                  (pass_id, f"pass-{pass_id}", f"uid-{pass_id}"))


def _expect(pass_id, source_id, title, expected_at, precision="day"):
    _ensure_pass(pass_id)
    with db.tx() as c:
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) VALUES (?,?,?,?,?,?,1)",
                  (pass_id, "tvmaze", source_id, title, expected_at, precision))


WHEN = 1804204800


def test_promotion_binds_the_expectation_to_the_real_airing():
    _guide_row("plex://x/p1", "Gobiligook", "a-p1", WHEN + 3600)
    _expect(60, "p1", "Gobiligook", WHEN)
    assert expectations.promote(now=WHEN) == 1
    row = db.one("SELECT * FROM expectations WHERE source_id = 'p1'")
    assert row["matched_guid"] == "plex://x/p1"
    assert row["matched_at"] == WHEN


def test_a_promoted_expectation_stops_waiting():
    _guide_row("plex://x/p2", "Gobiligook", "a-p2", WHEN + 3600)
    _expect(60, "p2", "Gobiligook", WHEN)
    expectations.promote(now=WHEN)
    assert expectations.waiting(60) == []


def test_a_different_show_at_the_same_time_is_not_a_match():
    _guide_row("plex://x/p3", "Something Else", "a-p3", WHEN + 3600)
    _expect(61, "p3", "Not In The Guide", WHEN)
    expectations.promote(now=WHEN)
    assert db.one("SELECT matched_guid FROM expectations "
                  "WHERE source_id = 'p3'")["matched_guid"] is None


def test_the_right_show_far_outside_the_window_is_not_a_match():
    """A day-precision guess allows a couple of days of slip, not a year. A
    title that happens to repeat next season is a different broadcast."""
    _guide_row("plex://x/p4", "Gobiligook", "a-p4", WHEN + 300 * 86400)
    _expect(62, "p4", "Gobiligook", WHEN)
    expectations.promote(now=WHEN)
    assert db.one("SELECT matched_guid FROM expectations "
                  "WHERE source_id = 'p4'")["matched_guid"] is None


def test_a_month_precision_guess_allows_the_whole_month():
    _guide_row("plex://x/p5", "Gobiligook", "a-p5", WHEN + 20 * 86400)
    _expect(63, "p5", "Gobiligook", WHEN, precision="month")
    assert expectations.promote(now=WHEN) == 1


def test_an_expectation_with_no_date_is_left_alone():
    """Nothing to match against. Guessing would bind it to the first thing
    with the same name, which could be years away."""
    _guide_row("plex://x/p6", "Gobiligook", "a-p6", WHEN)
    _expect(64, "p6", "Gobiligook", None)
    assert expectations.promote(now=WHEN) == 0


def test_the_series_title_matches_when_the_episode_title_differs():
    """The guide names the episode. A pass follows the series."""
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, "
                  "grandparent_title, section) VALUES "
                  "('plex://x/p7', 'Pilot', 'Gobiligook', 'shows')")
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, begins_at, "
                  "channel_vcn) VALUES ('a-p7', 'plex://x/p7', ?, '9.1')",
                  (WHEN + 3600,))
    _expect(65, "p7", "Gobiligook", WHEN)
    assert expectations.promote(now=WHEN) == 1


def test_a_show_the_guide_reached_and_never_carried_is_reported():
    _expect(70, "m1", "Never Aired", WHEN)
    raised = expectations.sweep_misses(guide_ends_at=WHEN + 5 * 86400,
                                       now=WHEN + 5 * 86400)
    assert len(raised) == 1
    assert "Never Aired" in raised[0]["detail"]
    assert raised[0]["severity"] == "warn"
    assert db.one("SELECT missed_at FROM expectations "
                  "WHERE source_id = 'm1'")["missed_at"]


def test_a_miss_keeps_looking_rather_than_being_deleted():
    """A show can slip a week. Deleting the expectation would be giving up on
    it quietly, which is the one thing this feature exists to prevent."""
    _expect(70, "m1", "Never Aired", WHEN)
    expectations.sweep_misses(guide_ends_at=WHEN + 5 * 86400, now=WHEN + 5 * 86400)
    assert db.one("SELECT 1 FROM expectations WHERE source_id = 'm1'") is not None
    assert [e["source_id"] for e in expectations.waiting(70)] == ["m1"]


def test_nothing_is_reported_while_the_guide_has_not_got_there_yet():
    """Silence before the date is the guide being short, not the show being
    missing. Warning then would cry wolf every day for months."""
    _expect(71, "m2", "Still Coming", WHEN)
    assert expectations.sweep_misses(guide_ends_at=WHEN - 86400, now=WHEN) == []
    assert db.one("SELECT missed_at FROM expectations "
                  "WHERE source_id = 'm2'")["missed_at"] is None


def test_nothing_is_reported_when_the_guide_end_is_unknown():
    _expect(72, "m3", "Unknown Guide End", WHEN)
    assert expectations.sweep_misses(guide_ends_at=None, now=WHEN) == []


def test_a_promoted_expectation_is_never_called_missing():
    _guide_row("plex://x/m4", "Arrived", "a-m4", WHEN + 3600)
    _expect(73, "m4", "Arrived", WHEN)
    expectations.promote(now=WHEN)
    assert expectations.sweep_misses(guide_ends_at=WHEN + 5 * 86400,
                                     now=WHEN + 5 * 86400) == []


def test_many_misses_are_one_notice_and_not_a_pile():
    for n in range(5):
        _expect(74, f"many-{n}", f"Show {n}", WHEN)
    raised = expectations.sweep_misses(guide_ends_at=WHEN + 5 * 86400,
                                       now=WHEN + 5 * 86400)
    assert len(raised) == 1
    assert "and others" in raised[0]["detail"]


@pytest.fixture
def sportsdb(monkeypatch):
    from app.sources import thesportsdb
    from tests import fake_sources
    url = fake_sources.start()
    monkeypatch.setattr(thesportsdb, "BASE", url)
    yield url
    fake_sources.stop()


def _team_pass(name="Ravens"):
    with db.tx() as c:
        cur = c.execute("INSERT INTO passes (kind, team_name, uid, enabled, "
                        "created_at) VALUES ('team', ?, 'uid-1', 1, 1)", (name,))
        return cur.lastrowid


def test_a_team_pass_that_predates_this_feature_fills_itself(sportsdb):
    """The whole reason this exists. An existing Chiefs pass had no
    expectations and nothing back-filled it.

    It gets the SEASON, not one game. `season()` walks `eventsround.php`, which
    is not capped the way `eventsseason.php` is, so a followed team shows the
    months of fixtures its league has published rather than the single next
    kickoff the old call could reach."""
    pass_id = _team_pass()
    assert expectations.fill_team_passes(now=1) >= 1
    waiting = expectations.waiting(pass_id)
    assert [e["subtitle"] for e in waiting] == ["Ravens vs Falcons",
                                                "Ravens vs Pilots"]


def test_the_team_ids_are_resolved_once_and_remembered(sportsdb):
    pass_id = _team_pass()
    expectations.fill_team_passes(now=1)
    row = db.one("SELECT sportsdb_team_id, sportsdb_league_id FROM passes "
                 "WHERE id = ?", (pass_id,))
    assert row["sportsdb_team_id"] == "134931"
    assert row["sportsdb_league_id"] == "4391"


def test_a_team_nobody_recognises_is_left_alone(sportsdb):
    """Resolving to the closest match would fill the pass with somebody
    else's games."""
    pass_id = _team_pass("Not A Real Team")
    expectations.fill_team_passes(now=1)
    assert expectations.waiting(pass_id) == []


def test_it_does_not_ask_again_the_same_day(sportsdb):
    """A season does not change hourly, and the free tier is rate limited."""
    _team_pass()
    assert expectations.fill_team_passes(now=1) >= 1
    assert expectations.fill_team_passes(now=2) == 0


def test_it_asks_again_the_next_day(sportsdb):
    _team_pass()
    expectations.fill_team_passes(now=1)
    assert expectations.fill_team_passes(now=1 + 2 * 86400) >= 1


def test_a_disabled_pass_is_not_filled(sportsdb):
    pass_id = _team_pass()
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 0 WHERE id = ?", (pass_id,))
    expectations.fill_team_passes(now=1)
    assert expectations.waiting(pass_id) == []


def test_a_team_nobody_recognises_is_not_asked_about_again_all_day(sportsdb):
    """The hole this closes: with no games stored there was nothing to date
    the attempt by, so an unknown team was looked up on EVERY sync. On a rate
    limited free tier that is the worst thing this could do."""
    _team_pass("Not A Real Team")
    expectations.fill_team_passes(now=1)
    asked = db.one("SELECT sportsdb_asked_at FROM passes")["sportsdb_asked_at"]
    assert asked == 1
    expectations.fill_team_passes(now=2)
    assert db.one("SELECT sportsdb_asked_at FROM passes")["sportsdb_asked_at"] == 1


def test_a_team_that_returns_no_games_is_not_asked_about_again_all_day(sportsdb):
    """Same hole, one step later. Out of season a real team answers nothing."""
    from app.sources import thesportsdb
    _team_pass()
    expectations.fill_team_passes(now=1)
    with db.tx() as c:
        c.execute("DELETE FROM expectations")
        c.execute("UPDATE passes SET sportsdb_asked_at = 1")
    calls = []
    real = thesportsdb.upcoming

    def counted(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    thesportsdb.upcoming = counted
    try:
        expectations.fill_team_passes(now=2)
    finally:
        thesportsdb.upcoming = real
    assert calls == []


def test_renaming_the_team_resolves_it_again(sportsdb):
    """`resolve_team_passes` adopts Plex's own spelling, so a pass can be
    renamed under us. Keeping the old ids would fill it with another team."""
    pass_id = _team_pass()
    expectations.fill_team_passes(now=1)
    with db.tx() as c:
        c.execute("UPDATE passes SET team_name = 'Not A Real Team' WHERE id = ?",
                  (pass_id,))
    expectations.fill_team_passes(now=2)
    row = db.one("SELECT sportsdb_team_id, sportsdb_asked_for FROM passes "
                 "WHERE id = ?", (pass_id,))
    assert row["sportsdb_asked_for"] == "Not A Real Team"
    assert row["sportsdb_team_id"] is None


def test_adding_a_key_refills_at_once_rather_than_tomorrow(sportsdb):
    """Somebody who pays should see the difference now, not in a day.

    The refill is what this pins, not the size of the answer. Since the season
    is walked round by round the free tier already returns a season, so a key
    buys rate limit and depth rather than the feature itself."""
    pass_id = _team_pass()
    expectations.fill_team_passes(now=1)
    before = db.one("SELECT sportsdb_asked_with a FROM passes WHERE id = ?",
                    (pass_id,))["a"]
    db.set_setting("sportsdb_key", "sub-key")
    expectations.fill_team_passes(now=2)
    after = db.one("SELECT sportsdb_asked_with a FROM passes WHERE id = ?",
                   (pass_id,))["a"]
    assert (before, after) == ("free", "key")
    assert len(expectations.waiting(pass_id)) >= 1


def test_removing_a_key_also_counts_as_a_change(sportsdb):
    pass_id = _team_pass()
    db.set_setting("sportsdb_key", "sub-key")
    expectations.fill_team_passes(now=1)
    with_key = db.one("SELECT sportsdb_asked_with FROM passes "
                      "WHERE id = ?", (pass_id,))["sportsdb_asked_with"]
    db.set_setting("sportsdb_key", "")
    expectations.fill_team_passes(now=2)
    without = db.one("SELECT sportsdb_asked_with FROM passes "
                     "WHERE id = ?", (pass_id,))["sportsdb_asked_with"]
    assert with_key != without


def _sports_row(guid, title, teams, airing_id, begins_at):
    """A guide row shaped the way the real guide shapes sport: the programme
    title is the matchup, the grandparent is the league, and the teams live in
    a JSON array."""
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, "
                  "grandparent_title, section, teams) VALUES (?,?,?,'sports',?)",
                  (guid, title, "NFL Football", db.js(teams)))
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, begins_at, "
                  "channel_vcn) VALUES (?,?,?,'9.1')", (airing_id, guid, begins_at))


def _team_expect(pass_id, source_id, team_name, expected_at):
    _ensure_pass(pass_id)
    with db.tx() as c:
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "subtitle, expected_at, precision, updated_at) "
                  "VALUES (?, 'thesportsdb', ?, ?, ?, ?, 'time', 1)",
                  (pass_id, source_id, team_name,
                   f"{team_name} vs Someone", expected_at))


def test_a_team_expectation_matches_the_game_in_the_guide():
    """The guide titles the programme "Chiefs at Broncos" and puts the league
    in grandparent_title. Comparing the team name against either would never
    match, so a whole season would sit as a plan and then be called missing.
    The teams live in a JSON array, which is what to match on."""
    _sports_row("plex://x/g1", "Kansas City Chiefs at Denver Broncos",
                [{"id": 245, "name": "Kansas City Chiefs"},
                 {"id": 99, "name": "Denver Broncos"}],
                "a-g1", WHEN + 1800)
    _team_expect(80, "g1", "Kansas City Chiefs", WHEN)
    assert expectations.promote(now=WHEN) == 1
    assert db.one("SELECT matched_guid FROM expectations WHERE source_id = 'g1'"
                  )["matched_guid"] == "plex://x/g1"


def test_a_team_expectation_does_not_match_somebody_elses_game():
    _sports_row("plex://x/g2", "Chicago Bears at Green Bay Packers",
                [{"id": 1, "name": "Chicago Bears"},
                 {"id": 2, "name": "Green Bay Packers"}],
                "a-g2", WHEN + 1800)
    _team_expect(81, "g2", "Kansas City Chiefs", WHEN)
    assert expectations.promote(now=WHEN) == 0


def test_a_team_expectation_matches_however_the_guide_spells_it():
    """`tident` folds case, accents and punctuation and nothing else. It is the
    same fold the pass engine already uses to decide what to record."""
    _sports_row("plex://x/g3", "Atletico Madrid at Someone",
                [{"id": 7, "name": "Atlético Madrid"}], "a-g3", WHEN + 1800)
    _team_expect(82, "g3", "Atletico Madrid", WHEN)
    assert expectations.promote(now=WHEN) == 1


def test_a_series_expectation_still_matches_on_its_title():
    """The team rule must not break the series rule."""
    _guide_row("plex://x/g4", "Gobiligook", "a-g4", WHEN + 1800)
    _expect(83, "g4", "Gobiligook", WHEN)
    assert expectations.promote(now=WHEN) == 1


def test_deleting_a_pass_does_not_leave_its_plans_on_the_screen():
    """Otherwise a season you stopped following shows as waiting for ever, and
    then gets reported missing, for a pass that no longer exists."""
    pass_id = _team_pass()
    expectations.store(pass_id, [_ann(source_id="orphan-1")], now=1)
    assert expectations.waiting()
    with db.tx() as c:
        c.execute("DELETE FROM passes WHERE id = ?", (pass_id,))
    assert expectations.waiting() == []


def test_disabling_a_pass_takes_its_plans_off_the_screen_too():
    """A pass you turned off should stop showing you what it was going to do."""
    pass_id = _team_pass()
    expectations.store(pass_id, [_ann(source_id="off-1")], now=1)
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 0 WHERE id = ?", (pass_id,))
    assert expectations.waiting() == []


def test_a_deleted_pass_is_never_reported_as_missing():
    pass_id = _team_pass()
    expectations.store(pass_id, [_ann(source_id="orphan-2")], now=1)
    with db.tx() as c:
        c.execute("DELETE FROM passes WHERE id = ?", (pass_id,))
    assert expectations.sweep_misses(guide_ends_at=WHEN + 99 * 86400,
                                     now=WHEN + 99 * 86400) == []
