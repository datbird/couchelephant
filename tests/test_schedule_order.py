"""The schedule reads outward from now, not from the start of recorded time.

Plain ascending put the oldest finished recording at the top. On a server whose
season has not started that is a month of "complete" rows above the thing you
opened the page to see.

Plain descending is not the answer either. It reads correctly only while
everything is in the past: book a season and it leads with next January and
buries tonight's game at the bottom.

So the two halves are ordered separately. What is still to come comes first,
soonest at the top, because that is what a schedule is for. What already aired
follows, most recent first, because that is what history is for.
"""
import time

from app import db
from app.routes.passes import _schedule_rows

NOW = int(time.time())
DAY = 86400


def _grab(grab_id, when, title):
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, begins_at, title, status) "
                  "VALUES (?, ?, ?, 'scheduled')", (grab_id, when, title))


def _titles():
    return [r["title"] for r in _schedule_rows()]


def test_history_reads_newest_first():
    _grab("a", NOW - 12 * DAY, "oldest")
    _grab("b", NOW - 5 * DAY, "middle")
    _grab("c", NOW - 1 * DAY, "newest")
    assert _titles() == ["newest", "middle", "oldest"]


def test_what_is_still_to_come_reads_soonest_first():
    _grab("a", NOW + 120 * DAY, "january")
    _grab("b", NOW + 2 * DAY, "tonight")
    _grab("c", NOW + 30 * DAY, "next month")
    assert _titles() == ["tonight", "next month", "january"]


def test_the_future_sits_above_the_past():
    """Both rules at once, which is the whole point. A booked season must not
    push tonight's game below three months of fixtures, and finished
    recordings must not push it below a month of history."""
    _grab("old", NOW - 10 * DAY, "aired last week")
    _grab("older", NOW - 40 * DAY, "aired last month")
    _grab("soon", NOW + 1 * DAY, "tomorrow")
    _grab("later", NOW + 100 * DAY, "in january")
    assert _titles() == ["tomorrow", "in january",
                         "aired last week", "aired last month"]


def test_a_grab_with_no_time_is_not_lost():
    """`begins_at` can be null. It coalesces to 0, which is the distant past,
    so it sorts last rather than vanishing."""
    _grab("dated", NOW - DAY, "dated")
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, begins_at, title, status) "
                  "VALUES ('undated', NULL, 'undated', 'scheduled')")
    assert _titles() == ["dated", "undated"]
