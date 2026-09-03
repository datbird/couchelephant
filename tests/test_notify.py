"""Outbound alerts: what gets sent, once, and to whom.

Every send goes to `tests/fake_chat.py` over real HTTP on localhost, so the
transports are exercised through httpx rather than mocked away. The rules being
pinned here are the ones that decide whether a channel stays useful or gets
muted: a fault is announced once and then reminded on a schedule, an activity
event is announced exactly once ever, and a destination only ever receives the
events it was asked for.
"""
import time

import pytest

from app import db, health, notify
from tests import fake_chat

HOUR = 3600
DAY = 86400


@pytest.fixture(autouse=True)
def chat(monkeypatch):
    """The fake Discord and Telegram, and notify pointed at them.

    The Discord host allowlist is a server-side request forgery guard, not a
    test seam, so it is relaxed here rather than weakened in the app. Telegram
    has no user-supplied host at all, so only its base moves.
    """
    url = fake_chat.start()
    monkeypatch.setattr(notify, "TELEGRAM_BASE", url)
    monkeypatch.setattr(notify, "DISCORD_HOSTS", frozenset({"127.0.0.1", "localhost"}))
    monkeypatch.setattr(notify, "DISCORD_SCHEMES", frozenset({"http"}))
    monkeypatch.setattr(notify, "NOTIFIARR_BASE", url)
    yield url
    fake_chat.stop()


def _discord(chat, events, name="DVR alerts", remind_hours=24):
    return notify.save_destination(
        name=name, kind="discord", events=events, remind_hours=remind_hours,
        webhook=f"{chat}/api/webhooks/123/abc")


def _telegram(chat, events, name="Phone", remind_hours=24):
    return notify.save_destination(
        name=name, kind="telegram", events=events, remind_hours=remind_hours,
        token="111:aaa", chat_id="4242")


def _notifiarr(chat, events, name="Notifiarr", remind_hours=24, channel="735481457153277994"):
    return notify.save_destination(
        name=name, kind="notifiarr", events=events, remind_hours=remind_hours,
        token="nkey-abc", chat_id=channel)


def _raise(code, severity="bad", title="Guide has gone stale", now=None):
    health.record([{"code": code, "severity": severity, "title": title,
                    "detail": "Plex last refreshed 5 days ago.", "hint": "h"}],
                  now or int(time.time()), owns={code})


def _clear(code, now=None):
    health.record([], now or int(time.time()), owns={code})


# ---------- faults ----------

def test_a_fault_is_announced_once_not_every_sync(chat):
    _discord(chat, [health.EPG_STALE])
    _raise(health.EPG_STALE)

    assert notify.dispatch() == 1
    assert len(fake_chat.discord_sent()) == 1

    # The sync loop runs hourly. The same open notice must not be re-sent.
    assert notify.dispatch() == 0
    assert notify.dispatch() == 0
    assert len(fake_chat.discord_sent()) == 1


def test_the_message_carries_the_title_and_the_detail(chat):
    _discord(chat, [health.EPG_STALE])
    _raise(health.EPG_STALE)
    notify.dispatch()

    body = fake_chat.discord_sent()[0]
    blob = str(body)
    assert "Guide has gone stale" in blob
    assert "Plex last refreshed 5 days ago." in blob


def test_a_reminder_waits_for_its_interval(chat):
    _discord(chat, [health.EPG_STALE], remind_hours=24)
    now = int(time.time())
    _raise(health.EPG_STALE, now=now)

    assert notify.dispatch(now=now) == 1
    # Nearly a day is not a day.
    assert notify.dispatch(now=now + 23 * HOUR) == 0
    assert notify.dispatch(now=now + 24 * HOUR) == 1
    assert notify.dispatch(now=now + 25 * HOUR) == 0
    assert notify.dispatch(now=now + 48 * HOUR) == 1

    sent = fake_chat.discord_sent()
    assert len(sent) == 3
    assert "Still open" in str(sent[1])
    assert "Still open" in str(sent[2])


def test_zero_remind_hours_silences_reminders_but_not_the_open(chat):
    _discord(chat, [health.EPG_STALE], remind_hours=0)
    now = int(time.time())
    _raise(health.EPG_STALE, now=now)

    assert notify.dispatch(now=now) == 1
    assert notify.dispatch(now=now + 30 * DAY) == 0
    assert len(fake_chat.discord_sent()) == 1


