"""Upgrading an install that already has data in it.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that exists, so a column
added to the schema string alone never reaches anybody who is already running
the app. It reaches a fresh install and nowhere else, which is the worst way
for it to fail: it works on the machine it was written on.

`db.MIGRATIONS` is the mechanism, and this is the check on it. The baseline is
`tests/schemas/v1.0.1.sql`, the schema exactly as the published version shipped
it, taken from the tag and frozen.

It has to be a real historical schema and not one derived from `MIGRATIONS`.
Derived, the check is circular: a migration nobody wrote is also a column
nobody strips, so the old database already has it and the upgrade passes while
proving nothing. That was the first version of this file, and removing a
migration entry did not fail it.
"""
import sqlite3
from pathlib import Path

from app import db

BASELINE = Path(__file__).parent / "schemas" / "v1.0.1.sql"


def _columns(con) -> dict[str, set[str]]:
    """Every table and its columns, asked of SQLite rather than parsed.

    Parsing the schema string means reinventing a SQL parser, and the first
    thing it gets wrong is a table constraint: `FOREIGN KEY (...)` reads as a
    column called FOREIGN.
    """
    out = {}
    for (table,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall():
        out[table] = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    return out


def _old_install(path) -> None:
    """A database as CouchElephant 1.0.1 left it, with rows in it."""
    con = sqlite3.connect(path)
    con.executescript(BASELINE.read_text())
    con.execute("INSERT INTO programs (guid, title, section, teams) "
                "VALUES ('plex://episode/old', 'Old Game', 'sports', '[]')")
    con.execute("INSERT INTO passes (kind, team_name, enabled, created_at) "
                "VALUES ('team', 'Kansas City Chiefs', 1, 1)")
    con.execute("INSERT INTO sync_log (started_at, ended_at, ok, detail) "
                "VALUES (1, 2, 1, 'an older sync')")
    con.commit()
    con.close()


def test_the_baseline_is_genuinely_older_than_today(tmp_path):
    """Guard on the guard. If the baseline already had every current column,
    every test below would pass while proving nothing at all."""
    old = sqlite3.connect(tmp_path / "b.db")
    old.executescript(BASELINE.read_text())
    before = _columns(old)
    old.close()

    fresh = sqlite3.connect(tmp_path / "f.db")
    fresh.executescript(db.SCHEMA)
    after = _columns(fresh)
    fresh.close()

    added = {t: sorted(c - before.get(t, set())) for t, c in after.items()}
    added = {t: c for t, c in added.items() if c}
    assert added, ("the frozen baseline matches today's schema, so this file "
                   "is checking nothing. Freeze a newer one only when a release "
                   "goes out, never to make a failure go away.")


def test_every_column_in_the_schema_survives_an_upgrade(tmp_path, monkeypatch):
    """The whole point. A column in the schema and not in MIGRATIONS works on
    a fresh install and is missing on every existing one."""
    path = tmp_path / "old.db"
    _old_install(path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    if hasattr(db._local, "conn"):
        del db._local.conn

    db.init()
    upgraded = _columns(db.connect())
    del db._local.conn

    # What a brand new install gets, asked of SQLite rather than parsed out of
    # the schema string. The two must be the same database.
    fresh_path = tmp_path / "fresh.db"
    fresh = sqlite3.connect(fresh_path)
    fresh.executescript(db.SCHEMA)
    fresh.row_factory = sqlite3.Row
    expected = _columns(fresh)
    fresh.close()

    for table, want in expected.items():
        missing = want - upgraded.get(table, set())
        assert not missing, (
            f"{table} is missing {sorted(missing)} after an upgrade. "
            f"A fresh install has it and an existing one does not, which means "
            f"it is in SCHEMA and not in MIGRATIONS.")


def test_an_upgrade_keeps_the_rows_that_were_already_there(tmp_path, monkeypatch):
    """A migration that dropped data would be worse than a missing column."""
    path = tmp_path / "old.db"
    _old_install(path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if hasattr(db._local, "conn"):
        del db._local.conn

    db.init()
    assert db.one("SELECT title FROM programs")["title"] == "Old Game"
    assert db.one("SELECT team_name FROM passes")["team_name"] == "Kansas City Chiefs"
    assert db.one("SELECT detail FROM sync_log")["detail"] == "an older sync"
    del db._local.conn


def test_a_new_column_starts_empty_rather_than_wrong(tmp_path, monkeypatch):
    """`teams_tried_at` on an existing row must read as "never asked", not as
    "asked just now". The second would mean the row is never enriched again."""
    path = tmp_path / "old.db"
    _old_install(path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if hasattr(db._local, "conn"):
        del db._local.conn

    db.init()
    row = db.one("SELECT teams_tried_at FROM programs WHERE guid = 'plex://episode/old'")
    assert row["teams_tried_at"] is None
    log = db.one("SELECT epg_refreshed_at, guide_ends_at FROM sync_log")
    assert log["epg_refreshed_at"] is None and log["guide_ends_at"] is None
    del db._local.conn


def test_upgrading_twice_is_not_an_error(tmp_path, monkeypatch):
    """`init` runs on every boot, not only on the boot that upgrades."""
    path = tmp_path / "old.db"
    _old_install(path)
    monkeypatch.setattr(db, "DB_PATH", str(path))
    if hasattr(db._local, "conn"):
        del db._local.conn

    db.init()
    db.init()
    db.init()
    assert db.one("SELECT title FROM programs")["title"] == "Old Game"
    del db._local.conn
