"""Is Plex doing its own job?

CouchElephant can only choose from the airings Plex offers it. When Plex stops
refreshing its guide, nothing here breaks and nothing here complains: passes
keep running, syncs keep succeeding, and the guide quietly gets shorter every
day until the game you wanted is past the end of it. That failure is silent,
and it stays silent right up to the evening the recording does not happen.

So these checks watch Plex rather than watching ourselves, and they ask Plex
what it is *supposed* to do rather than assuming. `/butler` gives the real
interval for the guide refresh and whether it is even switched on. Comparing
that against the DVR's own `refreshedAt` is a fact, not a heuristic.

A notice is raised by a check and cleared by the same check passing. There is
no dismiss. A health warning you can click away is one you will click away.
"""
import time

from . import db

# One code per condition. They are stable strings because they are the primary
# key of the notices table: the same problem recurring must land on the same
# row and keep its `first_seen`.
EPG_REFRESH_OFF = "epg_refresh_off"
EPG_STALE = "epg_stale"
GUIDE_SHORT = "guide_short"
PLEX_UNREACHABLE = "plex_unreachable"
TEAM_PASS_UNMATCHED = "team_pass_unmatched"
BOOKING_DRIFT = "booking_drift"
BOOKING_REPAIR_FAILED = "booking_repair_failed"
EXPECTATION_MISSED = "expectation_missed"

# A suggestion rather than a fault: something optional that would work better
# if it were set up. The ONLY severity that may ever be dismissed.
TIP = "tip"
KEYS_AVAILABLE = "keys_available"

# What each sweep is responsible for. A sweep clears the conditions it checked
# and nothing else: `record` resolving everything it was not handed would mean
# the Plex checks silently closing a finding they never looked at, once per
# sync, resetting its age every time.
PLEX_CODES = frozenset((EPG_REFRESH_OFF, EPG_STALE, GUIDE_SHORT, PLEX_UNREACHABLE))
REACH_CODES = frozenset((PLEX_UNREACHABLE,))
TEAM_CODES = frozenset((TEAM_PASS_UNMATCHED,))
BOOKING_CODES = frozenset((BOOKING_DRIFT, BOOKING_REPAIR_FAILED))
EXPECT_CODES = frozenset((EXPECTATION_MISSED,))
TIP_CODES = frozenset((KEYS_AVAILABLE,))

# Plex's guide refresh is a daily task. Complaining the first time it slips a
# day would cry wolf over one missed window, so a notice waits for twice the
# interval, and never less than two days.
STALE_GRACE = 2

# Below this the guide is running out, whatever Plex says about refreshing. It
# is the consequence rather than the cause, and it is the one that costs you a
# recording, so it is worth its own notice.
SHORT_GUIDE_DAYS = 3

DAY = 86400


def _days(seconds: float) -> str:
    """A duration a person can read, rounded the way they would say it."""
    d = seconds / DAY
    if d < 1:
        return f"{round(seconds / 3600)} hours"
    return "1 day" if round(d) == 1 else f"{round(d)} days"


def epg_task(tasks: list[dict]) -> dict | None:
    """Plex's own scheduled guide refresh, if it has one."""
    for t in tasks or []:
        if t.get("name") == "RefreshEpgGuides":
            return t
    return None


def check(*, tasks: list[dict], refreshed_at: int | None,
          guide_ends_at: int | None, now: int) -> list[dict]:
    """Every check, against one snapshot of Plex. Returns the notices raised.

    Taking the snapshot as arguments rather than a Plex client keeps this
    honest: the rules are testable without a server, and the only thing the
    caller has to get right is the reading.
    """
    out = []
    task = epg_task(tasks)

    if task is not None and not task.get("enabled"):
        out.append({
            "code": EPG_REFRESH_OFF,
            "severity": "bad",
            "title": "Plex is not refreshing its TV guide",
            "detail": "Plex's own 'Refresh EPG Guides' maintenance task is "
                      "switched off, so the guide will never grow past what it "
                      "already holds.",
            "hint": "Turn it back on in Plex under Settings, Scheduled Tasks.",
        })

    # An interval of 0 or a missing task means Plex did not say, so there is no
    # schedule to hold it to and no honest way to call it late.
    interval = int(task.get("interval") or 0) if task else 0
    if refreshed_at and interval > 0:
        age = now - refreshed_at
        allowed = max(interval * STALE_GRACE, 2) * DAY
        if age > allowed:
            out.append({
                "code": EPG_STALE,
                "severity": "bad",
                "title": "Plex's TV guide has not refreshed",
                "detail": f"Plex refreshes its guide every {_days(interval * DAY)}. "
                          f"It last refreshed {_days(age)} ago. Nothing new is "
                          f"reaching the guide, so it gets shorter every day.",
                "hint": "Refresh the guide from Plex, under Live TV & DVR. If it "
                        "goes stale again, the problem is Plex's scheduler "
                        "rather than the guide.",
            })

    if guide_ends_at:
        left = guide_ends_at - now
        if left < SHORT_GUIDE_DAYS * DAY:
            out.append({
                "code": GUIDE_SHORT,
                "severity": "bad" if left < DAY else "warn",
                "title": "The guide is running out",
                "detail": f"Plex's guide reaches only {_days(max(left, 0))} ahead. "
                          f"Anything further out cannot be found, so it cannot "
                          f"be recorded.",
                "hint": "A healthy guide reaches about two weeks. Refresh it in "
                        "Plex, under Live TV & DVR.",
            })
    return out