def test_a_cleared_fault_sends_a_recovery_and_forgets_it(chat):
    dest = _discord(chat, [health.EPG_STALE])
    now = int(time.time())
    _raise(health.EPG_STALE, now=now)
    notify.dispatch(now=now)

    _clear(health.EPG_STALE, now=now + 2 * DAY)
    assert notify.dispatch(now=now + 2 * DAY) == 1

    sent = fake_chat.discord_sent()
    assert len(sent) == 2
    assert "Cleared" in str(sent[1])
    # The state row is gone, so the same fault happening again is a new fault.
    assert db.one("SELECT COUNT(*) c FROM notify_state WHERE destination_id = ?",
                  (dest,))["c"] == 0

    _raise(health.EPG_STALE, now=now + 3 * DAY)
    assert notify.dispatch(now=now + 3 * DAY) == 1


def test_a_tip_never_sends(chat):
    # Asked for by name, which is the strongest form of the request, and still
    # refused: a suggestion is not worth a notification.
    _discord(chat, [health.KEYS_AVAILABLE, health.EPG_STALE])
    _raise(health.KEYS_AVAILABLE, severity=health.TIP, title="Two optional keys")

    assert notify.dispatch() == 0
    assert fake_chat.discord_sent() == []


def test_the_catalog_excludes_tips_entirely(chat):
    assert health.KEYS_AVAILABLE not in notify.CATALOG_CODES
    assert health.EPG_STALE in notify.CATALOG_CODES
    assert notify.RECORDING_STARTED in notify.CATALOG_CODES


# ---------- routing ----------

def test_two_destinations_each_get_only_their_own_events(chat):
    _discord(chat, [health.EPG_STALE], name="Faults")
    _telegram(chat, [health.TEAM_PASS_UNMATCHED], name="Phone")

    _raise(health.EPG_STALE)
    notify.dispatch()

    assert len(fake_chat.discord_sent()) == 1
    assert fake_chat.telegram_sent() == []

    _raise(health.TEAM_PASS_UNMATCHED, severity="warn", title="No games")
    notify.dispatch()

    assert len(fake_chat.discord_sent()) == 1
    assert len(fake_chat.telegram_sent()) == 1


def test_a_destination_added_today_hears_nothing_about_yesterday(chat):
    now = int(time.time())
    _discord(chat, [health.EPG_STALE], name="First")
    _raise(health.EPG_STALE, now=now)
    notify.dispatch(now=now)
    assert len(fake_chat.discord_sent()) == 1

    # A channel added while a fault is already open must not be handed a
    # backlog. It starts from now.
    _telegram(chat, [health.EPG_STALE], name="Late")
    notify.dispatch(now=now + 60)
    assert fake_chat.telegram_sent() == []

    # But it does hear the recovery, because it is a thing that happens after.
    _clear(health.EPG_STALE, now=now + DAY)
    notify.dispatch(now=now + DAY)
    assert len(fake_chat.telegram_sent()) == 0
    assert len(fake_chat.discord_sent()) == 2


def test_a_disabled_destination_sends_nothing(chat):
    dest = _discord(chat, [health.EPG_STALE])
    notify.set_enabled(dest, False)
    _raise(health.EPG_STALE)

    assert notify.dispatch() == 0
    assert fake_chat.discord_sent() == []


def test_a_destination_with_no_events_sends_nothing(chat):
    _discord(chat, [])
    _raise(health.EPG_STALE)

    assert notify.dispatch() == 0


# ---------- activity ----------

def _book(airing_id, title="Chiefs at Broncos", begins_at=None):
    with db.tx() as c:
        c.execute("INSERT INTO our_grabs (airing_id, program_guid, title, "
                  "channel_vcn, begins_at, source, created_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (airing_id, f"g/{airing_id}", title, "41.1",
                   begins_at or int(time.time()) + DAY, "pass", int(time.time())))


def _grab(grab_id, begins_at, status=None, title="Chiefs at Broncos"):
    with db.tx() as c:
        c.execute("INSERT INTO plex_grabs (id, subscription, status, title, "
                  "parent_title, channel_vcn, begins_at, ends_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                  (grab_id, "1", status, title, "NFL Football", "41.1",
                   begins_at, begins_at + 10800, int(time.time())))


