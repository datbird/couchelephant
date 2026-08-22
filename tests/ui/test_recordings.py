"""The Recordings page: sub-tabs, the two views, passes, and adding one."""
import pytest


@pytest.fixture
def recordings(page):
    page.goto("/recordings")
    page.wait_for_selector(".subtab")
    return page


def _follow_the_chiefs(page):
    """Make the pass through the app, not through the guide.

    The server runs in this process, so calling it directly is the same code
    path the panel takes. Driving the guide as well would make every test on
    this page depend on the other one, which is how a suite ends up telling
    you the wrong thing when it breaks. The panel's own version of this is
    tested once, in test_record.py.
    """
    from app import db, passes, plex as plexmod, sync, web
    team = db.one("SELECT * FROM teams WHERE name LIKE 'Kansas City%'")
    web._make_pass("team", team=dict(team))
    passes.run_passes()
    # The schedule is what Plex reports back, not what we asked for. Without
    # this the page is honestly empty, which is the behaviour, not a bug.
    p = plexmod.Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
    sync.sync_recordings(p)
    p.close()


def test_there_are_two_sub_tabs_and_no_next_games_section(recordings):
    labels = [recordings.locator(".subtab").nth(i).inner_text().strip()
              for i in range(recordings.locator(".subtab").count())]
    assert len(labels) == 2
    assert labels[0] == "Schedule"
    assert labels[1].startswith("Passes")
    assert "Next games" not in recordings.content()


def test_the_sub_tabs_switch_panes(recordings):
    assert recordings.locator('[data-pane="schedule"]').is_visible()
    recordings.click('.subtab[data-sub="passes"]')
    assert recordings.locator('[data-pane="passes"]').is_visible()
    assert not recordings.locator('[data-pane="schedule"]').is_visible()


def test_the_schedule_starts_empty_and_says_so(recordings):
    recordings.wait_for_selector("#agenda .agend, #agenda .agrow", timeout=15000)
    assert "Nothing is scheduled yet" in recordings.locator("#agenda").inner_text()


def test_a_booked_recording_appears_with_who_and_why(page):
    _follow_the_chiefs(page)
    page.goto("/recordings")
    page.wait_for_selector(".agrow", timeout=15000)
    row = page.locator(".agrow").first
    assert "Chiefs" in row.inner_text()
    assert "pass" in row.inner_text().lower(), "it says why it is recording"
    assert row.locator("i.own.ce").count() == 1


def test_the_calendar_is_the_same_data_seen_differently(page):
    _follow_the_chiefs(page)
    page.goto("/recordings")
    page.wait_for_selector(".agrow", timeout=15000)
    page.click('[data-view="calendar"]')
    page.wait_for_selector("#calgrid .calday", timeout=15000)
    assert page.locator('[data-view-pane="calendar"]').is_visible()
    assert not page.locator('[data-view-pane="agenda"]').is_visible()
    assert "Chiefs" in page.locator("#calgrid").inner_text()


def test_the_calendar_moves_month_by_month_and_comes_back(page):
    page.goto("/recordings")
    page.click('[data-view="calendar"]')
    page.wait_for_selector("#calgrid .calday")
    first = page.locator("#calmonth").inner_text()
    page.click("#calnext")
    assert page.locator("#calmonth").inner_text() != first
    page.click("#caltoday")
    assert page.locator("#calmonth").inner_text() == first


def test_a_pass_is_listed_with_its_controls(page):
    _follow_the_chiefs(page)
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    row = page.locator(".passrow").first
    assert "Kansas City Chiefs" in row.inner_text()
    assert row.locator('[data-act="edit"]').count() == 1
    assert row.locator('[data-act="toggle"]').count() == 1
    assert row.locator('[data-act="remove"]').count() == 1
    assert row.locator("i.own.ce").count() == 1
    assert "1" in page.locator("#pcount").inner_text()


def test_opening_a_pass_shows_what_it_will_record_and_why(page):
    _follow_the_chiefs(page)
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    page.click(".passrow .pmeta")
    page.wait_for_selector(".passdetail table", timeout=15000)
    detail = page.locator(".passdetail").first.inner_text()
    assert "Chiefs at Buccaneers" in detail
    assert "premiere" in detail.lower()
    assert "41.1" in detail


