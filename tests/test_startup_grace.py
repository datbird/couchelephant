"""Plex starting up is not Plex failing.

On 2026-09-14 the box that runs both was rebooted. CouchElephant and Plex came
up together, CouchElephant asked for `/livetv/dvrs` three seconds later, and
Plex answered 503 because it was still running its startup maintenance tasks.
Two alerts went out, "Plex could not be reached" and "A sync failed", for a
server that was working normally an hour later and had never been broken.

A restart is ordinary: it happens on every update the watchdog applies and
every reboot of the host. So a sync that lands in that window waits for the
server instead of reporting it.
"""
import time

import httpx
import pytest

from app import db, health, plex, sync
from tests import fake_plex


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The grace is real; the waiting is not worth the suite's time."""
    monkeypatch.setattr(sync, "STARTUP_POLL", 0)


def _failed_syncs():
    return db.query("SELECT * FROM sync_log WHERE ok = 0")


def _unreachable():
    return db.one("SELECT * FROM notices WHERE code = ? AND resolved_at IS NULL",
                  (health.PLEX_UNREACHABLE,))


def test_a_server_still_starting_is_waited_for(plex_url):
    """The reboot, exactly as it happened."""
    fake_plex.maintenance(3)

    ok, detail = sync.full_sync()

    assert ok, detail
    assert fake_plex.STATE.maintenance_served == 3, "the fake never got asked"
    assert not _failed_syncs(), "a sync that recovered was logged as failed"
    assert _unreachable() is None, "a server that came up raised a fault"


def test_the_wait_ends_and_the_fault_is_still_raised(plex_url, monkeypatch):
    """A grace with no end is an alert that never fires."""
    monkeypatch.setattr(sync, "STARTUP_GRACE", 1)
    fake_plex.maintenance(10_000)

    ok, detail = sync.full_sync()

    assert not ok
    assert len(_failed_syncs()) == 1, "one failure, not one per try"
    assert _unreachable() is not None
    assert "Maintenance" in detail
    assert "starting" in detail, "the alert should say what it waited for"


def test_a_real_failure_is_reported_at_once(plex_url, monkeypatch):
    """Nothing here may slow down the alert for a server that is broken."""
    monkeypatch.setattr(sync, "STARTUP_GRACE", 600)
    db.set_setting("plex_token", "")        # a fault, and not a starting one
    started = time.time()

    ok, _ = sync.full_sync()

    assert not ok
    assert time.time() - started < 5, "a plain fault was held in the grace"
    assert len(_failed_syncs()) == 1


def test_only_plex_saying_so_counts_as_starting():
    """A 503 from anything else is a fault. The word is the evidence."""
    starting = plex.PlexError(
        'GET /livetv/dvrs -> HTTP 503: {"code":503,"title":"Maintenance",'
        '"status":"Plex Media Server is currently running startup '
        'maintenance tasks."}', 503)
    assert plex.is_starting(starting)

    assert plex.is_starting(httpx.ConnectError("connection refused"))
    assert not plex.is_starting(
        plex.PlexError("GET /livetv/dvrs -> HTTP 503: upstream unavailable", 503))
    assert not plex.is_starting(plex.PlexError("GET / -> HTTP 401: no token", 401))
    assert not plex.is_starting(plex.PlexError("no Plex token configured"))
    assert not plex.is_starting(ValueError("something else entirely"))
