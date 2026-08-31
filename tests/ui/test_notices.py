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


def test_with_a_problem_the_sync_button_opens_it_instead_of_syncing(unwell):
    """Syncing is the reflex, and with the guide stale that reflex spends a
    minute re-reading a guide Plex has not moved. It answers nothing. The
    reason is the click worth making, so the button in the bar makes it."""
    before = db.one("SELECT COUNT(*) c FROM sync_log")["c"]
    unwell.click("#syncbtn")
    unwell.wait_for_selector("#noticemenu.open", timeout=5000)
    unwell.wait_for_timeout(600)
    assert db.one("SELECT COUNT(*) c FROM sync_log")["c"] == before
    # And it has to be out of the form, not merely decline to submit it. A
    # later edit that tucked it back inside would pass the count check above on
    # a fast machine and fail on a slow one.
    inside = unwell.evaluate(
        """() => !!document.querySelector('#syncbtn').closest('form')""")
    assert not inside, "the bar's sync button is still a submit"


def test_the_panel_can_still_sync_anyway(unwell):
    """None of these problems stop a sync from working. Taking the sync off the
    bar without putting it back would just be removing it."""
    before = db.one("SELECT COUNT(*) c FROM sync_log")["c"]
    unwell.click("#syncbtn")
    unwell.wait_for_selector("#noticemenu.open")
    assert unwell.locator("#syncanyway").is_visible()
    with unwell.expect_navigation():
        unwell.click("#syncanyway")
    unwell.wait_for_selector("header")
    assert db.one("SELECT COUNT(*) c FROM sync_log")["c"] > before


def test_the_sync_anyway_button_is_really_on_top(unwell):
    """`is_visible` believes a button the panel is drawing over. Ask the
    browser what is painted at the point instead."""
    unwell.click("#syncbtn")
    unwell.wait_for_selector("#noticemenu.open")
    box = unwell.locator("#syncanyway").bounding_box()
    assert box and box["width"] > 40 and box["height"] > 14, box
    hit = unwell.evaluate(
        """([x, y]) => {
             const el = document.elementFromPoint(x, y);
             return el ? el.closest('#syncanyway') !== null : false;
           }""",
        [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
    assert hit, "something is painted over the sync button in the panel"


def test_the_badge_and_the_icon_open_the_same_panel(unwell):
    """Two controls, one panel. The badge is what the eye goes to and the icon
    is what the hand hits, and either has to work."""
    unwell.click("#noticebtn")
    unwell.wait_for_selector("#noticemenu.open")
    unwell.keyboard.press("Escape")
    unwell.wait_for_selector("#noticemenu.open", state="detached", timeout=3000)
    unwell.click("#syncbtn")
    unwell.wait_for_selector("#noticemenu.open")
    assert unwell.locator("#noticemenu").count() == 1


def test_a_healthy_sync_button_still_just_syncs(page):
    """The new behaviour is the exception, not the rule. With nothing wrong
    there is no panel, and the icon syncs in one click as it always did."""
    with db.tx() as c:
        c.execute("UPDATE notices SET resolved_at = 1 WHERE resolved_at IS NULL")
    page.goto("/")
    page.wait_for_selector("header")
    assert page.locator("#noticemenu").count() == 0
    inside = page.evaluate(
        """() => !!document.querySelector('#syncbtn').closest('form')""")
    assert inside, "with nothing wrong the sync button must submit the sync form"
    before = db.one("SELECT COUNT(*) c FROM sync_log")["c"]
    with page.expect_navigation():
        page.click("#syncbtn")
    assert db.one("SELECT COUNT(*) c FROM sync_log")["c"] > before


def test_the_sync_button_says_what_it_will_do_now(unwell):
    """A control that has changed what it does has to say so. Otherwise the
    tooltip is a lie and a screen reader announces a sync that will not
    happen."""
    label = unwell.get_attribute("#syncbtn", "aria-label")
    assert "problem" in label.lower(), label
    tip = unwell.get_attribute("#syncbtn", "title")
    assert "sync anyway" in tip.lower(), tip
    assert "Last synced" in tip or "Never synced" in tip, tip
