"""Watching Plex do its own job.

The failure this exists for is silent. Plex stops refreshing its guide, every
sync here still succeeds, and the guide gets a day shorter every day until the
game you wanted is past the end of it. Nothing errors. You find out the evening
the recording does not happen.
"""
import time

from app import db, health, sync
from tests import fake_plex

DAY = 86400


def _tasks(enabled=True, interval=1):
    return [{"name": "BackupDatabase", "interval": 3, "enabled": True},
            {"name": "RefreshEpgGuides", "interval": interval, "enabled": enabled}]


# ---- the rules, without a server ----

def test_a_guide_refreshed_on_schedule_raises_nothing():
    now = int(time.time())
    out = health.check(tasks=_tasks(), refreshed_at=now - 3600,
                       guide_ends_at=now + 12 * DAY, now=now)
    assert out == []


def test_a_guide_that_stopped_refreshing_is_a_notice():
    """The real case: Plex refreshes daily and last did so five days ago."""
    now = int(time.time())
    out = health.check(tasks=_tasks(interval=1), refreshed_at=now - 5 * DAY,
                       guide_ends_at=now + 7 * DAY, now=now)
    codes = {n["code"] for n in out}
    assert health.EPG_STALE in codes
    stale = next(n for n in out if n["code"] == health.EPG_STALE)
    assert "5 days ago" in stale["detail"], stale["detail"]
    assert stale["severity"] == "bad"


def test_one_missed_window_is_not_a_notice():
    """A daily task that slipped a day has not failed, it is late. Crying wolf
    on the first slip trains you to ignore the badge."""
    now = int(time.time())
    out = health.check(tasks=_tasks(interval=1), refreshed_at=now - int(1.5 * DAY),
                       guide_ends_at=now + 12 * DAY, now=now)
    assert [n["code"] for n in out] == []


def test_the_interval_comes_from_plex_not_from_us():
    """Plex says how often it refreshes. A weekly task five days late is fine;
    a daily one five days late is broken. Same age, different answer."""
    now = int(time.time())
    weekly = health.check(tasks=_tasks(interval=7), refreshed_at=now - 5 * DAY,
                          guide_ends_at=now + 12 * DAY, now=now)
    daily = health.check(tasks=_tasks(interval=1), refreshed_at=now - 5 * DAY,
                         guide_ends_at=now + 12 * DAY, now=now)
    assert [n["code"] for n in weekly] == []
    assert health.EPG_STALE in {n["code"] for n in daily}


def test_a_switched_off_refresh_task_is_its_own_notice():
    now = int(time.time())
    out = health.check(tasks=_tasks(enabled=False), refreshed_at=now - 3600,
                       guide_ends_at=now + 12 * DAY, now=now)
    assert health.EPG_REFRESH_OFF in {n["code"] for n in out}


def test_a_server_that_does_not_answer_butler_is_not_accused():
    """No task list means Plex did not say what it intends. That is not the
    same as Plex intending nothing, and a notice would be a guess."""
    now = int(time.time())
    out = health.check(tasks=[], refreshed_at=now - 5 * DAY,
                       guide_ends_at=now + 12 * DAY, now=now)
    assert [n["code"] for n in out] == []


def test_a_guide_running_out_is_a_notice_whatever_plex_says():
    """The consequence, not the cause. This is the one that costs a recording."""
    now = int(time.time())
    out = health.check(tasks=_tasks(), refreshed_at=now - 3600,
                       guide_ends_at=now + int(1.5 * DAY), now=now)
    assert health.GUIDE_SHORT in {n["code"] for n in out}


def test_an_empty_guide_says_nothing_rather_than_guessing():
    """A first run has no airings yet. That is not a health problem."""
    now = int(time.time())
    out = health.check(tasks=_tasks(), refreshed_at=now - 3600,
                       guide_ends_at=None, now=now)
    assert [n["code"] for n in out] == []


# ---- raising, keeping and clearing ----

def test_a_notice_keeps_the_date_the_problem_started(clean_db):
    """The question you ask on finding a stale guide is "since when". Restamping
    it every sync would answer "an hour ago", every time, for ever."""
    first = int(time.time()) - 4 * DAY
    raised = [{"code": health.EPG_STALE, "severity": "bad", "title": "t",
               "detail": "d", "hint": "h"}]
    health.record(raised, first)
    health.record(raised, first + 4 * DAY)

    rows = health.open_notices()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == first
    assert rows[0]["last_seen"] == first + 4 * DAY