def test_pausing_a_pass_marks_it_paused(page):
    _follow_the_chiefs(page)
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    page.click('.passrow [data-act="toggle"]')
    page.wait_for_selector(".passrow .pill", timeout=15000)
    assert "paused" in page.locator(".passrow").first.inner_text()
    assert "Resume" in page.locator(".passrow").first.inner_text()


def test_removing_a_pass_empties_the_list(page):
    _follow_the_chiefs(page)
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    page.click('.passrow [data-act="remove"]')
    page.wait_for_selector("#passlist .empty", timeout=15000)


# ---- the add panel ----

def test_the_plus_button_opens_the_add_panel(recordings):
    recordings.click("#addrule")
    recordings.wait_for_selector("#rgo")
    assert "A recording schedule" in recordings.locator("#ovlbox").inner_text()
    assert recordings.locator("#rgo").is_disabled(), "nothing is chosen yet"


def test_the_team_list_has_a_search_box_that_filters(recordings):
    recordings.click("#addrule")
    recordings.wait_for_selector("#rlist button, #rlist .rrow", timeout=15000)
    before = recordings.locator("#rlist > *").count()
    recordings.fill("#rq", "Chiefs")
    recordings.wait_for_function(
        "n => document.querySelectorAll('#rlist > *').length < n",
        arg=before, timeout=15000)
    assert "Kansas City Chiefs" in recordings.locator("#rlist").inner_text()
    assert "Tampa" not in recordings.locator("#rlist").inner_text()


def test_the_add_panel_offers_plexs_own_settings_once_you_choose(recordings):
    recordings.click("#addrule")
    recordings.wait_for_selector("#rlist > *", timeout=15000)
    assert "options appear" in recordings.locator("#ovlbox").inner_text()
    recordings.click("#rlist >> text=Kansas City Chiefs")
    recordings.wait_for_selector('[data-set="minVideoQuality"]', timeout=15000)
    assert "Kansas City Chiefs" in recordings.locator("#rpick").inner_text()
    assert not recordings.locator("#rgo").is_disabled()


def test_the_add_panel_bar_names_who_will_own_the_rule(recordings):
    recordings.click("#addrule")
    recordings.wait_for_selector("#rlist > *", timeout=15000)
    recordings.click("#rlist >> text=Kansas City Chiefs")
    recordings.wait_for_selector('[data-set="minVideoQuality"]', timeout=15000)
    assert "Plex schedule" in recordings.locator("#optbar").inner_text()
    recordings.click("#multibtn")
    recordings.wait_for_selector("#multibody .multirow")
    recordings.check('#multibody input[data-net="ABC"]')
    assert "CouchElephant schedule" in recordings.locator("#optbar").inner_text()


def test_a_plain_plex_recording_can_be_booked_from_the_add_panel(recordings):
    from tests import fake_plex
    recordings.click("#addrule")
    recordings.wait_for_selector("#rlist > *", timeout=15000)
    recordings.click("#rlist >> text=Kansas City Chiefs")
    recordings.wait_for_selector('[data-set="minVideoQuality"]', timeout=15000)
    recordings.click("#rgo")
    recordings.wait_for_function(
        "() => !document.getElementById('rgo')", timeout=30000)
    assert fake_plex.STATE.created, "Plex was actually asked for it"
    # No source limit, so Plex holds the rule and it is listed in Plex's colour.
    recordings.click('.subtab[data-sub="passes"]')
    recordings.wait_for_selector('.passrow[data-who="plex"]', timeout=15000)


def test_switching_to_a_programme_changes_what_is_searched(recordings):
    recordings.click("#addrule")
    recordings.wait_for_selector("#rlist > *", timeout=15000)
    recordings.check('input[name=rmode][value="series"]')
    recordings.wait_for_selector("#rq")
    assert "Search programmes" in recordings.get_attribute("#rq", "placeholder")
    recordings.wait_for_function(
        "() => /Quiz Show/.test(document.getElementById('rlist').textContent)",
        timeout=15000)
