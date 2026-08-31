"""What a pass is still waiting for.

An expectation is an intention, never a booking. It says "you asked to follow
this, and a source outside Plex thinks it happens then". Only a guide airing
carries a channel, so only a guide airing can be recorded. Nothing in here
schedules anything.
"""
import datetime
import time
import zoneinfo

from . import db

WHEN_UNKNOWN = "date not announced"

# What each precision is allowed to show. Anything more is invented. A source
# that said "2027-03" did not say the first of March at midnight, and a reader
# takes an invented date for a real one.
_FORMATS = {
    "time": "%a %b %-d, %Y at %-I:%M %p",
    "day": "%a %b %-d, %Y",
    "month": "%B %Y",
    "year": "%Y",
}


def store(pass_id: int, items, now: int | None = None) -> int:
    """Write or refresh what this pass is waiting for.

    Upserts, so re-importing a season updates its games rather than piling up
    a second copy of every one of them.
    """
    now = int(now if now is not None else time.time())
    written = 0
    with db.tx() as c:
        for item in items:
            c.execute(
                """INSERT INTO expectations (pass_id, source, source_id, title,
                       subtitle, network, expected_at, precision, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source, source_id, pass_id) DO UPDATE SET
                     title=excluded.title, subtitle=excluded.subtitle,
                     network=excluded.network, expected_at=excluded.expected_at,
                     precision=excluded.precision,
                     updated_at=excluded.updated_at""",
                (pass_id, item.source, item.source_id, item.title, item.subtitle,
                 item.network, item.expected_at, item.precision, now))
            written += 1
    return written


def waiting(pass_id: int | None = None) -> list[dict]:
    """Expectations the guide has not confirmed yet, soonest first.

    A row with no date sorts last rather than first, which is where a `NULL`
    would otherwise land it.
    """
    sql = ("SELECT * FROM expectations WHERE matched_guid IS NULL{extra} "
           "ORDER BY COALESCE(expected_at, 1 << 40), title")
    if pass_id is None:
        return [dict(r) for r in db.query(sql.format(extra=""))]
    return [dict(r) for r in db.query(sql.format(extra=" AND pass_id = ?"),
                                      (pass_id,))]


def render_when(expected_at: int | None, precision: str, tz: str) -> str:
    """Say the date at exactly the precision the source gave, and no more.

    This is the honest half of the feature. A month rendered as a midnight is
    a broadcast time nobody published, and the user would plan around it.
    """
    if not expected_at:
        return WHEN_UNKNOWN
    fmt = _FORMATS.get(precision) or _FORMATS["year"]
    try:
        zone = zoneinfo.ZoneInfo(tz or "UTC")
    except Exception:
        # A bad timezone setting must not take the page down with it.
        zone = zoneinfo.ZoneInfo("UTC")
    when = datetime.datetime.fromtimestamp(expected_at, zone)
    try:
        return when.strftime(fmt)
    except ValueError:
        # Some libc builds reject the %-d dash-padding form.
        return when.strftime(fmt.replace("%-", "%"))


# How far either side of an expected date a guide airing may sit and still be
# the same broadcast. A published kickoff should land within a day of what the
# league said. A month-precision guess is a whole month wide by definition.
#
# The window is what stops a title that repeats next season from binding to an
# expectation made this one.
_WINDOW = {
    "time": 86400,
    "day": 2 * 86400,
    "month": 31 * 86400,
    "year": 366 * 86400,
}


def promote(now: int | None = None) -> int:
    """Bind an expectation to a real guide airing, once one exists.

    From that moment the pass behaves like every other pass and books through
    the path that already exists. Nothing here books anything: it only stops
    the waiting.

    An expectation with no date is left alone. There is nothing to match it
    against, and binding on the title alone would catch a broadcast years off.
    """
    now = int(now if now is not None else time.time())
    matched = 0
    for item in waiting():
        if not item["expected_at"]:
            continue
        span = _WINDOW.get(item["precision"], _WINDOW["year"])
        row = db.one(
            """SELECT p.guid AS guid FROM airings a
                 JOIN programs p ON p.guid = a.program_guid
               WHERE ulower(COALESCE(NULLIF(p.grandparent_title, ''), p.title))
                     = ulower(?)
                 AND a.begins_at BETWEEN ? AND ?
               ORDER BY a.begins_at LIMIT 1""",
            (item["title"], item["expected_at"] - span, item["expected_at"] + span))
        if not row:
            continue
        with db.tx() as c:
            c.execute("UPDATE expectations SET matched_guid = ?, matched_at = ?, "
                      "missed_at = NULL WHERE id = ?",
                      (row["guid"], now, item["id"]))
        matched += 1
    return matched