def test_a_notice_clears_itself_when_the_check_passes(clean_db):
    now = int(time.time())
    health.record([{"code": health.EPG_STALE, "severity": "bad", "title": "t",
                    "detail": "d", "hint": "h"}], now)
    assert len(health.open_notices()) == 1
    health.record([], now + 60)
    assert health.open_notices() == []


def test_a_problem_that_comes_back_starts_its_age_again(clean_db):
    now = int(time.time())
    n = [{"code": health.EPG_STALE, "severity": "bad", "title": "t",
          "detail": "d", "hint": "h"}]
    health.record(n, now - 10 * DAY)
    health.record([], now - 9 * DAY)          # fixed
    health.record(n, now)                     # and broken again
    assert health.open_notices()[0]["first_seen"] == now


def test_the_worst_notice_sorts_first(clean_db):
    now = int(time.time())
    health.record([
        {"code": "a", "severity": "warn", "title": "t", "detail": "d", "hint": None},
        {"code": "b", "severity": "bad", "title": "t", "detail": "d", "hint": None},
    ], now)
    assert [n["code"] for n in health.open_notices()] == ["b", "a"]


# ---- against the fake server ----

# The fake guide is deliberately two days long, because a test wants the
# smallest guide that proves the behaviour. That is genuinely a short guide, so
# GUIDE_SHORT is raised against it and raised correctly. These tests therefore
# ask about the refresh notices rather than about an empty list.
EPG_CODES = {health.EPG_STALE, health.EPG_REFRESH_OFF}


def test_a_healthy_sync_raises_nothing_about_the_refresh(plex, synced):
    assert EPG_CODES & {n["code"] for n in health.open_notices()} == set()


def test_a_short_fake_guide_is_correctly_called_short(plex, synced):
    """Not incidental. The fake guide reaches two days, and two days is the
    condition this notice exists to report."""
    assert health.GUIDE_SHORT in {n["code"] for n in health.open_notices()}


def test_a_stale_plex_is_noticed_by_an_ordinary_sync(plex, synced):
    """End to end: nothing about the sync fails, and the problem still surfaces."""
    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    ok, detail = sync.full_sync()
    assert ok, detail
    assert health.EPG_STALE in {n["code"] for n in health.open_notices()}


def test_the_notice_goes_away_once_plex_catches_up(plex, synced):
    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    sync.full_sync()
    assert health.open_notices()
    fake_plex.STATE.refreshed_at = None       # refreshed just now
    sync.full_sync()
    assert EPG_CODES & {n["code"] for n in health.open_notices()} == set()


def test_butler_is_read_from_its_own_root_not_a_media_container(plex):
    """`/butler` is the one response Plex does not wrap. Unwrapping it anyway
    returns {} and reads as "no scheduled tasks", which is a lie a health check
    would act on."""
    tasks = plex.butler_tasks()
    assert any(t["name"] == "RefreshEpgGuides" for t in tasks)


def test_each_sync_writes_down_what_the_guide_looked_like(plex, synced):
    """Two numbers per sync are what make a frozen guide visible at all."""
    row = db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
    assert row["epg_refreshed_at"], "when Plex last refreshed"
    assert row["guide_ends_at"], "how far the guide reached"


def test_a_plex_that_cannot_be_reached_is_a_notice(clean_db):
    db.set_setting("plex_url", "http://127.0.0.1:1")
    db.set_setting("plex_token", "x")
    ok, detail = sync.full_sync()
    assert not ok
    assert health.PLEX_UNREACHABLE in {n["code"] for n in health.open_notices()}


def test_reaching_plex_again_clears_the_unreachable_notice(plex, synced):
    """A sync that failed once must not leave a permanent scar."""
    db.set_setting("plex_url", "http://127.0.0.1:1")
    assert not sync.full_sync()[0]
    assert health.PLEX_UNREACHABLE in {n["code"] for n in health.open_notices()}
    db.set_setting("plex_url", str(plex.base))
    assert sync.full_sync()[0]
    assert health.PLEX_UNREACHABLE not in {n["code"] for n in health.open_notices()}


# ---- what the person actually sees ----

