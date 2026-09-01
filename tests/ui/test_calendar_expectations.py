"""A thing you are waiting for belongs ON the calendar, and at its own precision.

The "Waiting for the Plex guide data" card told you a plan existed. The calendar, which is
where you actually look to see what a month holds, did not draw one at all, so a
month with three announced games and no bookings read as an empty month.

THE CONSTRAINT THAT SHAPES THIS. An expectation carries a `precision`: time, day,
month or year. A league gives a kickoff; an announcement often gives only a month.
A calendar cell IS a day, so putting a month-precision row in one states a
broadcast day nobody published. That is the exact thing `render_when` and the
`precision` column exist to prevent, and it does not stop being true because the
pixels are prettier.

So the rule this file pins:

  * time / day precision  -> a cell, on that day, dashed and in the waiting colour
  * month / year precision -> a band under the grid, never a cell
  * no date at all         -> neither; the plan card above already lists it

Dashed is not decoration. It is the same "a plan, not a booking" language the plan
card already uses, and `test_expectations.py` pins it there.
"""
import datetime
import time

import pytest

from app import db


def _mid_month(day):
    """Midday UTC on `day` of the month the calendar opens on.

    Midday, because the browser buckets by ITS local date and the seed is UTC.
    Any zone within +/-12h still lands on the intended day; a midnight seed
    would fall into the day before for half the world and make this flaky.
    """
    now = datetime.datetime.now(datetime.UTC)
    return int(datetime.datetime(now.year, now.month, day, 12, 0,
                                 tzinfo=datetime.UTC).timestamp())


@pytest.fixture
def calendar_waiting(page):
    """One expectation the guide can place, and one it cannot."""
    with db.tx() as c:
        cur = c.execute("INSERT INTO passes (kind, series_title, uid, enabled, "
                        "created_at) VALUES ('series', 'Calwait', 'uid-cal', 1, 1)")
        pid = cur.lastrowid
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (?, 'tvmaze', 'cal-day', 'Dayprecise Derby', ?, 'day', ?)",
                  (pid, _mid_month(12), int(time.time())))
        c.execute("INSERT INTO expectations (pass_id, source, source_id, title, "
                  "expected_at, precision, updated_at) "
                  "VALUES (?, 'tvmaze', 'cal-month', 'Monthonly Match', ?, 'month', ?)",
                  (pid, _mid_month(18), int(time.time())))
    page.goto("/recordings")
    page.wait_for_selector(".subtab")
    page.click('[data-view="calendar"]')
    page.wait_for_selector("#calgrid .calday", timeout=15000)
    return page


def test_a_dated_expectation_is_drawn_on_the_calendar(calendar_waiting):
    """The whole point: it shows up where you look for the month."""
    item = calendar_waiting.locator('[data-cal-expectation="cal-day"]')
    item.wait_for(timeout=10000)
    assert "Dayprecise Derby" in item.inner_text()


def test_it_sits_in_the_cell_for_its_own_day(calendar_waiting):
    calendar_waiting.wait_for_selector('[data-cal-expectation="cal-day"]')
    day = calendar_waiting.eval_on_selector(
        '[data-cal-expectation="cal-day"]',
        "el => el.closest('.calday').querySelector('.cald').textContent.trim()")
    assert day == "12"


def test_it_reads_as_a_plan_and_not_as_a_booking(calendar_waiting):
    """Dashed, and in its own colour, or it is indistinguishable from a booking."""
    calendar_waiting.wait_for_selector('[data-cal-expectation="cal-day"]')
    style = calendar_waiting.eval_on_selector(
        '[data-cal-expectation="cal-day"]',
        "el => getComputedStyle(el).borderLeftStyle")
    assert style == "dashed"
    booked = calendar_waiting.eval_on_selector(
        '[data-cal-expectation="cal-day"]',
        "el => getComputedStyle(el).borderLeftColor")
    other = calendar_waiting.eval_on_selector(
        ".legend i.plex", "el => getComputedStyle(el).backgroundColor")
    assert booked != other


def test_a_month_precision_row_is_never_placed_in_a_day(calendar_waiting):
    """The rule the precision column exists for. A cell is a day; a month is not."""
    calendar_waiting.wait_for_selector("#calwait")
    assert calendar_waiting.locator(
        '.calday [data-cal-expectation="cal-month"]').count() == 0


def test_a_month_precision_row_still_appears_under_the_grid(calendar_waiting):
    """Not placeable is not the same as not shown. It goes in the band."""
    band = calendar_waiting.locator("#calwait")
    band.wait_for(timeout=10000)
    assert band.is_visible()
    assert "Monthonly Match" in band.inner_text()


def test_the_band_states_the_month_and_invents_no_time(calendar_waiting):
    calendar_waiting.wait_for_selector("#calwait")
    text = calendar_waiting.locator("#calwait").inner_text()
    assert datetime.datetime.now().strftime("%B") in text
    assert "12:00" not in text
    assert "00:00" not in text


def test_the_legend_names_the_third_kind(calendar_waiting):
    """Three colours on the grid means three entries in the legend."""
    assert calendar_waiting.locator(".legend i.wait").count() == 1
    assert ("Waiting for the Plex guide data"
            in calendar_waiting.locator(".legend").inner_text())


def test_an_expectation_is_not_clickable_as_a_programme(calendar_waiting):
    """It has no airing yet. That is what waiting MEANS."""
    calendar_waiting.wait_for_selector('[data-cal-expectation="cal-day"]')
    aid = calendar_waiting.eval_on_selector(
        '[data-cal-expectation="cal-day"]', "el => el.dataset.aid || ''")
    assert aid == ""


def test_the_band_stays_hidden_when_nothing_is_loosely_dated(page):
    """An empty band would be a permanent strip of nothing under every month."""
    page.goto("/recordings")
    page.wait_for_selector(".subtab")
    page.click('[data-view="calendar"]')
    page.wait_for_selector("#calgrid .calday", timeout=15000)
    page.wait_for_timeout(600)
    assert not page.locator("#calwait").is_visible()
