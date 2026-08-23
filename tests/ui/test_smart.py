"""The Smart Pass panel: the two choices, the builder, and the guard."""
import pytest


@pytest.fixture
def add(page):
    page.goto("/recordings")
    page.click("#addrule")
    page.wait_for_selector("input[name=rmode]")
    return page


def _filter(page):
    page.check('input[name=rmode][value="smart"]')
    page.check('input[name=rsub][value="filter"]')
    page.wait_for_selector("#sfroot .sfrow")
    return page


def test_the_two_choices_are_named_as_asked(add):
    labels = add.eval_on_selector_all(
        "label.pickrow:has(input[name=rmode]) > span:first-of-type",
        "els => els.map(e => e.textContent.trim())")
    assert labels == ["Smart Pass", "Programme or Series"]
    text = add.locator("#ovlbox").inner_text()
    assert "sports teams, or a smart filter" in text


def test_smart_pass_offers_a_team_or_a_filter(add):
    add.check('input[name=rmode][value="smart"]')
    labels = add.eval_on_selector_all(
        "label.pickrow:has(input[name=rsub]) > span:first-of-type",
        "els => els.map(e => e.textContent.trim())")
    assert labels == ["Sports team", "Smart filter"]


def test_a_programme_pass_has_no_sub_choice(add):
    add.check('input[name=rmode][value="series"]')
    assert add.locator("input[name=rsub]").count() == 0
    assert "Search programmes" in add.get_attribute("#rq", "placeholder")


def test_the_sports_team_route_is_unchanged(add):
    add.check('input[name=rmode][value="smart"]')
    add.check('input[name=rsub][value="team"]')
    add.wait_for_selector("#rlist > *", timeout=15000)
    assert "teams" in add.get_attribute("#rq", "placeholder")
    add.click("#rlist >> text=Kansas City Chiefs")
    add.wait_for_selector('[data-set="minVideoQuality"]', timeout=15000)
    assert "Kansas City Chiefs" in add.locator("#rpick").inner_text()


# ---- the builder ----

def test_the_filter_starts_with_one_empty_condition(add):
    _filter(add)
    assert add.locator("#sfroot .sfgroup").count() == 1
    assert add.locator("#sfroot .sfrow").count() == 1
    assert add.locator("#sfroot .sfhead select").input_value() == "all"


def test_the_fields_offered_are_the_ones_the_guide_can_answer(add):
    _filter(add)
    fields = add.eval_on_selector_all(
        "#sfroot .sffield option", "els => els.map(e => e.value)")
    for expect in ("genre", "rating", "title", "year", "duration", "channel", "hd"):
        assert expect in fields


def test_choosing_a_field_offers_only_its_own_comparisons(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "year")
    cmps = add.eval_on_selector_all(
        "#sfroot .sfcmp option", "els => els.map(e => e.value)")
    assert "gt" in cmps
    assert "contains" not in cmps, "a year cannot contain anything"


def test_a_field_with_a_known_list_offers_a_dropdown(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "genre")
    add.wait_for_selector("select.sfval")
    values = add.eval_on_selector_all(
        "select.sfval option", "els => els.map(e => e.value)")
    assert "Football" in values


def test_changing_the_field_clears_a_value_that_no_longer_means_anything(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "title")
    add.fill("#sfroot input.sfval", "Quiz")
    add.select_option("#sfroot .sffield", "year")
    assert add.input_value("#sfroot .sfval") == ""


def test_conditions_and_groups_can_be_added_and_removed(add):
    _filter(add)
    add.click('#sfroot [data-act="addcond"]')
    assert add.locator("#sfroot .sfrow").count() == 2
    add.click('#sfroot .sfgroup >> [data-act="addgroup"]')
    assert add.locator("#sfroot .sfgroup").count() == 2
    add.click('#sfroot .sfgroup .sfgroup [data-act="del"]')
    assert add.locator("#sfroot .sfgroup").count() == 1
    add.click('#sfroot .sfrow .sfx')
    assert add.locator("#sfroot .sfrow").count() == 1


def test_groups_nest_more_than_two_deep(add):
    _filter(add)
    add.click('#sfroot > .sfgroup > .sfacts [data-act="addgroup"]')
    add.wait_for_selector("#sfroot .sfgroup .sfgroup")
    add.click('#sfroot .sfgroup .sfgroup > .sfacts [data-act="addgroup"]')
    assert add.locator("#sfroot .sfgroup .sfgroup .sfgroup").count() == 1