def test_a_booking_is_announced_once_however_many_syncs_run(chat):
    _discord(chat, [notify.PASS_BOOKED])
    _book("a1")

    assert notify.dispatch() == 1
    assert notify.dispatch() == 0
    assert notify.dispatch() == 0
    assert len(fake_chat.discord_sent()) == 1
    assert "Chiefs at Broncos" in str(fake_chat.discord_sent()[0])

    # A second, different booking is its own event.
    _book("a2", title="Seahawks at Chiefs")
    assert notify.dispatch() == 1


def test_a_recording_starts_when_its_time_passes_not_when_plex_says_so(chat):
    """Only `complete` was ever observed on a live server, so the start signal
    cannot depend on knowing Plex's other status words."""
    _discord(chat, [notify.RECORDING_STARTED])
    now = int(time.time())
    _grab("g1", begins_at=now + HOUR)

    # Still in the future.
    assert notify.dispatch(now=now) == 0
    # Its time has come.
    assert notify.dispatch(now=now + HOUR + 60) == 1
    assert notify.dispatch(now=now + 2 * HOUR) == 0


def test_a_recording_finishes_when_it_completes_or_when_it_vanishes(chat):
    _discord(chat, [notify.RECORDING_STARTED, notify.RECORDING_FINISHED])
    now = int(time.time())
    _grab("g1", begins_at=now - HOUR)
    _grab("g2", begins_at=now - HOUR, title="Something Else")
    notify.dispatch(now=now)          # two starts
    assert len(fake_chat.discord_sent()) == 2

    # One reaches a terminal status. The other is dropped by sync_recordings,
    # which is what Plex doing the same thing looks like from here.
    _grab("g1", begins_at=now - HOUR, status="complete")
    with db.tx() as c:
        c.execute("DELETE FROM plex_grabs WHERE id = 'g2'")

    assert notify.dispatch(now=now + HOUR) == 2
    assert notify.dispatch(now=now + 2 * HOUR) == 0


def test_a_failed_sync_is_announced_once(chat):
    _discord(chat, [notify.SYNC_FAILED])
    with db.tx() as c:
        c.execute("INSERT INTO sync_log (started_at, ended_at, ok, detail) "
                  "VALUES (?,?,0,?)",
                  (int(time.time()), int(time.time()), "ConnectError: refused"))

    assert notify.dispatch() == 1
    assert notify.dispatch() == 0
    assert "ConnectError" in str(fake_chat.discord_sent()[0])


def test_activity_state_is_pruned_but_open_faults_are_not(chat):
    dest = _discord(chat, [notify.PASS_BOOKED, health.EPG_STALE])
    now = int(time.time())
    _book("a1")
    _raise(health.EPG_STALE, now=now)
    notify.dispatch(now=now)
    assert db.one("SELECT COUNT(*) c FROM notify_state")["c"] == 2

    notify.dispatch(now=now + 40 * DAY)
    rows = db.query("SELECT event FROM notify_state WHERE destination_id = ?", (dest,))
    assert [r["event"] for r in rows] == [health.EPG_STALE]


# ---------- failure, and secrets ----------

def test_a_send_that_fails_does_not_break_the_run(chat):
    _discord(chat, [health.EPG_STALE])
    _telegram(chat, [health.EPG_STALE])
    fake_chat.DISCORD_STATUS = 401          # a revoked webhook
    _raise(health.EPG_STALE)

    # Discord fails, Telegram still gets its message, dispatch still returns.
    assert notify.dispatch() == 1
    assert len(fake_chat.telegram_sent()) == 1


def test_a_failed_send_is_retried_on_the_next_sync(chat):
    _discord(chat, [health.EPG_STALE])
    fake_chat.DISCORD_STATUS = 401
    _raise(health.EPG_STALE)
    assert notify.dispatch() == 0

    # A message that never arrived was never delivered, so nothing was recorded
    # and the next sync tries again. Otherwise one blip loses the alert forever.
    fake_chat.DISCORD_STATUS = 204
    assert notify.dispatch() == 1