def test_the_header_carries_no_badge_when_plex_is_well(client, plex, synced):
    from app import db as _db
    with _db.tx() as c:
        c.execute("DELETE FROM notices")
    body = client.get("/").text
    assert "sync-badge" not in body
    assert 'class="sync-wrap ' in body or 'class="sync-wrap"' in body


def test_the_badge_and_the_reason_appear_together(client, plex, synced):
    """A red dot with no explanation is an anxiety generator. The panel behind
    it has to say what is wrong and what to do."""
    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    sync.full_sync()
    body = client.get("/").text
    assert "sync-badge" in body
    assert "has-notices" in body
    assert "Plex&#39;s TV guide has not refreshed" in body or \
           "Plex's TV guide has not refreshed" in body
    assert "Live TV &amp; DVR" in body or "Live TV & DVR" in body


def test_the_api_reports_how_long_it_has_been_wrong(client, plex, synced):
    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    sync.full_sync()
    d = client.get("/api/notices").json()
    stale = next(n for n in d["notices"] if n["code"] == health.EPG_STALE)
    assert stale["since"]
    assert stale["age_seconds"] >= 0
    assert d["bad"] >= 1


# ---- the thresholds, at the exact point they turn ----

def test_the_grace_is_twice_the_interval_and_not_a_day_either_side():
    now = int(time.time())

    def stale(age_days, interval=1):
        out = health.check(tasks=_tasks(interval=interval),
                           refreshed_at=now - int(age_days * DAY),
                           guide_ends_at=now + 12 * DAY, now=now)
        return health.EPG_STALE in {n["code"] for n in out}

    assert not stale(1.9), "a daily task 1.9 days late is late, not broken"
    assert stale(2.1), "past twice the interval it is broken"
    assert not stale(13.9, interval=7), "a weekly task gets a week of grace"
    assert stale(14.1, interval=7)


def test_an_interval_of_zero_means_plex_did_not_say():
    """No schedule is not the same as a schedule of never. There is nothing to
    hold Plex to, so calling it late would be inventing the deadline."""
    now = int(time.time())
    out = health.check(tasks=_tasks(interval=0), refreshed_at=now - 5 * DAY,
                       guide_ends_at=now + 12 * DAY, now=now)
    assert health.EPG_STALE not in {n["code"] for n in out}


def test_the_short_guide_notice_gets_worse_as_it_runs_out():
    now = int(time.time())

    def sev(days):
        out = health.check(tasks=_tasks(), refreshed_at=now - 3600,
                           guide_ends_at=now + int(days * DAY), now=now)
        found = [n for n in out if n["code"] == health.GUIDE_SHORT]
        return found[0]["severity"] if found else None

    assert sev(5) is None
    assert sev(2) == "warn"
    assert sev(0.5) == "bad"


def test_a_guide_that_ended_in_the_past_does_not_read_as_negative_time():
    now = int(time.time())
    out = health.check(tasks=_tasks(), refreshed_at=now - 3600,
                       guide_ends_at=now - 3 * DAY, now=now)
    short = next(n for n in out if n["code"] == health.GUIDE_SHORT)
    assert "-" not in short["detail"], short["detail"]


# ---- how long ago, in words ----

def test_ago_reads_the_way_a_person_would_say_it():
    from app.routes._shared import ago
    now = int(time.time())
    assert ago(now) == "just now"
    assert ago(now - 30) == "just now"
    assert ago(now - 600) == "10 minutes ago"
    assert ago(now - 3600) == "an hour ago"
    assert ago(now - 5 * 3600) == "5 hours ago"
    assert ago(now - 2 * DAY) == "2 days ago"
    assert ago(now - 5 * DAY) == "5 days ago"


def test_ago_says_something_rather_than_nothing_when_it_has_no_date():
    from app.routes._shared import ago
    assert ago(None) == "just now"
    assert ago(0) == "just now"


def test_ago_never_counts_forwards():
    """A clock that has moved backwards must not produce "-3 days ago"."""
    from app.routes._shared import ago
    assert ago(int(time.time()) + 600) == "just now"


# ---- a health notice is not the user's data ----

def test_notices_do_not_travel_in_an_export(client, plex, synced):
    """A notice describes somebody else's Plex at a moment in time. Carrying it
    to another machine would report a problem that machine does not have."""
    import io
    import json
    import zipfile

    from app import portable

    fake_plex.STATE.refreshed_at = int(time.time()) - 5 * DAY
    sync.full_sync()
    assert health.open_notices(), "there is a notice to leak"

    blob = portable.export_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        manifest = json.loads(z.read(portable.MANIFEST))
        body = b"".join(z.read(n) for n in z.namelist())
    assert "notices" not in manifest["stores"]
    assert b"epg_stale" not in body, "the notice itself rode along in some store"


