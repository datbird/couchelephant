"""What a pass is still waiting for.

An expectation is an intention, never a booking. It says "you asked to follow
this, and a source outside Plex thinks it happens then". Only a guide airing
carries a channel, so only a guide airing can be recorded. Nothing in here
schedules anything.
"""
import dataclasses
import datetime
import time
import zoneinfo

from . import db, teamcat

WHEN_UNKNOWN = "date not announced"

# What each precision is allowed to show. Anything more is invented. A source
# that said "2027-03" did not say the first of March at midnight, and a reader
# takes an invented date for a real one.
# Written the way `routes/_shared.fmt` writes every other time in the product:
# 24 hour, day before month. CouchElephant is run worldwide, and a plan showing
# "7:15 PM" on Sep 14 would be the only US formatted date anywhere in it.
#
# The month and day NAMES are still English, because Python formats them in the
# C locale. That is true of every date in the app, not just these, so it is a
# product-wide job rather than something to solve here.
_FORMATS = {
    "time": "%a %d %b %Y, %H:%M",
    "day": "%a %d %b %Y",
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

    Only for a pass that still exists and is still enabled. A row with no date
    sorts last rather than first, which is where a `NULL` would land it.
    """
    # Joined to the pass, not just filtered by id. A pass that was deleted
    # would otherwise leave its games waiting on the screen for ever, and
    # `sweep_misses` would go on reporting them missing for something nobody
    # follows any more. A pass you turned off should stop showing you what it
    # was going to do, for the same reason.
    sql = ("SELECT e.* FROM expectations e "
           "  JOIN passes p ON p.id = e.pass_id AND p.enabled = 1 "
           " WHERE e.matched_guid IS NULL{extra} "
           " ORDER BY COALESCE(e.expected_at, 1 << 40), e.title")
    if pass_id is None:
        return [dict(r) for r in db.query(sql.format(extra=""))]
    return [dict(r) for r in db.query(sql.format(extra=" AND e.pass_id = ?"),
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
    return datetime.datetime.fromtimestamp(expected_at, zone).strftime(fmt)


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


def _match_in_guide(item: dict, span: int):
    """Find the guide airing this expectation was waiting for, or nothing.

    Sport and everything else are matched differently, because the guide
    describes them differently.

    A game is titled by its matchup, "Kansas City Chiefs at Denver Broncos",
    with the league in `grandparent_title`. A team expectation carries the team
    name, so comparing it against either of those never matches, and a whole
    season would sit as a plan and then be reported missing. The teams are in a
    JSON array on the programme, and that is what to match on.

    The fold is `tident`, the same one `passes.candidate_airings` uses to
    decide what to record. Deliberately not `teamcat.norm`: norm also drops
    club words, which folds Real Madrid and Atletico Madrid both to "madrid".
    Right for finding a team in a catalogue, wrong for picking a broadcast.
    """
    lo, hi = item["expected_at"] - span, item["expected_at"] + span
    if item["source"] == "thesportsdb":
        key = teamcat.ident(item["title"] or "")
        if not key:
            return None
        return db.one(
            """SELECT p.guid AS guid FROM airings a
                 JOIN programs p ON p.guid = a.program_guid
               WHERE p.teams IS NOT NULL AND p.teams != '[]'
                 AND EXISTS (SELECT 1 FROM json_each(p.teams) t
                             WHERE tident(json_extract(t.value, '$.name')) = ?)
                 AND a.begins_at BETWEEN ? AND ?
               ORDER BY a.begins_at LIMIT 1""", (key, lo, hi))
    return db.one(
        """SELECT p.guid AS guid FROM airings a
             JOIN programs p ON p.guid = a.program_guid
           WHERE ulower(COALESCE(NULLIF(p.grandparent_title, ''), p.title))
                 = ulower(?)
             AND a.begins_at BETWEEN ? AND ?
           ORDER BY a.begins_at LIMIT 1""", (item["title"], lo, hi))


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
        row = _match_in_guide(item, span)
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


def fill_series_passes(now: int | None = None) -> int:
    """Give every enabled series pass the episodes TVmaze has dated for it.

    THE GAP THIS CLOSES. A series pass got its expectations exactly once, from
    the announced search, at the moment it was made. `search` answers one row
    per SHOW carrying its premiere date, and nothing ever asked again. So a
    followed show held one row for ever: it never learned about next season,
    and for a show that had already premiered that one row was a date in the
    past that could only ever be swept as missed.

    Nothing had to be bought to fix it. TVmaze's episode list is free and
    unkeyed, exactly like the search that was already being used. This is the
    series half of `fill_team_passes`, and it follows the same discipline.

    **Knowing when NOT to ask is most of this function**, same as its sports
    twin: it asks when a pass has never been asked, when a day has gone by, or
    when the title changed under us so the stored show id belongs to another
    programme. TVmaze allows roughly 20 calls per 10 seconds per address, so a
    library of followed shows must not re-ask on every sync.

    The attempt is dated BEFORE the calls. A show with no dated episodes
    produces no rows, and inferring the attempt from the rows would re-ask on
    every sync for ever.

    ONLY FUTURE EPISODES ARE KEPT. The source hands back the whole run,
    hundreds of rows for an old show, and an episode that already aired is not
    something anyone is waiting for. `promote()` binds the rest as the guide
    reaches them.
    """
    from .sources import tvmaze

    now = int(now if now is not None else time.time())
    filled = 0
    rows = db.query(
        """SELECT id, series_title, tvmaze_show_id, tvmaze_asked_at,
                  tvmaze_asked_for
           FROM passes
           WHERE kind = 'series' AND enabled = 1
             AND COALESCE(series_title, '') <> ''""")
    for row in rows:
        renamed = (row["tvmaze_asked_for"] or "") != row["series_title"]
        fresh = bool(row["tvmaze_asked_at"]
                     and now - row["tvmaze_asked_at"] < _REFILL_AFTER)
        if fresh and not renamed:
            continue

        # A rename invalidates the id the same way it does for a team.
        show_id = None if renamed else row["tvmaze_show_id"]
        if not show_id:
            # The pass may already carry the answer. `_follow` stored the show's
            # own announcement against this pass, and its source_id IS the
            # TVmaze show id, so a pass made through the announced search needs
            # no lookup at all. Episode rows are prefixed, so they cannot be
            # mistaken for the show.
            found = db.one(
                "SELECT source_id FROM expectations WHERE pass_id = ? "
                "AND source = 'tvmaze' AND source_id NOT LIKE 'ep-%' LIMIT 1",
                (row["id"],))
            show_id = found["source_id"] if found else None

        with db.tx() as c:
            c.execute("""UPDATE passes SET tvmaze_asked_at = ?,
                             tvmaze_asked_for = ?, tvmaze_show_id = ?
                         WHERE id = ?""",
                      (now, row["series_title"], show_id, row["id"]))

        if not show_id:
            # A pass made from the guide rather than the announced search has
            # no id yet. Resolved by EXACT title only: a near match would fill
            # the pass with another programme's episodes, which is the same
            # refusal `thesportsdb.team` makes for an unknown team name.
            try:
                hits = tvmaze.search(row["series_title"])
            except Exception:                       # noqa: BLE001 — retried tomorrow
                continue
            wanted = (row["series_title"] or "").strip().casefold()
            exact = [h for h in hits if (h.title or "").strip().casefold() == wanted]
            if len(exact) != 1:
                continue                            # nothing, or nothing decidable
            show_id = exact[0].source_id
            with db.tx() as c:
                c.execute("UPDATE passes SET tvmaze_show_id = ? WHERE id = ?",
                          (show_id, row["id"]))

        try:
            found = tvmaze.episodes(show_id)
        except Exception:                           # noqa: BLE001 — retried tomorrow
            continue
        # The source leaves the title empty because it only knows the episode.
        # The pass knows the programme, so it is named here.
        ahead = [
            dataclasses.replace(e, title=row["series_title"])
            for e in found if e.expected_at and e.expected_at > now
        ]
        if ahead:
            store(row["id"], ahead, now=now)
            # THE SHOW ROW IS SUPERSEDED, so drop it. `_follow` stored one row
            # for the programme itself, dated at its PREMIERE. For a show that
            # already premiered that date is in the past, and its only future
            # was to be reported missing for ever by `sweep_misses`. Real dated
            # episodes from the same source say strictly more.
            #
            # Only when episodes actually arrived, and never one already bound
            # to an airing: an unannounced show has no episodes yet, and there
            # its premiere row is the whole signal.
            with db.tx() as c:
                c.execute("DELETE FROM expectations WHERE pass_id = ? "
                          "AND source = 'tvmaze' AND source_id NOT LIKE 'ep-%' "
                          "AND matched_guid IS NULL", (row["id"],))
            filled += 1
    return filled


# What the sports fetch asks for. Part of the refill fingerprint, so changing
# the calls forces one refill per pass rather than leaving a stale answer on
# screen until tomorrow.
_FETCH_SHAPE = "rounds"


def fill_team_passes(now: int | None = None) -> int:
    """Give every enabled team pass the games its league has scheduled.

    A team pass is made from the team picker, never from the announced search,
    so nothing else ever reaches the sports source. Doing it here covers a pass
    made today and one made months ago alike, with no action from anyone.

    What arrives depends on the key. Without one TheSportsDB answers a single
    upcoming game per team. A subscriber key is what gives the full season.
    Both endpoints are asked and merged, so a key raises the answer without
    changing any of this.

    **Knowing when NOT to ask is most of this function.** It asks when the pass
    has never been asked about, when a day has gone by, when the team was
    renamed under us so the stored ids belong to somebody else, or when a key
    was added or removed, which changes what the answer would be. Otherwise it
    asks nobody anything.

    The attempt is dated on the pass rather than worked out from the rows it
    produced. An attempt that finds nothing produces no rows, so an unknown
    team, or a real team out of season, would otherwise be looked up on every
    single sync against a rate limited free tier.
    """
    from .sources import thesportsdb

    now = int(now if now is not None else time.time())
    key = db.get_setting("sportsdb_key") or ""
    # Not the key itself. Whether there is one is all that changes the answer,
    # and a key does not belong in a column that ends up in logs and exports.
    #
    # AND WHICH CALLS WE MAKE, because that changes the answer just as much as a
    # key does. 1.0.7 moved the season from `eventsseason.php` to a walk over
    # `eventsround.php`, which is the difference between one fixture and a whole
    # season. Without the shape in here, every existing install would have gone
    # on showing the old thin answer for up to a day after upgrading, and the
    # release would have looked like it had not worked. Bump `_FETCH_SHAPE`
    # whenever a change alters what comes back.
    fingerprint = ("key" if key.strip() else "free") + "/" + _FETCH_SHAPE
    filled = 0
    rows = db.query(
        """SELECT id, team_name, sportsdb_team_id, sportsdb_league_id,
                  sportsdb_asked_at, sportsdb_asked_for, sportsdb_asked_with
           FROM passes
           WHERE kind = 'team' AND enabled = 1
             AND COALESCE(team_name, '') <> ''""")
    for row in rows:
        renamed = (row["sportsdb_asked_for"] or "") != row["team_name"]
        rekeyed = (row["sportsdb_asked_with"] or "") != fingerprint
        fresh = bool(row["sportsdb_asked_at"]
                     and now - row["sportsdb_asked_at"] < _REFILL_AFTER)
        if fresh and not renamed and not rekeyed:
            continue

        # A rename invalidates the ids: they point at whoever the old name
        # resolved to.
        team_id = None if renamed else row["sportsdb_team_id"]
        league_id = None if renamed else row["sportsdb_league_id"]

        # Dated BEFORE the calls, not after. A source that raises still counts
        # as an attempt, or a provider having a bad day is retried hourly.
        with db.tx() as c:
            c.execute("""UPDATE passes SET sportsdb_asked_at = ?,
                             sportsdb_asked_for = ?, sportsdb_asked_with = ?,
                             sportsdb_team_id = ?, sportsdb_league_id = ?
                         WHERE id = ?""",
                      (now, row["team_name"], fingerprint, team_id, league_id,
                       row["id"]))

        if not team_id:
            try:
                found = thesportsdb.team(row["team_name"], key=key)
            except Exception:
                continue
            if not found:
                # An unknown name resolves to nothing rather than to the
                # closest match, which would fill the pass with another team.
                continue
            team_id, league_id = found["team_id"], found["league_id"]
            with db.tx() as c:
                c.execute("UPDATE passes SET sportsdb_team_id = ?, "
                          "sportsdb_league_id = ? WHERE id = ?",
                          (team_id, league_id, row["id"]))

        # Both endpoints, because what each gives depends on the key. Written
        # out rather than looped over: a lambda closing over a loop variable is
        # a trap even when it is called at once.
        games = []
        try:
            games.extend(thesportsdb.upcoming(team_id, key=key))
        except Exception:
            pass
        try:
            games.extend(thesportsdb.season(row["team_name"], league_id, key=key))
        except Exception:
            pass
        # The two endpoints overlap. The table keys on source_id anyway, but
        # deduping here keeps the returned count honest.
        seen, unique = set(), []
        for game in games:
            if game.source_id in seen:
                continue
            seen.add(game.source_id)
            unique.append(game)
        # ONLY WHAT IS STILL AHEAD. A published season is mostly behind you by
        # December, and a game that already kicked off is not something anyone
        # is waiting for. Same rule the series fill follows.
        unique = [g for g in unique if g.expected_at and g.expected_at > now]
        if unique:
            store(row["id"], unique, now=now)
            # Drop what the source we did NOT use left here on an earlier run.
            # Switching from TheSportsDB's single game to ESPN's whole season
            # would otherwise leave that one game beside its own duplicate,
            # dated the same day, for ever. Never one already bound to an
            # airing: that is a real booking's link to its plan.
            kept = sorted({g.source for g in unique})
            holes = ",".join("?" * len(kept))
            with db.tx() as c:
                c.execute(
                    f"DELETE FROM expectations WHERE pass_id = ? "
                    f"AND source NOT IN ({holes}) AND matched_guid IS NULL",
                    (row["id"], *kept))
            filled += 1
    return filled