def test_every_condition_offers_what_a_blank_means(add):
    _filter(add)
    assert add.locator('#sfroot input[data-k="blank"]').count() == 1
    tip = add.get_attribute("#sfroot .sfblank", "title")
    assert "does not match" in tip


def test_the_panel_says_how_much_of_the_guide_carries_a_rating(add):
    _filter(add)
    assert "content rating for" in add.locator("#ovlbox").inner_text()


# ---- the count and the guard ----

def test_the_count_says_what_would_be_recorded(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "genre")
    add.select_option("select.sfval", "Football")
    add.wait_for_function(
        "() => /programme/.test(document.getElementById('sfcount').textContent)",
        timeout=15000)
    text = add.locator("#sfcount").inner_text()
    assert "1" in text
    assert "would be recorded" in text
    assert "Chiefs at Buccaneers" in add.locator("#sfsample").inner_text()


def test_a_loose_filter_is_marked_and_the_button_says_the_number(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "hd")
    add.wait_for_function(
        "() => document.getElementById('sfcount').className.includes('warn')"
        " || document.getElementById('sfcount').className.includes('bad')",
        timeout=15000)
    assert "most of the guide" in add.locator("#sfcount").inner_text()
    add.wait_for_function(
        "() => /Create [0-9]+ recordings/.test(document.getElementById('rgo').textContent)",
        timeout=15000)


def test_the_first_press_asks_and_the_second_creates(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "hd")
    add.wait_for_function(
        "() => /Create [0-9]+ recordings/.test(document.getElementById('rgo').textContent)",
        timeout=15000)

    add.click("#rgo")
    add.wait_for_function(
        "() => /Press Create again/.test(document.getElementById('ovlmsg').textContent)",
        timeout=20000)
    from app import db
    assert not db.query("SELECT 1 FROM passes"), "the first press creates nothing"

    add.click("#rgo")
    add.wait_for_function(
        "() => document.getElementById('rgo').textContent === 'Done'", timeout=30000)
    assert db.one("SELECT 1 FROM passes WHERE kind='smart'")


def test_a_narrow_filter_creates_on_the_first_press(add):
    _filter(add)
    add.select_option("#sfroot .sffield", "genre")
    add.select_option("select.sfval", "Football")
    add.fill("#sfname", "Chiefs football")
    add.wait_for_function(
        "() => document.getElementById('rgo').textContent === 'Create schedule'",
        timeout=15000)
    add.click("#rgo")
    add.wait_for_function(
        "() => document.getElementById('rgo').textContent === 'Done'", timeout=30000)
    from app import db
    assert db.one("SELECT label FROM passes WHERE kind='smart'")["label"] == \
        "Chiefs football"


def test_the_bar_says_couchelephant_owns_every_smart_filter(add):
    _filter(add)
    assert "CouchElephant schedule" in add.locator("#optbar").inner_text()
    assert "cannot be given conditions" in add.locator("#optbar").inner_text()


def test_the_create_button_waits_for_a_condition_worth_asking(add):
    _filter(add)
    assert add.locator("#rgo").is_disabled()


# ---- it shows up afterwards ----

def test_a_smart_pass_appears_in_the_list_with_its_own_icon(page):
    from app import db, passes, web
    web._make_pass("smart", smart={"field": "genre", "cmp": "is", "value": "Football"},
                   name="Chiefs football")
    passes.run_passes()
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    row = page.locator(".passrow").first
    assert "Chiefs football" in row.inner_text()
    assert row.locator(".kind.smart").count() == 1
    assert "genre is Football" in row.inner_text()


def test_opening_a_smart_pass_shows_what_it_records_and_why(page):
    from app import passes, web
    web._make_pass("smart", smart={"field": "genre", "cmp": "is", "value": "Football"},
                   name="Chiefs football")
    passes.run_passes()
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    page.click(".passrow .pmeta")
    page.wait_for_selector(".passdetail table", timeout=15000)
    detail = page.locator(".passdetail").first.inner_text()
    assert "Chiefs at Buccaneers" in detail
    assert "premiere" in detail.lower()