def unreachable(detail: str) -> list[dict]:
    """The notice for a sync that could not talk to Plex at all.

    Its own entry point because there is no snapshot to check in that case:
    the failure to read *is* the finding.
    """
    return [{
        "code": PLEX_UNREACHABLE,
        "severity": "bad",
        "title": "Plex could not be reached",
        "detail": detail,
        "hint": "Check that Plex is running and that the address and token in "
                "Settings still work.",
    }]


def record(raised: list[dict], now: int, owns=None) -> None:
    """Persist one sweep's findings: raise what is failing, clear what is not.

    `owns` names the conditions this sweep actually checked. Anything in it
    that was not raised is resolved; anything outside it is left alone. Without
    that, the Plex sweep would close the team-pass finding on every sync and
    the team sweep would reopen it, and the age of the problem would reset each
    hour. Passing None means this sweep speaks for every condition.

    A notice already open keeps its `first_seen`, so its age is the age of the
    problem rather than the age of the last sync.
    """
    codes = {n["code"] for n in raised}
    with db.tx() as c:
        for n in raised:
            c.execute(
                """INSERT INTO notices (code, severity, title, detail, hint,
                                        first_seen, last_seen, resolved_at)
                   VALUES (?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(code) DO UPDATE SET
                     severity=excluded.severity, title=excluded.title,
                     detail=excluded.detail, hint=excluded.hint,
                     last_seen=excluded.last_seen,
                     -- A problem that comes back after being fixed is a new
                     -- problem, so its age starts again. One that never went
                     -- away keeps the date it started.
                     first_seen=CASE WHEN notices.resolved_at IS NULL
                                     THEN notices.first_seen
                                     ELSE excluded.first_seen END,
                     resolved_at=NULL""",
                (n["code"], n["severity"], n["title"], n["detail"], n.get("hint"),
                 now, now))
        stale = (set(owns) - codes) if owns is not None else None
        if stale is not None:
            if not stale:
                return
            marks = ",".join("?" * len(stale))
            c.execute(f"UPDATE notices SET resolved_at = ? WHERE resolved_at IS NULL "
                      f"AND code IN ({marks})", (now, *sorted(stale)))
        elif codes:
            marks = ",".join("?" * len(codes))
            c.execute(f"UPDATE notices SET resolved_at = ? WHERE resolved_at IS NULL "
                      f"AND code NOT IN ({marks})", (now, *codes))
        else:
            c.execute("UPDATE notices SET resolved_at = ? WHERE resolved_at IS NULL",
                      (now,))


def keys_tip(has_tmdb: bool, has_sportsdb: bool,
             film_passes: int, team_passes: int) -> list[dict]:
    """Offer a key only where one would actually add something.

    TVmaze needs no key at all, so following a series already works for
    everyone. This is about the two optional ones, and only for somebody
    already following the kind of thing they help with. Anybody else is never
    told they exist, because a suggestion you cannot use is just noise.
    """
    wants = []
    if team_passes and not has_sportsdb:
        wants.append("TheSportsDB, for a team's published season. This works "
                     "without a key; a subscriber key at thesportsdb.com "
                     "raises the rate limit and the depth of the answer")
    if film_passes and not has_tmdb:
        wants.append("TMDB, for films and their release dates, free at "
                     "themoviedb.org/settings/api")
    if not wants:
        return []
    return [{
        "code": KEYS_AVAILABLE,
        "severity": TIP,
        "title": "Two optional keys would fill in more of what you follow",
        "detail": ("CouchElephant already looks past the end of the guide using "
                   "TVmaze, which needs no key and is always on. Also useful "
                   "here: " + "; ".join(wants) + "."),
        "hint": "Settings, then Sources. Both are free, and neither is required.",
    }]


def dismiss(code: str) -> bool:
    """Wave off a suggestion. Refuses anything that is not a suggestion.

    The notices exist because a problem you can click away is a problem you
    forget about. A tip is not a problem, so it may go. Everything else stays,
    and this guard is the whole reason the two can live in one panel.
    """
    row = db.one("SELECT severity FROM notices WHERE code = ?", (code,))
    if not row or row["severity"] != TIP:
        return False
    with db.tx() as c:
        c.execute("UPDATE notices SET dismissed_at = ? WHERE code = ?",
                  (int(time.time()), code))
    return True


def open_notices() -> list[dict]:
    """What is wrong right now, worst first. A dismissed tip is not shown.

    The badge takes its colour from the first row, so a suggestion must never
    sort above a fault and hide it.
    """
    rows = db.query(
        "SELECT * FROM notices WHERE resolved_at IS NULL AND dismissed_at IS NULL "
        "ORDER BY CASE severity WHEN 'bad' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
        "first_seen")
    return [dict(r) for r in rows]
