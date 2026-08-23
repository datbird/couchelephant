"""The settings window: sections, sub-tabs, search, and the things it does."""
import pytest


def _settled(page, timeout=20000):
    """Wait for the verdict to stop saying "Checking..."."""
    page.wait_for_function(
        "() => { var v = document.getElementById('plexverdict');"
        "  return v && !v.hidden && !v.className.includes('busy'); }",
        timeout=timeout)


@pytest.fixture
def settings(page):
    page.goto("/settings")
    page.wait_for_selector("#setnav .nav-item")
    return page


def test_the_gear_opens_settings_over_the_page_it_was_pressed_on(page):
    """It opens in place rather than navigating, so the guide is still behind
    it and closing puts you back where you were."""
    page.goto("/")
    page.wait_for_selector(".gprog")
    page.click("#gearbtn")
    page.wait_for_selector("#setwin", timeout=15000)
    assert page.locator("#setwin").is_visible()
    assert page.url.endswith("/")
    page.click("#setclose")
    assert not page.locator("#setwin").is_visible()
    assert page.locator(".gprog").count() > 0


def test_there_is_no_settings_tab_left_in_the_tab_bar(page):
    page.goto("/")
    labels = page.eval_on_selector_all(".pt-tab", "els => els.map(e => e.textContent.trim())")
    assert "Settings" not in labels


def test_the_sections_run_down_the_left(settings):
    names = settings.eval_on_selector_all(
        "#setnav .nav-item", "els => els.map(e => e.textContent.trim())")
    assert names == ["Plex", "Recording", "Accounts", "Channels",
                     "Backup & restore", "About"]


def test_each_section_has_its_own_sub_tabs(settings):
    settings.click('.nav-item[data-sec="plex"]')
    tabs = settings.eval_on_selector_all(
        "#settabs .tab", "els => els.map(e => e.textContent.trim())")
    assert tabs == ["Server", "Guide"]

    settings.click('.nav-item[data-sec="accounts"]')
    tabs = settings.eval_on_selector_all(
        "#settabs .tab", "els => els.map(e => e.textContent.trim())")
    assert tabs == ["Sign-in", "People"]


def test_switching_sub_tab_switches_the_pane(settings):
    settings.click('.nav-item[data-sec="plex"]')
    assert settings.locator('section[data-tab="server"]').is_visible()
    settings.click('#settabs .tab[data-tab="guide"]')
    assert settings.locator('section[data-tab="guide"]').is_visible()
    assert not settings.locator('section[data-tab="server"]').is_visible()


def test_the_search_reaches_across_every_section(settings):
    settings.fill("#setq", "team domain")
    visible = settings.eval_on_selector_all(
        "#setnav .nav-item:not([hidden])", "els => els.map(e => e.dataset.sec)")
    assert visible == ["accounts"]
    # A word that lives in two sections lights both.
    settings.fill("#setq", "cloudflare")
    visible = settings.eval_on_selector_all(
        "#setnav .nav-item:not([hidden])", "els => els.map(e => e.dataset.sec)")
    assert visible == ["accounts", "data"]

    settings.fill("#setq", "zzzznothing")
    assert settings.locator("#setnone").is_visible()


def test_a_good_connection_gets_a_green_tick(settings):
    settings.click("text=Test connection")
    _settled(settings)
    v = settings.locator("#plexverdict")
    assert "fakeplex" in v.inner_text()
    assert "ok" in (settings.get_attribute("#plexverdict", "class") or "")
    assert "bad" not in (settings.get_attribute("#plexverdict", "class") or "")
    tick = settings.eval_on_selector(
        "#plexverdict .vmark",
        "e => getComputedStyle(e, '::before').content")
    assert "\u2713" in tick, "a green tick, not just a green box"


def test_a_bad_connection_gets_a_red_cross_and_says_why(settings):
    from app import db
    db.set_setting("plex_url", "http://127.0.0.1:1")
    settings.reload()
    settings.click("text=Test connection")
    _settled(settings, timeout=30000)
    v = settings.locator("#plexverdict")
    assert "bad" in (settings.get_attribute("#plexverdict", "class") or "")
    assert "Could not reach" in v.inner_text()


def test_the_verdict_box_is_not_drawn_before_there_is_a_verdict(settings):
    """It used to render as an empty bordered strip under the button."""
    assert not settings.locator("#plexverdict").is_visible()


def test_the_channel_list_offers_an_override_and_a_reset(settings):
    settings.click('.nav-item[data-sec="channels"]')
    settings.click('#settabs .tab[data-tab="art"]')
    settings.wait_for_selector("#chlist .chrow", timeout=15000)
    row = settings.locator("#chlist .chrow").first
    assert row.locator('input[type="file"]').count() == 1
    assert row.locator(".chreset").count() == 1


def test_the_channel_list_is_searchable(settings):
    settings.click('.nav-item[data-sec="channels"]')
    settings.click('#settabs .tab[data-tab="art"]')
    settings.wait_for_selector("#chlist .chrow", timeout=15000)
    before = settings.locator("#chlist .chrow").count()
    settings.fill("#chq", "41.1")
    settings.wait_for_function(
        "n => document.querySelectorAll('#chlist .chrow:not([hidden])').length < n",
        arg=before, timeout=15000)


def test_the_about_tab_carries_the_version_and_plain_words(settings):
    # Read the version rather than spelling it out. A release bumps it, and a
    # test that hardcodes it fails the release build it is meant to protect.
    from app.routes._shared import VERSION
    settings.click('.nav-item[data-sec="about"]')
    text = settings.locator('section[data-sec="about"]').inner_text()
    assert VERSION in text
    assert "records every one that comes up" in text
    import re
    for jargon in ("API", "subscription", "one-shot", "guid", "premiere",
                   "endpoint"):
        assert not re.search(rf"\b{jargon}\b", text, re.I), \
            f"{jargon} is jargon; say what it does instead"


def test_the_version_is_on_the_logo_tooltip_too(page):
    from app.routes._shared import VERSION
    page.goto("/")
    tip = page.get_attribute(".brand .mark", "title") or ""
    assert VERSION in tip


def test_the_close_button_leaves_settings(settings):
    settings.click("#setclose")
    settings.wait_for_url(lambda url: not url.endswith("/settings"))
