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
