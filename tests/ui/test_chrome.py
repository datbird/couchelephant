"""The frame around every page: header, tabs, theme, account menu."""


def test_the_guide_is_the_landing_page(page):
    page.goto("/")
    assert "CouchElephant" in page.title()
    assert page.locator(".pt-tab.active").inner_text().strip() == "Guide"


def test_there_are_exactly_two_tabs_and_they_fill_the_bar(page):
    page.goto("/")
    tabs = page.locator(".pt-tab")
    assert tabs.count() == 2
    nav = page.locator("#ptnav").bounding_box()
    total = sum(tabs.nth(i).bounding_box()["width"] for i in range(2))
    # Centred and full width, the way Ludodex does it.
    assert total > nav["width"] * 0.9


def test_the_tabs_navigate(page):
    page.goto("/")
    page.click('.pt-tab[data-t="recordings"]')
    page.wait_for_url("**/recordings")
    assert page.locator(".pt-tab.active").inner_text().strip() == "Recordings"


def test_a_divider_separates_the_header_from_the_tabs(page):
    page.goto("/")
    # A border, not a spacer element, so it survives a theme change. It sits
    # on the header, between it and the tabs.
    w = page.eval_on_selector("header", "el => getComputedStyle(el).borderBottomWidth")
    colour = page.eval_on_selector("header", "el => getComputedStyle(el).borderBottomColor")
    assert w not in ("", "0px")
    bg = page.eval_on_selector("header", "el => getComputedStyle(el).backgroundColor")
    assert colour != bg, "a divider the same colour as the bar is not a divider"


def test_the_header_holds_sync_settings_and_account_in_that_order(page):
    page.goto("/")
    order = page.eval_on_selector_all(
        "header .right button, header .right a",
        "els => els.map(e => e.getAttribute('aria-label') || e.id)")
    order = [o for o in order if o]
    assert order.index("Sync the guide now") < order.index("Settings")
    assert order.index("Settings") < order.index("pbtn")


def test_the_sync_time_is_a_tooltip_not_a_line_of_text(page):
    page.goto("/")
    tip = page.get_attribute('[aria-label="Sync the guide now"]', "title")
    assert "sync" in tip.lower()
    assert "Never synced" in tip or "Last synced" in tip
    assert "synced" not in page.locator("header").inner_text().lower()


def test_the_account_menu_opens_and_says_sign_in_is_off(page):
    page.goto("/")
    assert not page.locator("#pmenu").is_visible()
    page.click("#pbtn")
    menu = page.locator("#pmenu")
    assert menu.is_visible()
    assert "Guest" in menu.inner_text()
    assert "Sign-in is off" in menu.inner_text()


def test_the_account_menu_closes_when_you_click_away(page):
    page.goto("/")
    page.click("#pbtn")
    page.click("main", position={"x": 5, "y": 5})
    assert not page.locator("#pmenu").is_visible()


def test_the_theme_toggle_flips_the_page_and_is_remembered(page):
    page.goto("/")
    assert page.eval_on_selector(
        "html", "el => el.getAttribute('data-theme')") in (None, "dark")
    dark = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")

    page.click("#pbtn")
    page.click("#themebtn")
    assert page.eval_on_selector("html", "el => el.getAttribute('data-theme')") == "light"
    light = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")
    assert light != dark, "a theme that does not change the background is not a theme"

    page.reload()
    assert page.eval_on_selector("html", "el => el.getAttribute('data-theme')") == "light"


def test_a_light_system_preference_is_honoured_on_a_first_visit(light_system):
    light_system.goto("/")
    assert light_system.eval_on_selector(
        "html", "el => el.getAttribute('data-theme')") == "light"


def test_the_theme_reaches_the_browser_chrome_too(page):
    page.goto("/")
    before = page.get_attribute("#themecolor", "content")
    page.click("#pbtn")
    page.click("#themebtn")
    assert page.get_attribute("#themecolor", "content") != before


def test_preview_mode_is_announced_in_the_header(page):
    from app import db
    db.set_setting("dry_run", "1")
    page.goto("/")
    assert "PREVIEW MODE" in page.locator("header").inner_text()
