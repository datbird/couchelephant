"""The health badge, in a real browser.

Everything about this feature exists to be *seen*. A check that raises a
perfect notice into a database nobody looks at has not told anybody anything,
and the server-side tests cannot tell the difference. So this half asks the
questions they cannot: is the badge on the screen, does clicking it open the
reason, and does clicking it avoid starting a sync.
"""
import time

import pytest

from app import db, health, sync
from tests import fake_plex

DAY = 86400


@pytest.fixture
def unwell(page):
    """A server whose guide Plex stopped refreshing five days ago."""
    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    sync.full_sync()
    assert health.EPG_STALE in {n["code"] for n in health.open_notices()}
    page.goto("/")
    page.wait_for_selector("header")
    yield page
    fake_plex.STATE.refreshed_at = None


def test_a_healthy_server_shows_no_badge(page):
    with db.tx() as c:
        c.execute("UPDATE notices SET resolved_at = 1 WHERE resolved_at IS NULL")
    page.goto("/")
    page.wait_for_selector("header")
    assert page.locator("#noticebtn").count() == 0


def test_the_badge_appears_on_the_sync_button(unwell):
    """Not a fourth icon in the bar. A guide that has stopped moving is a sync
    problem, so it badges the control you would reach for anyway."""
    assert unwell.locator("#noticebtn").is_visible()
    assert unwell.locator('.sync-wrap.has-notices #noticebtn').count() == 1
    assert unwell.locator('.sync-wrap form[action="/sync"] button').count() == 1


def test_the_badge_is_actually_drawn_over_the_button(unwell):
    """`is_visible` believes a zero-opacity element in a scrolled-away corner.
    Ask the browser what is painted at that point instead."""
    box = unwell.locator("#noticebtn").bounding_box()
    assert box and box["width"] > 6 and box["height"] > 6, box
    hit = unwell.evaluate(
        """([x, y]) => {
             const el = document.elementFromPoint(x, y);
             return el ? el.closest('#noticebtn') !== null : false;
           }""",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    assert hit, "something is painted over the badge"


def test_clicking_the_badge_says_what_is_wrong_and_what_to_do(unwell):
    unwell.click("#noticebtn")
    menu = unwell.locator("#noticemenu.open")
    menu.wait_for(timeout=5000)
    text = menu.inner_text()
    assert "guide has not refreshed" in text
    assert "5 days ago" in text, text
    assert "Live TV & DVR" in text, "a problem with no next step is just anxiety"
    assert "days ago" in text.lower()


def test_reading_the_notice_does_not_start_a_sync(unwell):
    """The badge sits on the sync button. If it submitted that form, looking at
    a warning would kick off a minute of work nobody asked for."""
    before = db.one("SELECT COUNT(*) c FROM sync_log")["c"]
    unwell.click("#noticebtn")
    unwell.wait_for_selector("#noticemenu.open")
    unwell.wait_for_timeout(600)
    assert db.one("SELECT COUNT(*) c FROM sync_log")["c"] == before
    # And the badge must stay outside the form, not merely decline to submit
    # it. A later edit that tucks it inside would pass a click-and-count check
    # on a fast machine and fail on a slow one.
    inside = unwell.evaluate(
        """() => !!document.querySelector('#noticebtn').closest('form')""")
    assert not inside, "the badge is inside the sync form"


def test_the_panel_closes_on_escape_and_on_a_click_outside(unwell):
    unwell.click("#noticebtn")
    unwell.wait_for_selector("#noticemenu.open")
    unwell.keyboard.press("Escape")
    unwell.wait_for_selector("#noticemenu.open", state="detached", timeout=3000)

    unwell.click("#noticebtn")
    unwell.wait_for_selector("#noticemenu.open")
    unwell.mouse.click(400, 500)
    unwell.wait_for_selector("#noticemenu.open", state="detached", timeout=3000)


def test_the_badge_follows_you_between_pages(unwell):
    """A problem with Plex is not a property of the page you happen to be on."""
    unwell.goto("/recordings")
    unwell.wait_for_selector("header")
    assert unwell.locator("#noticebtn").is_visible()


def test_the_badge_goes_away_once_plex_catches_up(unwell):
    fake_plex.STATE.refreshed_at = None
    sync.full_sync()
    unwell.goto("/")
    unwell.wait_for_selector("header")
    assert health.EPG_STALE not in {n["code"] for n in health.open_notices()}


def test_the_badge_is_legible_in_light_mode_too(light_system):
    """The colour tokens have to carry it. White on a pale ground is a badge
    nobody sees, which is the same as no badge."""
    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    sync.full_sync()
    light_system.goto("/")
    light_system.wait_for_selector("#noticebtn")
    colours = light_system.evaluate(
        """() => {
             const s = getComputedStyle(document.querySelector('#noticebtn'));
             return [s.backgroundColor, s.color];
           }""")
    assert colours[0] != colours[1], "the glyph and its ground are the same colour"
    assert "rgba(0, 0, 0, 0)" not in colours[0], "the badge has no fill"
    fake_plex.STATE.refreshed_at = None