def test_editing_a_smart_pass_opens_its_conditions(page):
    from app import web
    web._make_pass("smart", smart={"field": "genre", "cmp": "is", "value": "Football"},
                   name="Chiefs football")
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    page.click('.passrow [data-act="edit"]')
    page.wait_for_selector("#sfroot .sfrow", timeout=15000)
    assert page.eval_on_selector("#sfroot .sffield", "e => e.value") == "genre"
    assert page.input_value("#sfname") == "Chiefs football"
    assert page.locator("input[name=rmode]").count() == 0, "what it is stays fixed"


# ---- Plex's own settings on a smart pass ----

def _ready_filter(page):
    _filter(page)
    page.select_option("#sfroot .sffield", "genre")
    page.select_option("select.sfval", "Football")
    page.wait_for_function(
        "() => /programme/.test(document.getElementById('sfcount').textContent)",
        timeout=20000)
    return page


def test_a_smart_pass_offers_plexs_own_settings(add):
    """Padding above all. A game that runs long is cut off without it."""
    _ready_filter(add)
    add.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    ids = add.eval_on_selector_all("[data-set]", "els => els.map(e => e.dataset.set)")
    assert "startOffsetMinutes" in ids
    assert "endOffsetMinutes" in ids
    assert "minVideoQuality" in ids
    text = add.locator("#ovlbox").inner_text()
    assert "Minutes after end" in text


def test_it_does_not_offer_what_a_pass_cannot_honour(add):
    _ready_filter(add)
    add.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    ids = add.eval_on_selector_all("[data-set]", "els => els.map(e => e.dataset.set)")
    for gone in ("onlyNewAirings", "lineupChannel", "startTimeslot", "oneShot"):
        assert gone not in ids


def test_a_sports_filter_arrives_with_padding_filled_in(add):
    _ready_filter(add)
    add.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    assert add.input_value('[data-set="endOffsetMinutes"]') == "30"
    assert "Sport overruns" in add.locator("#ovlbox").inner_text()


def test_the_padding_you_set_is_what_gets_booked(add):
    from tests import fake_plex
    _ready_filter(add)
    add.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    add.fill('[data-set="endOffsetMinutes"]', "45")
    add.fill("#sfname", "Football")
    add.click("#rgo")
    add.wait_for_function(
        "() => document.getElementById('rgo').textContent === 'Done'", timeout=30000)
    assert fake_plex.STATE.created[0]["prefs"]["endOffsetMinutes"] == "45"


def test_editing_a_smart_pass_shows_the_padding_it_saved(page):
    from app import web
    web._make_pass("smart", smart={"field": "genre", "cmp": "is", "value": "Football"},
                   name="Football", prefs={"endOffsetMinutes": "90"})
    page.goto("/recordings")
    page.click('.subtab[data-sub="passes"]')
    page.wait_for_selector(".passrow", timeout=15000)
    page.click('.passrow [data-act="edit"]')
    page.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    assert page.input_value('[data-set="endOffsetMinutes"]') == "90"


def test_padding_shows_plexs_own_words_and_reaches_two_hours(add):
    _ready_filter(add)
    add.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    tip = add.get_attribute('label.optrow:has([data-set="endOffsetMinutes"]) .opthelp',
                            "data-tip")
    assert "adding minutes after" in tip

    # A suggestion list, not a limit.
    listed = add.eval_on_selector_all(
        '#set_endOffsetMinutes_opts option', "els => els.map(e => e.value)")
    assert "120" in listed

    # And the field still takes a number nobody suggested.
    add.fill('[data-set="endOffsetMinutes"]', "240")
    assert add.input_value('[data-set="endOffsetMinutes"]') == "240"


def test_an_explanation_is_a_tooltip_not_a_second_line(add):
    """Plex's summaries run to twenty lines. Inline, one row was taller than
    the panel."""
    _ready_filter(add)
    add.wait_for_selector('[data-set="comskipMethod"]', timeout=20000)

    marks = add.locator(".optgrid .opthelp")
    assert marks.count() >= 4, "each Plex setting carries its explanation"
    assert "Attempt to automatically detect" not in add.locator(".optgrid").inner_text()
    assert add.locator(".tipbox.on").count() == 0, "and nothing is shown until asked"

    # The words are still there, on the mark.
    tip = add.get_attribute('label.optrow:has([data-set="comskipMethod"]) .opthelp',
                            "data-tip")
    assert "detect and remove commercials" in tip


def test_no_option_row_is_taller_than_a_row(add):
    _ready_filter(add)
    add.wait_for_selector('[data-set="comskipMethod"]', timeout=20000)
    heights = add.eval_on_selector_all(
        ".optgrid .optrow:not(.wide)", "els => els.map(e => e.offsetHeight)")
    assert heights
    assert max(heights) <= 64, f"a row grew to {max(heights)}px"


