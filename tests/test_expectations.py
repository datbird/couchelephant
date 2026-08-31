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
    expectations.store(50, [_ann(source_id="s1")], now=10)
    expectations.store(50, [_ann(source_id="s1", title="X renamed")], now=20)
    rows = db.query("SELECT * FROM expectations WHERE pass_id = 50")
    assert len(rows) == 1
    assert rows[0]["title"] == "X renamed"
    assert rows[0]["updated_at"] == 20


def test_waiting_is_only_what_the_guide_has_not_confirmed():
    expectations.store(51, [_ann(source_id="w1"), _ann(source_id="w2")], now=1)
    with db.tx() as c:
        c.execute("UPDATE expectations SET matched_guid = 'plex://x' "
                  "WHERE source_id = 'w1'")
    assert [e["source_id"] for e in expectations.waiting(51)] == ["w2"]


def test_waiting_covers_every_pass_when_none_is_named():
    expectations.store(52, [_ann(source_id="a1")], now=1)
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


def _expect(pass_id, source_id, title, expected_at, precision="day"):
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
