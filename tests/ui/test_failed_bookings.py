"""A refused booking on screen: loud in both views, and retryable.

The fault this covers showed nothing at all. A pass failed 364 times over three
weeks and the schedule looked like a quiet week, so the test that matters is
"can you see it", not "is the message worded well".
"""
import time

import pytest


def _a_failed_chiefs_booking(page):
    """A pass, and a run of failures against the live game, as a sync writes
    them. The pass is made through the app so the row reads as it really does.
    """
    from app import db
    from app.routes import record as record_routes
    team = db.one("SELECT * FROM teams WHERE name LIKE 'Kansas City%'")
    pid, _, _ = record_routes._make_pass("team", team=dict(team))
    a = db.one("""SELECT a.*, p.title FROM airings a JOIN programs p
                  ON p.guid = a.program_guid
                  WHERE p.teams LIKE '%Kansas City%' ORDER BY a.begins_at LIMIT 1""")
    now = int(time.time())
    with db.tx() as c:
        for i in range(3):
            c.execute(
                """INSERT INTO pass_actions (pass_id, program_guid, airing_id,
                                             program_title, channel_vcn, begins_at,
                                             action, reason, dry_run, created_at)
                   VALUES (?,?,?,?,?,?,'failed',?,0,?)""",
                (pid, a["program_guid"], a["id"], a["title"], a["channel_vcn"],
                 a["begins_at"],
                 "PlexError: create recording -> HTTP 400: Bad Request",
                 now - (3 - i) * 3600))
    return a


@pytest.fixture
def failed(page):
    a = _a_failed_chiefs_booking(page)
    page.goto("/recordings")
    page.wait_for_selector(".agrow", timeout=15000)
    return a


def test_the_agenda_shows_a_refused_booking_in_red(failed, page):
    row = page.locator(".agrow.fail").first
    assert row.count() == 1
    text = row.inner_text()
    assert "NOT RECORDING" in text
    assert "3 tries" in text
    assert "HTTP 400" in text, "the error is on the row, not hidden behind a click"


def test_the_calendar_shows_the_same_refusal(failed, page):
    page.click('[data-view="calendar"]')
    page.wait_for_selector("#calgrid .calday", timeout=15000)
    assert page.locator("#calgrid .calitem.fail").count() == 1


def test_clicking_it_opens_the_error_and_a_retry_button(failed, page):
    page.click(".agrow.fail")
    page.wait_for_selector("#failretry", timeout=15000)
    body = page.locator("#ovlbox").inner_text()
    assert "This will not record" in body
    assert "HTTP 400: Bad Request" in body, "Plex's own words, verbatim"
    assert "Kansas City Chiefs pass" in body
    assert page.locator("#failretry").is_enabled()


def test_the_retry_button_schedules_it(failed, page):
    page.click(".agrow.fail")
    page.wait_for_selector("#failretry", timeout=15000)
    page.click("#failretry")
    page.wait_for_selector("#ovlmsg.said.ok", timeout=15000)
    from app import db
    assert db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (failed["id"],))
