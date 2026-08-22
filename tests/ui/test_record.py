"""Pressing record: the options panel, the ownership bar, and the result."""
from tests import fake_plex


def _open_options(page, title="Chiefs"):
    page.goto("/")
    page.wait_for_selector(".gprog")
    page.click(f'.gprog.live[title*="{title}"]')
    page.wait_for_selector("#ovlbox .air")
    page.click("#ovlbox .air.best [data-rec]")
    page.wait_for_selector("#optgo")


def test_the_options_panel_shows_plexs_own_settings(page):
    _open_options(page)
    text = page.locator("#ovlbox").inner_text()
    assert "Resolution" in text
    assert "Minutes before start" in text


def test_plexs_plumbing_is_not_shown(page):
    _open_options(page)
    ids = page.eval_on_selector_all("[data-set]", "els => els.map(e => e.dataset.set)")
    assert "oneShot" not in ids
    assert "comskipEnabled" not in ids


def test_every_option_is_marked_with_whose_feature_it_is(page):
    _open_options(page)
    rows = page.locator(".optgrid .optrow")
    assert rows.count() >= 3
    for i in range(rows.count()):
        assert rows.nth(i).locator("i.own").count() == 1, "every row carries a marker"
    key = page.locator(".ownkey").inner_text().upper()
    assert "COUCHELEPHANT" in key and "PLEX DVR" in key
    # And the two markers are actually different colours, not the same chip twice.
    ce = page.eval_on_selector("i.own.ce", "e => getComputedStyle(e).backgroundColor")
    plex = page.eval_on_selector("i.own.plex", "e => getComputedStyle(e).backgroundColor")
    assert ce != plex


def test_the_bar_says_plex_until_you_limit_the_sources(page):
    _open_options(page)
    assert "Plex schedule" in page.locator("#optbar").inner_text()

    # A single broadcast is already pinned to one channel, so the limit only
    # means anything on the rule that keeps matching.
    page.check('input[name=tpl][value="1"]')
    page.wait_for_selector("#multibtn:not([disabled])")
    page.click("#multibtn")
    page.wait_for_selector("#multibody .multirow")
    page.check('#multibody input[data-net="ABC"]')
    page.check('#multibody input[data-net="CBS"]')
    assert "CouchElephant schedule" in page.locator("#optbar").inner_text()


def test_the_source_picker_takes_more_than_one_channel(page):
    _open_options(page)
    page.check('input[name=tpl][value="1"]')
    page.click("#multibtn")
    page.wait_for_selector("#multibody .multirow")
    boxes = page.locator('#multibody input[type="checkbox"]')
    assert boxes.count() >= 6, "four channels and their networks"
    page.check('#multibody input[data-ch="9.1"]')
    page.check('#multibody input[data-ch="5.1"]')
    summary = page.locator("#multisum").inner_text()
    assert "9.1" in summary and "5.1" in summary


def test_the_source_picker_is_searchable(page):
    _open_options(page)
    page.check('input[name=tpl][value="1"]')
    page.click("#multibtn")
    page.wait_for_selector("#multibody .multirow")
    before = page.locator("#multibody .multirow").count()
    page.fill("#multiq", "ABC")
    assert page.locator("#multibody .multirow").count() < before


def test_a_single_broadcast_says_it_is_already_pinned(page):
    _open_options(page)
    assert "Limit to channel" in page.locator("#ovlbox").inner_text()
    assert "stops Plex choosing a repeat" in page.locator("#ovlbox").inner_text()


def test_the_back_button_returns_to_the_programme(page):
    _open_options(page)
    page.click("#optback")
    page.wait_for_selector("#ovlbox .air")
    assert "Chiefs at Buccaneers" in page.locator("#ovlbox").inner_text()


def test_recording_it_reports_success_and_marks_the_guide(page):
    _open_options(page)
    page.click("#optgo")
    page.wait_for_selector("#ovlbox .air .pill.ok", timeout=15000)
    assert "scheduled" in page.locator("#ovlbox").inner_text()
    assert len(fake_plex.STATE.created) == 1
    pinned = fake_plex.STATE.created[0]["prefs"]
    assert pinned["oneShot"] == "1"
    assert pinned["startTimeslot"] == str(fake_plex.LIVE_AT)

    page.click("#ovlx")
    page.wait_for_selector(".gprog.sched-ce", timeout=15000)


def test_the_button_offers_to_cancel_rather_than_saying_done(page):
    _open_options(page)
    page.click("#optgo")
    page.wait_for_selector("#ovlbox .air .pill.ok", timeout=15000)
    row = page.locator("#ovlbox .air.best")
    assert row.locator("[data-cancel]").count() == 1
    assert "Cancel" in row.inner_text()
    assert "Done" not in row.inner_text()


def test_cancelling_takes_it_back_out_of_plex(page):
    _open_options(page)
    page.click("#optgo")
    page.wait_for_selector("#ovlbox .air .pill.ok", timeout=15000)
    page.click("#ovlbox .air.best [data-cancel]")
    page.wait_for_selector("#ovlbox .air.best [data-rec]", timeout=15000)
    assert fake_plex.STATE.subscriptions == {}


def test_following_a_team_from_the_panel_says_what_it_did(page):
    page.goto("/")
    page.wait_for_selector(".gprog")
    page.click('.gprog.live[title*="Chiefs"]')
    page.wait_for_selector("#ovlbox .air")
    page.click('#ovlbox [data-team]')
    # The panel redraws once the pass has been made and the game booked, so
    # wait for the answer rather than for the "Working..." that precedes it.
    page.wait_for_function(
        "() => /Following Kansas City Chiefs/.test("
        "document.getElementById('ovlbox').textContent)", timeout=30000)
