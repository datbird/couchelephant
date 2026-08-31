"""What a pass is still waiting for.

An expectation is an intention, never a booking. Only a guide airing carries a
channel, so only a guide airing can be recorded. These tests hold that line.
"""
import sqlite3

import pytest

from app import db


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