def test_telegram_reports_failure_in_its_body_not_its_status(chat):
    _telegram(chat, [health.EPG_STALE])
    fake_chat.TELEGRAM_OK = False
    _raise(health.EPG_STALE)

    # HTTP 200 with ok:false is Telegram's way of saying no. Treating the
    # status alone as success would record a delivery that never happened.
    assert notify.dispatch() == 0


def test_a_webhook_that_is_not_discord_is_refused(chat):
    for bad in ("https://evil.example.com/api/webhooks/1/2",
                "http://169.254.169.254/latest/meta-data/",
                "https://discord.com.evil.example.com/api/webhooks/1/2",
                "file:///etc/passwd",
                "http://discord.com/api/webhooks/1/2"):   # not https
        assert not notify.valid_webhook(
            bad, hosts=notify.REAL_DISCORD_HOSTS,
            schemes=notify.REAL_DISCORD_SCHEMES), bad

    for good in ("https://discord.com/api/webhooks/123/abc",
                 "https://canary.discord.com/api/webhooks/123/abc"):
        assert notify.valid_webhook(
            good, hosts=notify.REAL_DISCORD_HOSTS,
            schemes=notify.REAL_DISCORD_SCHEMES), good


def test_saving_a_bad_webhook_is_rejected(chat):
    with pytest.raises(ValueError):
        notify.save_destination(name="Bad", kind="discord", events=[],
                                webhook="https://evil.example.com/x",
                                hosts=notify.REAL_DISCORD_HOSTS,
                                schemes=notify.REAL_DISCORD_SCHEMES)


def test_secrets_never_leave_the_database(chat, client):
    _discord(chat, [health.EPG_STALE])
    _telegram(chat, [health.EPG_STALE])

    body = client.get("/partial/settings").text
    assert f"{chat}/api/webhooks/123/abc" not in body
    assert "111:aaa" not in body
    # And the list the UI reads is masked at the source, not in the template.
    for d in notify.destinations():
        assert "abc" not in (d.get("webhook") or "")
        assert "111:aaa" != d.get("token")


def test_the_test_button_says_what_happened(chat):
    dest = _discord(chat, [health.EPG_STALE])
    assert notify.test(dest).startswith("OK")

    fake_chat.DISCORD_STATUS = 401
    verdict = notify.test(dest)
    assert not verdict.startswith("OK")
    assert "401" in verdict


def test_finding_a_telegram_chat_id(chat):
    assert notify.find_chat_id("111:aaa") is None       # nobody has messaged it

    fake_chat.TELEGRAM_UPDATES = [
        {"update_id": 1, "message": {"chat": {"id": 987654, "first_name": "Robert"},
                                     "text": "hello"}}]
    assert notify.find_chat_id("111:aaa") == "987654"


def test_the_settings_page_carries_the_section_and_the_catalog(chat, client):
    body = client.get("/settings").text
    assert 'data-sec="notify"' in body
    assert "Add a destination" in body
    # Every catalog code has a checkbox, and the tip has none.
    for code, _ in notify.CATALOG:
        assert f'value="{code}"' in body, code
    assert f'value="{health.KEYS_AVAILABLE}"' not in body


def test_an_existing_destination_renders_its_editor(chat, client):
    _discord(chat, [health.EPG_STALE], name="DVR alerts")
    body = client.get("/settings").text
    assert "DVR alerts" in body
    assert "Edit DVR alerts" in body
    # The event it carries is ticked, one it does not is not.
    assert body.count('name="events"') >= len(notify.CATALOG)


# ---------- notifiarr ----------

def test_notifiarr_carries_the_channel_the_title_and_the_colour(chat):
    _notifiarr(chat, [health.EPG_STALE])
    _raise(health.EPG_STALE)
    assert notify.dispatch() == 1

    body = fake_chat.notifiarr_sent()[0]
    assert body["notification"]["name"] == "CouchElephant"
    # An integer, not a string. Notifiarr's schema says integer, and a quoted
    # id is the mistake that makes it silently go nowhere.
    assert body["discord"]["ids"]["channel"] == 735481457153277994
    assert isinstance(body["discord"]["ids"]["channel"], int)
    assert body["discord"]["text"]["title"] == "Guide has gone stale"
    assert "Plex last refreshed 5 days ago." in body["discord"]["text"]["description"]
    # Six hex digits, no leading hash, which is what the API asks for.
    assert body["discord"]["color"] == "D1453B"


