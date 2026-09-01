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
