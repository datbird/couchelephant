"""What a brand new install looks like, and the account modes."""
import pytest


@pytest.fixture
def fresh(page):
    from app import db
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    page.goto("/")
    # By id: the header carries a form of its own now, hidden in the notice
    # panel, and it resolves first.
    page.wait_for_selector("#wform")
    return page


def test_an_unconfigured_install_lands_on_the_setup_screen(fresh):
    assert "/welcome" in fresh.url
    assert "Set up CouchElephant" in fresh.locator("body").inner_text()


def test_it_asks_for_the_timezone_the_plex_server_is_in(fresh):
    assert fresh.locator('select[name="timezone"]').count() == 1
    # Labels are uppercased by the stylesheet, so compare on the words.
    assert "TIMEZONE" in fresh.locator('label[for="wz"]').inner_text().upper()


def test_it_says_accounts_can_wait(fresh):
    text = fresh.locator("body").inner_text()
    assert "Cloudflare Access" in text
    assert "Settings" in text


def test_a_server_it_cannot_reach_is_reported_rather_than_saved(fresh):
    from app import db
    fresh.fill('input[name="plex_url"]', "http://127.0.0.1:1")
    fresh.fill('input[name="plex_token"]', "nope")
    fresh.click("#wform button")
    fresh.wait_for_function(
        "() => { var v = document.getElementById('wverdict');"
        "  return v && !v.hidden && !v.className.includes('busy'); }",
        timeout=30000)
    assert "Could not reach" in fresh.locator("body").inner_text()
    assert db.get_setting("plex_url") == ""


def test_a_server_that_answers_is_saved_and_lets_you_in(fresh, plex_url):
    fresh.fill('input[name="plex_url"]', plex_url)
    fresh.fill('input[name="plex_token"]', "test-token")
    fresh.click("#wform button")
    fresh.wait_for_url(lambda u: "/welcome" not in u, timeout=30000)
    assert fresh.locator("#ptnav").count() == 1


def test_a_new_install_asks_nobody_to_sign_in(page):
    page.goto("/")
    page.click("#pbtn")
    assert "Sign-in is off" in page.locator("#pmenu").inner_text()
    assert page.locator('#pmenu a[href="/login"]').count() == 0


def test_turning_on_local_accounts_puts_a_sign_in_in_front(page):
    from app import auth, db
    db.set_setting("auth_mode", "local")
    auth.create_user("someone", "a-long-enough-password", role="admin")
    page.goto("/")
    page.wait_for_url("**/login**", timeout=15000)
    assert page.locator('input[name="password"]').count() == 1


def test_a_wrong_password_says_so_without_saying_which_half(page):
    from app import auth, db
    db.set_setting("auth_mode", "local")
    auth.create_user("someone", "a-long-enough-password", role="admin")
    page.goto("/login")
    page.fill('input[name="username"]', "someone")
    page.fill('input[name="password"]', "wrong")
    page.click(".signin-card form button")
    page.wait_for_selector(".signin-card .banner.bad", timeout=15000)
    text = page.locator("body").inner_text()
    assert "do not match" in text.lower()
    assert "no such user" not in text.lower()


def test_signing_in_gets_you_the_guide_and_your_name(page):
    from app import auth, db
    db.set_setting("auth_mode", "local")
    auth.create_user("someone", "a-long-enough-password", role="admin")
    page.goto("/login")
    page.fill('input[name="username"]', "someone")
    page.fill('input[name="password"]', "a-long-enough-password")
    page.click(".signin-card form button")
    page.wait_for_selector("#ptnav", timeout=15000)
    page.click("#pbtn")
    menu = page.locator("#pmenu").inner_text()
    assert "someone" in menu
    assert "Administrator" in menu
    assert "Sign out" in menu


def test_a_signed_in_theme_follows_the_account_not_the_browser(page, browser, base_url):
    from app import auth, db
    db.set_setting("auth_mode", "local")
    auth.create_user("someone", "a-long-enough-password", role="admin")
    page.goto("/login")
    page.fill('input[name="username"]', "someone")
    page.fill('input[name="password"]', "a-long-enough-password")
    page.click(".signin-card form button")
    page.wait_for_selector("#ptnav", timeout=15000)
    page.click("#pbtn")
    page.click("#themebtn")
    page.wait_for_timeout(500)

    # A different browser entirely, same account.
    ctx = browser.new_context(base_url=base_url, color_scheme="dark")
    other = ctx.new_page()
    other.goto("/login")
    other.fill('input[name="username"]', "someone")
    other.fill('input[name="password"]', "a-long-enough-password")
    other.click(".signin-card form button")
    other.wait_for_selector("#ptnav", timeout=15000)
    assert other.eval_on_selector("html", "el => el.getAttribute('data-theme')") == "light"
    assert other.eval_on_selector(
        "html", "el => el.getAttribute('data-theme-from')") == "account"
    ctx.close()


def test_the_timezone_picker_starts_on_utc(fresh):
    """The default must be in the list, or the browser shows the first entry."""
    assert fresh.locator("#wz").input_value() == "UTC"
