"""The guide grid, and opening a programme from it."""
import pytest


def test_the_grid_draws_channels_and_programmes(guide):
    assert guide.locator(".gch").count() >= 4
    assert guide.locator(".gprog").count() >= 3


def test_the_live_broadcast_is_marked_and_the_repeat_is_not(guide):
    live = guide.locator(".gprog.live")
    assert live.count() >= 1
    titles = [live.nth(i).get_attribute("title") for i in range(live.count())]
    assert any("Chiefs at Buccaneers" in (t or "") for t in titles)


def test_a_drm_airing_is_marked_as_such(guide):
    assert guide.locator(".gprog.drm").count() == 1


def test_there_is_a_now_line(guide):
    assert guide.locator("#nowline.gnow").count() == 1


def test_the_grid_scrolls_in_both_directions(guide):
    box = guide.eval_on_selector("#gw", """el => ({
        sw: el.scrollWidth, cw: el.clientWidth,
        sh: el.scrollHeight, ch: el.clientHeight})""")
    assert box["sw"] > box["cw"], "time has to run past the right edge"


def test_opening_a_programme_shows_every_airing_of_it(guide):
    guide.click('.gprog.live[title*="Chiefs"]')
    guide.wait_for_selector("#ovl.show .air")
    box = guide.locator("#ovlbox")
    assert "Chiefs at Buccaneers" in box.inner_text()
    assert box.locator(".air").count() == 2
    assert box.locator(".air.best").count() == 1, "one of them is the live one"
    assert "LIVE" in box.locator(".air.best").inner_text()


def test_the_panel_offers_to_follow_the_teams_in_it(guide):
    guide.click('.gprog.live[title*="Chiefs"]')
    guide.wait_for_selector("#ovlbox .air")
    assert guide.locator('#ovlbox [data-team]').count() == 2


def test_a_drm_airing_cannot_be_recorded_from_the_panel(guide):
    guide.click(".gprog.drm")
    guide.wait_for_selector("#ovlbox .air")
    assert "cannot record" in guide.locator("#ovlbox").inner_text()
    assert guide.locator("#ovlbox [data-rec]").count() == 0


def test_preview_mode_says_so_at_the_top_and_disables_the_buttons(page):
    """The message used to be a grey line at the bottom of a scrolling panel,
    which is why pressing Record looked like it did nothing."""
    from app import db
    db.set_setting("dry_run", "1")
    page.goto("/")
    page.wait_for_selector(".gprog")
    page.click('.gprog.live[title*="Chiefs"]')
    page.wait_for_selector("#ovlbox .air")
    banner = page.locator("#ovlbox .banner")
    assert banner.count() == 1
    assert banner.bounding_box()["y"] < page.locator("#ovlbox .air").first.bounding_box()["y"]
    assert page.locator("#ovlbox .act button[disabled]").count() >= 1


def test_the_panel_closes(guide):
    guide.click('.gprog[title*="Chiefs"]')
    guide.wait_for_selector("#ovlbox .air")
    guide.click("#ovlx")
    assert not guide.locator("#ovlbox .air").first.is_visible()


def test_the_search_box_opens_and_filters(page):
    page.goto("/")
    page.wait_for_selector(".gprog")
    page.click("#sbtoggle")
    page.fill("#sbinput", "Quiz")
    page.keyboard.press("Enter")
    page.wait_for_selector("#grid, .gprog")
    assert "Quiz" in page.content()


@pytest.mark.parametrize("locale,wants_meridiem", [
    ("en-US", True), ("en-GB", False), ("de-DE", False), ("fr-FR", False),
])
def test_the_clock_follows_the_viewer_not_the_author(browser, base_url, synced,
                                                     locale, wants_meridiem):
    """The guide used to write 12-hour AM/PM everywhere, which reads as broken
    in most of the world. Nothing about this app is American, and its clock
    should not be either."""
    import re

    from tests.ui.conftest import _page
    ctx, page = _page(browser, base_url, viewport={"width": 1440, "height": 900},
                      locale=locale)
    try:
        page.goto("/")
        page.wait_for_selector(".gtick", timeout=20000)
        ticks = [t.strip() for t in page.locator(".gtick").all_inner_texts()]
        assert ticks, "the guide drew no time axis"
        assert all(re.match(r"^\d{1,2}[:.]\d{2}", t) for t in ticks), ticks[:4]
        meridiem = any(re.search(r"(AM|PM|a\.m\.|p\.m\.)", t, re.I) for t in ticks)
        assert meridiem is wants_meridiem, f"{locale} rendered {ticks[:4]}"
    finally:
        ctx.close()
