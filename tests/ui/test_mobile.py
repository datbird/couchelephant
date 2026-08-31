"""The phone layout. A separate design, so it gets its own checks."""


def test_nothing_scrolls_sideways_off_the_screen(phone):
    phone.goto("/")
    phone.wait_for_selector(".gprog")
    over = phone.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert over <= 1, "the page itself must not scroll sideways"


def test_the_tagline_gives_way_to_the_controls(phone):
    phone.goto("/")
    assert not phone.locator(".tagline").is_visible()
    assert phone.locator("#syncbtn").is_visible()
    assert phone.locator("#gearbtn").is_visible()
    assert phone.locator("#pbtn").is_visible()


def test_the_tabs_still_fill_the_bar(phone):
    phone.goto("/")
    nav = phone.locator("#ptnav").bounding_box()
    tabs = phone.locator(".pt-tab")
    total = sum(tabs.nth(i).bounding_box()["width"] for i in range(tabs.count()))
    assert total > nav["width"] * 0.9


def test_the_guide_grid_is_usable_on_a_phone(phone):
    phone.goto("/")
    phone.wait_for_selector(".gprog")
    assert phone.locator(".gch").count() >= 4
    box = phone.eval_on_selector("#gw", "el => ({sw: el.scrollWidth, cw: el.clientWidth})")
    assert box["sw"] > box["cw"], "time scrolls inside the grid, not the page"


def test_a_programme_panel_fits_the_screen(phone):
    phone.goto("/")
    phone.wait_for_selector(".gprog")
    phone.tap('.gprog.live[title*="Chiefs"]')
    phone.wait_for_selector("#ovlbox .air")
    box = phone.locator("#ovlbox").bounding_box()
    assert box["width"] <= 390
    assert box["x"] >= 0


def test_the_source_picker_does_not_steal_focus_on_a_touch_screen(phone):
    """An autofocused search box throws the on-screen keyboard over the list
    the moment it opens, which is the whole control."""
    phone.goto("/recordings")
    phone.tap("#addrule")
    phone.wait_for_selector("#multibtn")
    phone.tap("#multibtn")
    phone.wait_for_selector("#multibody")
    assert phone.evaluate("() => document.activeElement.id") != "multiq"


def test_the_agenda_row_stacks_rather_than_squeezing(phone):
    phone.goto("/recordings")
    phone.wait_for_selector(".subtab")
    assert phone.locator('[data-pane="schedule"]').is_visible()
    over = phone.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert over <= 1