def test_notifiarr_reports_failure_in_its_body_not_its_status(chat):
    _notifiarr(chat, [health.EPG_STALE])
    fake_chat.NOTIFIARR_OK = False
    _raise(health.EPG_STALE)

    # 200 with an error in the body. Trusting the status alone would record a
    # delivery that never happened, and the alert would be lost for good.
    assert notify.dispatch() == 0
    # So the next sync tries again.
    fake_chat.NOTIFIARR_OK = True
    assert notify.dispatch() == 1


def test_notifiarr_refuses_a_channel_that_is_not_an_id(chat):
    dest = _notifiarr(chat, [health.EPG_STALE], channel="general")
    _raise(health.EPG_STALE)
    assert notify.dispatch() == 0
    assert fake_chat.notifiarr_sent() == []
    # And the verdict says how to get the right value, not just that this one
    # is wrong. "Invalid channel" would leave somebody stuck.
    verdict = notify.test(dest).lower()
    assert "channel id" in verdict and "developer mode" in verdict


def test_notifiarr_masks_its_api_key(chat, client):
    _notifiarr(chat, [health.EPG_STALE])
    assert "nkey-abc" not in client.get("/partial/settings").text
    for d in notify.destinations():
        assert d.get("token") != "nkey-abc"


def test_all_three_kinds_route_side_by_side(chat):
    _discord(chat, [health.EPG_STALE], name="Discord")
    _telegram(chat, [health.EPG_STALE], name="Phone")
    _notifiarr(chat, [health.EPG_STALE], name="Relay")
    _raise(health.EPG_STALE)

    assert notify.dispatch() == 3
    assert len(fake_chat.discord_sent()) == 1
    assert len(fake_chat.telegram_sent()) == 1
    assert len(fake_chat.notifiarr_sent()) == 1
    # And each keeps its own state, so none of them repeats.
    assert notify.dispatch() == 0


def test_an_unknown_kind_is_refused(chat):
    with pytest.raises(ValueError):
        notify.save_destination(name="x", kind="carrier-pigeon", events=[])


def test_an_empty_field_renders_empty_not_the_word_none(chat, client):
    """`X if d else ''` yields Python's None for a NULL column, and Jinja prints
    that as the word "None" inside the input box. A Discord destination has no
    chat id, so that field showed it."""
    _discord(chat, [health.EPG_STALE], name="DVR alerts")
    body = client.get("/settings").text
    assert 'value="None"' not in body


# ---------- carried with the rest of your decisions ----------

def test_a_destination_travels_with_an_export(chat):
    """A destination is a decision, like a pass, so a restore has to bring it
    back. It was silently dropped when the feature first shipped."""
    from app import dbstore
    _discord(chat, [health.EPG_STALE], name="DVR alerts")
    rows = dbstore.read("destinations", include_secrets=True)
    assert len(rows) == 1
    rec = next(iter(rows.values()))
    assert rec["name"] == "DVR alerts"
    assert rec["events"] == health.EPG_STALE
    assert rec["webhook"]


def test_a_destination_is_keyed_by_uid_not_by_name_or_id(chat):
    """Two channels may share a name, and `id` differs between installs."""
    from app import dbstore
    _discord(chat, [health.EPG_STALE], name="Discord")
    _discord(chat, [health.GUIDE_SHORT], name="Discord")
    rows = dbstore.read("destinations", include_secrets=True)
    assert len(rows) == 2, "a shared name must not collapse two rows into one"
    assert all(len(k) == 32 for k in rows)


def test_the_secrets_stay_behind_unless_the_export_asked_for_them(chat):
    """Same rule as plex_token. An export shared with somebody must not hand
    them a webhook that posts into your channel."""
    from app import dbstore
    _discord(chat, [health.EPG_STALE])
    _telegram(chat, [health.EPG_STALE])
    _notifiarr(chat, [health.EPG_STALE])
    for rec in dbstore.read("destinations").values():
        assert "webhook" not in rec
        assert "token" not in rec
        # What it is and what it carries still travel, so a restore knows what
        # to ask you to paste back in.
        assert rec["kind"] and rec["events"]
