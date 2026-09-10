"""A booking that Plex refused has to be visible, and retryable.

Written after 2026-09-10, when a team pass failed 364 times over three weeks
and the schedule showed nothing at all. The programme left "Waiting for the
Plex guide data" as soon as the guide carried it, and no row took its place.
"""
import time

from app import db, passes
from tests import fake_plex


def _failed_attempt(airing_id, when=None, reason="PlexError: HTTP 400", n=3):
    """`n` failures against one airing, the way a sync writes them."""
    a = db.one("SELECT * FROM airings WHERE id = ?", (airing_id,))
    at = when or int(time.time())
    with db.tx() as c:
        for i in range(n):
            c.execute(
                """INSERT INTO pass_actions (pass_id, program_guid, airing_id,
                                             program_title, channel_vcn, begins_at,
                                             action, reason, dry_run, created_at)
                   VALUES (1,?,?,?,?,?,'failed',?,0,?)""",
                (a["program_guid"], airing_id, "Chiefs at Buccaneers",
                 a["channel_vcn"], a["begins_at"], reason, at - (n - i) * 3600))


def _a_pass():
    with db.tx() as c:
        c.execute("INSERT INTO passes (id, kind, team_id, team_name, enabled, "
                  "created_at) VALUES (1,'team',236,'Kansas City Chiefs',1,?)",
                  (int(time.time()),))


def _live_airing():
    return db.one("SELECT id FROM airings WHERE begins_at = ?",
                  (fake_plex.LIVE_AT,))["id"]


def test_a_failed_booking_appears_in_the_schedule(client, synced):
    """The whole point. Nothing at all was worse than a wrong answer."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid)
    rows = client.get("/api/schedule").json()["rows"]
    fail = [r for r in rows if r["status"] == "failed"]
    assert len(fail) == 1
    assert fail[0]["airing_id"] == aid
    assert fail[0]["attempts"] == 3
    assert "HTTP 400" in fail[0]["error"]
    assert fail[0]["reason"] == "the Kansas City Chiefs pass"


def test_a_failed_booking_appears_in_the_calendar_window(client, synced):
    """Agenda and calendar read one feed, so one row serves both. This is the
    windowed call the calendar makes, and it must carry the failure too."""
    _a_pass()
    _failed_attempt(_live_airing())
    d = client.get("/api/schedule", params={
        "limit": 1000, "start": fake_plex.LIVE_AT - 86400,
        "end": fake_plex.LIVE_AT + 86400}).json()
    assert [r["status"] for r in d["rows"]].count("failed") == 1


def test_a_booking_that_succeeded_later_is_not_still_shown_as_failed(client, synced,
                                                                    plex):
    """The newest action wins. A retry that worked must clear the row, or the
    schedule cries wolf and stops being read."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid)
    row = db.one("""SELECT a.*, p.title, p.rating_key FROM airings a
                    JOIN programs p ON p.guid = a.program_guid WHERE a.id = ?""", (aid,))
    passes._schedule(plex, row, None, "pass", pass_id=1)
    from app import sync
    sync.sync_recordings(plex)
    rows = client.get("/api/schedule").json()["rows"]
    assert not [r for r in rows if r["status"] == "failed"]


def test_a_failure_for_a_broadcast_that_already_aired_is_dropped(client, synced):
    """Nothing can be done about last night, so it is not an alarm."""
    _a_pass()
    aid = _live_airing()
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ?, ends_at = ? WHERE id = ?",
                  (int(time.time()) - 7200, int(time.time()) - 3600, aid))
    _failed_attempt(aid)
    with db.tx() as c:
        c.execute("UPDATE pass_actions SET begins_at = ?", (int(time.time()) - 7200,))
    rows = client.get("/api/schedule").json()["rows"]
    assert not [r for r in rows if r["status"] == "failed"]


def test_the_failure_panel_gives_the_error_verbatim(client, synced):
    """Plex's own words. A paraphrase loses the part that says what to change."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid, reason="PlexError: create recording -> HTTP 400: Bad Request")
    d = client.get("/api/schedule/failure", params={"airing_id": aid}).json()
    assert d["ok"]
    f = d["failure"]
    assert f["error"] == "PlexError: create recording -> HTTP 400: Bad Request"
    assert f["attempts"] == 3
    assert f["pass_name"] == "Kansas City Chiefs"
    assert f["in_guide"] is True
    assert f["b"] == fake_plex.LIVE_AT


def test_retrying_a_failed_booking_schedules_it(client, synced):
    """The button has to do the same thing a sync would, or a green retry
    proves nothing about the next automatic attempt."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid)
    d = client.post("/api/schedule/retry", data={"airing_id": aid}).json()
    assert d["ok"], d
    assert db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (aid,))
    rows = client.get("/api/schedule").json()["rows"]
    assert not [r for r in rows if r["status"] == "failed"]


def test_a_retry_that_fails_is_written_down(client, synced):
    """Otherwise the attempt count on the row is a lie."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid)
    fake_plex.STATE.drop_next_create = True
    d = client.post("/api/schedule/retry", data={"airing_id": aid})
    assert d.status_code == 500
    assert not d.json()["ok"]
    rows = client.get("/api/schedule").json()["rows"]
    fail = [r for r in rows if r["status"] == "failed"]
    assert len(fail) == 1
    assert fail[0]["attempts"] == 4, "the hand retry counts too"


def test_a_retry_says_so_when_the_broadcast_left_the_guide(client, synced):
    """A button that always fails the same way is worse than no button."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid)
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE id = ?", (aid,))
    r = client.post("/api/schedule/retry", data={"airing_id": aid})
    assert r.status_code == 409
    assert "no longer in the Plex guide" in r.json()["error"]


def test_preview_mode_refuses_a_retry(client, synced):
    """Preview mode means nothing reaches Plex, buttons included."""
    _a_pass()
    aid = _live_airing()
    _failed_attempt(aid)
    db.set_setting("dry_run", "1")
    r = client.post("/api/schedule/retry", data={"airing_id": aid})
    assert r.status_code == 400
    assert "Preview mode" in r.json()["error"]
