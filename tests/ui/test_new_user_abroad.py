"""A brand new install, set up from scratch, by somebody who is not American.

Everything else in this suite starts from a database that is already
configured. This does not. It walks the first-run screen, picks a timezone on
the other side of the world, and then uses the app, because that is the path a
stranger takes and the one nothing else covers.
"""
import re

import pytest

from tests.ui.conftest import _page

# (timezone, browser locale, does that locale write am/pm). Australia uses a
# 12-hour clock and Japan does not, which is the point: the app follows the
# locale rather than a rule anybody invented.
ABROAD = [
    ("Asia/Tokyo", "ja-JP", False),
    ("Australia/Sydney", "en-AU", True),
    ("Europe/London", "en-GB", False),
    ("America/Chicago", "en-US", True),
]


@pytest.fixture
def unconfigured(base_url):
    """A database with nothing in it, as a new install has."""
    from app import db
    for k in ("plex_url", "plex_token"):
        db.set_setting(k, "")
    db.set_setting("timezone", "UTC")
    yield


@pytest.mark.parametrize("zone,locale,meridiem_here", ABROAD)
def test_a_new_install_sets_itself_up_from_anywhere(browser, base_url, unconfigured,
                                                    plex_url, zone, locale,
                                                    meridiem_here):
    ctx, page = _page(browser, base_url, viewport={"width": 1440, "height": 900},
                      locale=locale, timezone_id=zone)
    try:
        # 1. Any page sends a fresh install to the setup screen.
        page.goto("/recordings")
        page.wait_for_selector("#wform", timeout=20000)

        # 2. The timezone they actually live in is on offer.
        assert page.locator(f'#wz option[value="{zone}"]').count() == 1, \
            f"{zone} is not in the timezone list"
        # The page guesses from the browser, so a stranger in Tokyo does not
        # have to hunt through 484 zones for their own.
        assert page.locator("#wz").input_value() == zone, \
            "the picker should start on the viewer's own timezone"

        # 3. Fill it in the way a person would, and save.
        page.fill("#wu", plex_url)
        page.fill("#wt", "demo")
        page.click("#wform button[type=submit]")

        # 4. It saved what they typed, and started pulling the guide itself.
        # No hand-run sync here: a stranger does not have one.
        from app import db
        page.wait_for_function(
            "() => !document.getElementById('wform')", timeout=45000)
        assert db.get_setting("timezone") == zone
        page.wait_for_selector(".growr", timeout=60000)
        assert db.query("SELECT 1 FROM airings LIMIT 1"), "the guide never synced"

        # The demo guide covers about half a day, so which local day holds it
        # depends on the zone. Whichever it is, the programmes have to draw.
        for i in range(3):
            if page.locator(".gprog").count():
                break
            page.locator(".daybar a").nth(i + 1).click()
            page.wait_for_selector(".growr", timeout=20000)
            page.wait_for_timeout(600)
        assert page.locator(".gprog").count(), "no programme drew on any day"
        ticks = [t.strip() for t in page.locator(".gtick").all_inner_texts()]
        assert ticks, "the guide drew no time axis"
        meridiem = any(re.search(r"(AM|PM)", t, re.I) for t in ticks)
        assert meridiem is meridiem_here, f"{locale} clock: {ticks[:3]}"

        # 5. The day strip is in their language, not the server's.
        days = [d.strip() for d in page.locator(".daybar a").all_inner_texts()]
        assert days[0] == "Today"
        if locale == "ja-JP":
            assert any(re.search(r"[月火水木金土日]", d) for d in days[1:]), days[:4]

        # 6. Preview mode is on, so nothing reached the DVR yet.
        assert db.get_setting("dry_run") == "1"
    finally:
        ctx.close()
