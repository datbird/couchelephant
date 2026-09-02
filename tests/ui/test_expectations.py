"""A thing you are waiting for has to look different from a thing you booked.

The server side of this is covered elsewhere. What only a browser can answer
is whether the difference is visible, and whether a month is ever drawn as a
broadcast time nobody published.
"""
import pytest

from app import db, health

WHEN = 1804204800


@pytest.fixture
def waiting(page):
    with db.tx() as c:
        # An expectation only counts while its pass exists and is enabled, so
        # the pass has to be here too.
        cur = c.execute("INSERT INTO passes (kind, series_title, uid, enabled, "
                        "created_at) VALUES ('series', 'Gobiligook', 'uid-ui', "
                        "1, 1)")
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (?, 'tvmaze', 'ui-1', 'Gobiligook', ?, 'month', 1)",
                  (cur.lastrowid, WHEN))
    page.goto("/recordings")
    page.wait_for_selector("header")
    return page


def test_it_shows_as_a_plan_and_not_as_a_booking(waiting):
    row = waiting.locator('[data-expectation="ui-1"]')
    row.wait_for(timeout=5000)
    assert "Gobiligook" in row.inner_text()
    # A dashed border is the whole visual difference. A solid one would read
    # as a booking.
    style = waiting.eval_on_selector('[data-expectation="ui-1"]',
                                     "el => getComputedStyle(el).borderTopStyle")
    assert style == "dashed"


def test_a_month_is_drawn_as_a_month(waiting):
    """The user must never read an invented midnight as a real broadcast."""
    waiting.wait_for_selector('[data-expectation="ui-1"]')
    text = waiting.locator('[data-expectation="ui-1"]').inner_text()
    assert "2027" in text
    assert "12:00" not in text
    assert "00:00" not in text


def _seed_a_season(page):
    """Twenty waiting rows on one pass, the shape a followed NFL team makes."""
    with db.tx() as c:
        cur = c.execute("INSERT INTO passes (kind, series_title, uid, enabled, "
                        "created_at) VALUES ('series', 'Longrun', 'uid-long', "
                        "1, 1)")
        for n in range(1, 21):
            c.execute("INSERT INTO expectations (pass_id, source, source_id, "
                      "title, subtitle, expected_at, precision, updated_at) "
                      "VALUES (?, 'tvmaze', ?, 'Longrun', ?, ?, 'day', 1)",
                      (cur.lastrowid, f"ep-{n}", f"S01E{n:02d}",
                       WHEN + n * 86400))
    page.goto("/recordings")
    page.wait_for_selector("#planmore", timeout=10000)
    return page


def test_the_card_opens_folded_to_the_next_three(page):
    """A followed team hands this card its whole season. Seventeen games, and
    several passes, would turn the top of the page into a wall."""
    page = _seed_a_season(page)
    assert page.locator("#planlist .plan:visible").count() == 3


def test_the_rest_are_rendered_and_not_thrown_away(page):
    """A fold, not a limit. Expanding must not need a second request, and the
    DATA is never capped: the calendar draws every one of them."""
    page = _seed_a_season(page)
    assert page.locator("#planlist .plan").count() == 20


def test_the_control_says_how_many_are_hidden(page):
    page = _seed_a_season(page)
    assert "Show all 20" in page.locator("#planmore").inner_text()
    assert page.locator("#planmore").get_attribute("aria-expanded") == "false"


def test_expanding_shows_the_whole_season(page):
    page = _seed_a_season(page)
    page.click("#planmore")
    assert page.locator("#planlist .plan:visible").count() == 20
    assert "Show fewer" in page.locator("#planmore").inner_text()
    assert page.locator("#planmore").get_attribute("aria-expanded") == "true"


def test_it_folds_back_up_again(page):
    page = _seed_a_season(page)
    page.click("#planmore")
    page.click("#planmore")
    assert page.locator("#planlist .plan:visible").count() == 3


def test_no_fold_control_when_everything_already_fits(waiting):
    """One waiting row needs no "show all"."""
    assert waiting.locator("#planmore").count() == 0
    assert waiting.locator("#planlist .plan:visible").count() == 1


def test_the_card_stays_hidden_when_nothing_is_waiting(page):
    page.goto("/recordings")
    page.wait_for_selector("header")
    page.wait_for_timeout(500)
    assert not page.locator("#plancard").is_visible()


def test_only_a_suggestion_offers_a_way_to_wave_it_off(page):
    health.record([
        {"code": health.KEYS_AVAILABLE, "severity": health.TIP,
         "title": "Keys", "detail": "D", "hint": "H"},
        {"code": health.EPG_STALE, "severity": "bad",
         "title": "Stale", "detail": "D", "hint": "H"},
    ], 100, owns=frozenset({health.KEYS_AVAILABLE, health.EPG_STALE}))
    page.goto("/")
    page.wait_for_selector("#noticebtn")
    page.click("#noticebtn")
    page.wait_for_selector("#noticemenu.open")
    assert page.locator(f'[data-dismiss="{health.KEYS_AVAILABLE}"]').count() == 1
    assert page.locator(f'[data-dismiss="{health.EPG_STALE}"]').count() == 0


def test_waving_off_the_suggestion_makes_it_go_away(page):
    health.record([{"code": health.KEYS_AVAILABLE, "severity": health.TIP,
                    "title": "Keys", "detail": "D", "hint": "H"}], 100,
                  owns=frozenset({health.KEYS_AVAILABLE}))
    page.goto("/")
    page.wait_for_selector("#noticebtn")
    page.click("#noticebtn")
    page.wait_for_selector("#noticemenu.open")
    page.click(f'[data-dismiss="{health.KEYS_AVAILABLE}"]')
    page.wait_for_timeout(1200)
    assert health.KEYS_AVAILABLE not in {n["code"] for n in health.open_notices()}