def test_the_guide_reading_is_not_carried_either(client, plex, synced):
    """`epg_refreshed_at` and `guide_ends_at` describe the Plex this install
    talks to. They are a reading, not a decision of the user's."""
    import io
    import json
    import zipfile

    from app import portable

    sync.full_sync()
    blob = portable.export_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for name in z.namelist():
            if name.endswith(".json"):
                text = z.read(name).decode()
                assert "epg_refreshed_at" not in text, name
                assert "guide_ends_at" not in text, name
        json.loads(z.read(portable.MANIFEST))


def _raise(code, severity):
    health.record([{"code": code, "severity": severity, "title": "T",
                    "detail": "D", "hint": None}], 100, owns=frozenset({code}))


def _open_codes():
    return {n["code"] for n in health.open_notices()}


def test_a_tip_can_be_waved_off():
    _raise("keys_available", health.TIP)
    assert health.dismiss("keys_available") is True
    assert "keys_available" not in _open_codes()


def test_a_health_problem_cannot_be_waved_off():
    """The rule the notices were built on: a health problem you can click away
    is a health problem you forget about. A dismissible tip must not become a
    way around that.

    Mutation-check this one. Take the severity guard out of `health.dismiss`
    and this test has to fail.
    """
    for severity in ("bad", "warn"):
        _raise("epg_stale", severity)
        assert health.dismiss("epg_stale") is False
        assert "epg_stale" in _open_codes()


def test_a_notice_that_does_not_exist_cannot_be_waved_off():
    assert health.dismiss("no_such_code") is False


def test_a_waved_off_tip_stays_gone_when_it_is_raised_again():
    """It is a suggestion. Asking a second time is nagging."""
    _raise("keys_available", health.TIP)
    health.dismiss("keys_available")
    _raise("keys_available", health.TIP)
    assert "keys_available" not in _open_codes()


def test_a_real_fault_sorts_above_a_suggestion():
    """The badge takes its colour from the first notice, so a tip must never
    push a fault down the list and hide it."""
    health.record([
        {"code": "keys_available", "severity": health.TIP, "title": "T",
         "detail": "D", "hint": None},
        {"code": "guide_short", "severity": "warn", "title": "T",
         "detail": "D", "hint": None},
        {"code": "epg_stale", "severity": "bad", "title": "T",
         "detail": "D", "hint": None},
    ], 100, owns=frozenset({"keys_available", "guide_short", "epg_stale"}))
    assert [n["severity"] for n in health.open_notices()] == ["bad", "warn", "tip"]


def test_no_key_is_offered_when_neither_would_help():
    """An install that only follows broadcast series gains nothing from either
    key, so it must never be nagged about them."""
    assert health.keys_tip(has_tmdb=False, has_sportsdb=False,
                           film_passes=0, team_passes=0) == []


def test_the_sports_key_is_offered_only_to_someone_following_a_team():
    raised = health.keys_tip(has_tmdb=False, has_sportsdb=False,
                             film_passes=0, team_passes=2)
    assert len(raised) == 1
    assert raised[0]["severity"] == health.TIP
    text = raised[0]["detail"] + raised[0]["hint"]
    assert "TheSportsDB" in text
    assert "thesportsdb.com" in text
    assert "TMDB" not in text


def test_the_film_key_is_offered_only_to_someone_following_a_film():
    raised = health.keys_tip(has_tmdb=False, has_sportsdb=False,
                             film_passes=1, team_passes=0)
    text = raised[0]["detail"] + raised[0]["hint"]
    assert "TMDB" in text
    assert "themoviedb.org" in text
    assert "TheSportsDB" not in text


def test_nothing_is_offered_once_the_keys_are_set():
    assert health.keys_tip(has_tmdb=True, has_sportsdb=True,
                           film_passes=3, team_passes=3) == []


def test_the_tip_says_that_series_already_work_without_a_key():
    """Nobody should come away thinking the feature needs configuring."""
    raised = health.keys_tip(has_tmdb=False, has_sportsdb=False,
                             film_passes=0, team_passes=1)
    assert "TVmaze" in raised[0]["detail"]
