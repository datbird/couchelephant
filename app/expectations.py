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


# Naming every one of them turns a notice into a wall of text. Three plus a
# count says the same thing and can be read at a glance.
_NAMES_SHOWN = 3


def sweep_misses(guide_ends_at: int | None, now: int | None = None) -> list[dict]:
    """Report anything the guide has now reached past and never carried.

    Only judged once the guide actually extends beyond the expected date.
    Before that, silence is the guide being short rather than the show being
    missing, and warning then would cry wolf every day for months.

    A miss is a warning and never a deletion. A show can slip a week, and
    throwing the expectation away would be giving up on it quietly, which is
    the one thing this whole feature exists to prevent.
    """
    now = int(now if now is not None else time.time())
    if not guide_ends_at:
        return []
    late = [e for e in waiting()
            if e["expected_at"] and e["expected_at"] < guide_ends_at]
    if not late:
        return []
    with db.tx() as c:
        for item in late:
            c.execute("UPDATE expectations SET missed_at = ? WHERE id = ?",
                      (now, item["id"]))
    names = sorted({e["title"] for e in late})
    shown = ", ".join(names[:_NAMES_SHOWN])
    if len(names) > _NAMES_SHOWN:
        shown += " and others"
    return [{
        "code": "expectation_missed",
        "severity": "warn",
        "title": "Something you are waiting for did not reach the guide",
        "detail": (f"The guide now runs past the date announced for {shown}, "
                   f"and no airing matched. The date may have moved, the title "
                   f"may be spelled differently in the guide, or it may not be "
                   f"carried on a channel you receive."),
        "hint": ("CouchElephant keeps looking. Check the title against the "
                 "guide, or remove the pass if it is not coming."),
    }]


# A published season does not change hourly, and the free tier is rate limited.
# Asking once a day per pass is plenty.
_REFILL_AFTER = 86400


def fill_team_passes(now: int | None = None) -> int:
    """Give every enabled team pass the games its league has scheduled.

    This is the step that was missing. A team pass is made from the team
    picker, not from the announced search, so nothing ever reached the sports
    source. An existing pass had no expectations and nothing back-filled it.

    Doing it here rather than at pass creation covers both: a pass made today
    and one made months ago fill on the next sync, with no action from anyone.

    What arrives depends on the key. Without one TheSportsDB answers one
    upcoming game per team. A subscriber key is what gives the full season.
    Both are asked for and merged, so a key raises the answer without changing
    any of this.
    """
    from .sources import thesportsdb

    now = int(now if now is not None else time.time())
    key = db.get_setting("sportsdb_key") or ""
    filled = 0
    rows = db.query(
        """SELECT p.id, p.team_name, p.sportsdb_team_id, p.sportsdb_league_id,
                  (SELECT MAX(updated_at) FROM expectations e
                    WHERE e.pass_id = p.id AND e.source = 'thesportsdb') AS asked_at
           FROM passes p
           WHERE p.kind = 'team' AND p.enabled = 1
             AND COALESCE(p.team_name, '') <> ''""")
    for row in rows:
        if row["asked_at"] and now - row["asked_at"] < _REFILL_AFTER:
            continue
        team_id = row["sportsdb_team_id"]
        league_id = row["sportsdb_league_id"]
        if not team_id:
            try:
                found = thesportsdb.team(row["team_name"], key=key)
            except Exception:
                # A source being unreachable is not something the user can act
                # on. The pass is untouched and the next sync tries again.
                continue
            if not found:
                # An unknown name resolves to nothing rather than the closest
                # match, which would fill the pass with somebody else's games.
                continue
            team_id, league_id = found["team_id"], found["league_id"]
            with db.tx() as c:
                c.execute("UPDATE passes SET sportsdb_team_id = ?, "
                          "sportsdb_league_id = ? WHERE id = ?",
                          (team_id, league_id, row["id"]))
        # Both endpoints, because what each gives depends on the key. Written
        # out rather than looped over: a lambda closing over a loop variable
        # is a trap even when it is called at once.
        games = []
        try:
            games.extend(thesportsdb.upcoming(team_id, key=key))
        except Exception:
            pass
        try:
            games.extend(thesportsdb.season(row["team_name"], league_id, key=key))
        except Exception:
            pass
        # The two endpoints overlap. The pass keys on source_id anyway, but
        # deduping here keeps the count honest.
        seen, unique = set(), []
        for game in games:
            if game.source_id in seen:
                continue
            seen.add(game.source_id)
            unique.append(game)
        if unique:
            store(row["id"], unique, now=now)
            filled += 1
    return filled