def test_the_tooltip_appears_on_hover(add):
    _ready_filter(add)
    add.wait_for_selector('[data-set="endOffsetMinutes"]', timeout=20000)
    assert add.locator(".tipbox.on").count() == 0
    add.locator('label.optrow:has([data-set="endOffsetMinutes"]) .opthelp').hover()
    add.wait_for_selector(".tipbox.on", timeout=5000)
    assert "adding minutes after" in add.locator(".tipbox").inner_text()


def test_the_tooltip_is_not_clipped_by_the_panel(add):
    """It was a pseudo-element inside a box that scrolls, and a scrolling
    ancestor clips its children. The words were cut off at the panel edge."""
    _ready_filter(add)
    add.wait_for_selector('[data-set="minVideoQuality"]', timeout=20000)
    add.locator('label.optrow:has([data-set="minVideoQuality"]) .opthelp').hover()
    add.wait_for_selector(".tipbox.on", timeout=5000)

    box = add.locator(".tipbox").bounding_box()
    size = add.viewport_size
    assert box["x"] >= 0, "clipped on the left"
    assert box["x"] + box["width"] <= size["width"], "clipped on the right"
    assert box["y"] >= 0 and box["y"] + box["height"] <= size["height"]
    # And it is on the body, so no ancestor can clip it.
    assert add.eval_on_selector(
        ".tipbox", "e => e.parentElement.tagName") == "BODY"


def test_the_tooltip_follows_the_row_when_the_panel_scrolls(add):
    """Hiding on scroll was the first attempt, and it is wrong twice over:
    pointing at a mark scrolls that mark into view, so the tooltip killed
    itself the moment it appeared."""
    _ready_filter(add)
    add.wait_for_selector('[data-set="minVideoQuality"]', timeout=20000)
    mark = 'label.optrow:has([data-set="minVideoQuality"]) .opthelp'
    add.locator(mark).hover()
    add.wait_for_selector(".tipbox.on", timeout=5000)

    def gap():
        m = add.locator(mark).bounding_box()
        t = add.locator(".tipbox").bounding_box()
        return round(abs((m["y"] + m["height"] / 2) - (t["y"] + t["height"] / 2)))

    before = gap()
    add.eval_on_selector("#ovlbox", "e => e.scrollBy(0, 60)")
    add.wait_for_timeout(200)
    assert add.locator(".tipbox.on").count() == 1, "it stayed up"
    assert abs(gap() - before) <= 4, "and it moved with the row"


def test_the_record_panel_and_the_pass_panel_look_the_same(page):
    """Same component, so the guide's Record panel gets the tooltips too."""
    page.goto("/")
    page.wait_for_selector(".gprog")
    page.click('.gprog.live[title*="Chiefs"]')
    page.wait_for_selector("#ovlbox .air")
    page.click("#ovlbox .air.best [data-rec]")
    page.wait_for_selector("#optgo")
    assert page.locator(".optgrid .opthelp").count() >= 3
    heights = page.eval_on_selector_all(
        ".optgrid .optrow:not(.wide)", "els => els.map(e => e.offsetHeight)")
    assert max(heights) <= 64


def test_no_option_label_is_truncated(add):
    """A control sized on its content squeezed the label instead of itself."""
    _ready_filter(add)
    add.wait_for_selector('[data-set="comskipMethod"]', timeout=20000)
    clipped = add.eval_on_selector_all(
        ".optgrid .optname",
        "els => els.filter(e => e.scrollWidth > e.clientWidth + 1)"
        ".map(e => e.textContent)")
    assert clipped == [], f"labels cut off: {clipped}"


def test_the_options_stay_two_columns_of_single_rows(add):
    """The complaint that produced this test: the explanations turned a tidy
    two-column grid into one long scroll."""
    _ready_filter(add)
    add.wait_for_selector('[data-set="comskipMethod"]', timeout=20000)
    cols = add.eval_on_selector(
        ".optgrid", "e => getComputedStyle(e).gridTemplateColumns.split(' ').length")
    assert cols == 2
    heights = add.eval_on_selector_all(
        ".optgrid .optrow", "els => els.map(e => e.offsetHeight)")
    assert len(set(heights)) == 1, f"rows are ragged: {sorted(set(heights))}"
