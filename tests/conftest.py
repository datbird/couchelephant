"""Fixtures. Every one of them is scratch, and the guard proves it."""
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The environment has to be set before the app imports, because db.py reads
# these at module level. Doing it in a fixture would be too late.
_ROOT = tempfile.mkdtemp(prefix="ce-test-")
os.environ["COUCHELEPHANT_DB"] = os.path.join(_ROOT, "couchelephant.db")
os.environ["COUCHELEPHANT_AUTH_DB"] = os.path.join(_ROOT, "auth.db")
os.environ["COUCHELEPHANT_LOGOS"] = os.path.join(_ROOT, "logos")

from tests import fake_plex, isolation           # noqa: E402
isolation.assert_isolated()                      # before anything is imported

from app import auth, db, passes, sync           # noqa: E402


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def plex_url():
    url, stop = fake_plex.start()
    os.environ["COUCHELEPHANT_TEST_PLEX"] = url
    isolation.assert_isolated()
    yield url
    stop()


@pytest.fixture(autouse=True)
def clean_db(plex_url):
    """A fresh database for every test.

    Dropping and recreating rather than reusing, because a test that leaves a
    pass behind changes what the next one sees, and a suite that only passes in
    order is worth very little.
    """
    isolation.assert_isolated()
    db.init()
    conn = db.connect()
    # Order would otherwise matter, and a new table would break the wipe the
    # first time somebody added one. Turning the constraints off for the
    # duration says what is meant: start from nothing.
    conn.executescript("PRAGMA foreign_keys=OFF")
    for table in _tables(conn):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.executescript("PRAGMA foreign_keys=ON")
    a = auth._con()
    a.executescript("DELETE FROM users; DELETE FROM sessions; "
                    "DELETE FROM email_map; DELETE FROM prefs;")
    a.commit()
    db.set_setting("plex_url", plex_url)
    db.set_setting("plex_token", "test-token")
    db.set_setting("dry_run", "0")
    db.set_setting("auth_mode", "none")
    fake_plex.STATE.reset()
    yield


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]


@pytest.fixture
def plex(plex_url):
    from app.plex import Plex
    p = Plex(plex_url, "test-token")
    yield p
    p.close()


@pytest.fixture
def synced(plex):
    """A database with the fake server's guide already pulled in."""
    ok, detail = sync.full_sync()
    assert ok, detail
    return detail


@pytest.fixture
def client():
    """The app, driven in-process. No network, no port."""
    from fastapi.testclient import TestClient
    from app.web import app
    with TestClient(app, base_url="http://testserver") as c:
        # Same-origin by default, so the guard does not reject every call.
        c.headers.update({"Origin": "http://testserver", "Host": "testserver"})
        yield c
