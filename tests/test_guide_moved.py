"""What happens to a booking when the guide moves the broadcast under it.

A guide refresh re-times a game and renumbers its airings. Both of those move
the identity the app books against, and three separate places used to read
that move as something it was not:

  - the schedule kept showing NOT RECORDING for a game that was recording,
    because a failure is keyed on a channel and a start time;
  - the old subscription stayed on Plex for ever, pinned to a slot nothing
    airs in, because a booking is keyed on an airing id;
  - the pass would not look at the game again, because a single historical
    log line said it had once been scheduled.

Measured on a live DVR on 2026-09-13. One NFL game shifted from 7:00 PM to
7:15 PM on the same channel. It left a red row that could not clear, a second
subscription, and an orphan of the first.
"""
import time

from app import db, passes, sync
from tests import fake_plex

HOUR = 3600


def _chiefs_pass(prefs=None):
    team = db.one("SELECT * FROM teams WHERE name LIKE '%Chiefs%'")
    assert team, "the fake guide should carry the Chiefs"
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, networks, channels, "
                  "prefs, uid, enabled, created_at) "
                  "VALUES ('team',?,?,'[]','[]',?,'u1',1,?)",
                  (team["id"], team["name"], db.js(prefs or {}), int(time.time())))
    return db.one("SELECT * FROM passes")


def _guide_says(plex, when):
    """The guide now carries the live game at this time, and we have read it.

    Moved on the server and then pulled in, never written straight into the
    database. The server decides what it still has scheduled by reading its
    own listing, so a database that disagreed with the guide would be a state
    no real install can reach.

    The guide and Plex's own schedule, and nothing else. A whole sync would
    run the booking check too, and then every test here would be asserting
    against work that had already happened somewhere it could not watch.
    """
    fake_plex.move_broadcast(fake_plex.GAME_GUID, fake_plex.LIVE_AT, when)
    provider, shows, sports, movies = sync.discover(plex)
    sync.sync_guide(plex, provider, shows, sports, movies)
    sync.sync_recordings(plex)
    return when


def _guide_drops_the_game(plex):
    """The guide stops carrying the programme at all, and we have read that."""
    fake_plex.drop_from_guide(fake_plex.GAME_GUID)
    provider, shows, sports, movies = sync.discover(plex)
    sync.sync_guide(plex, provider, shows, sports, movies)
    sync.sync_recordings(plex)


def _kickoff(plex, hours=6):
    """Put the game far enough out that a repair is allowed at all."""
    when = int(time.time()) + hours * HOUR
    fake_plex.move_broadcast(fake_plex.GAME_GUID, fake_plex.LIVE_AT, when)
    ok, detail = sync.full_sync()
    assert ok, detail
    return when


def _guide_renumbers(plex):
    """A guide refresh mints new ids for the same broadcasts, and we read it."""
    fake_plex.renumber()
    provider, shows, sports, movies = sync.discover(plex)
    sync.sync_guide(plex, provider, shows, sports, movies)
    sync.sync_recordings(plex)


def _booked():
    return db.query("SELECT * FROM our_grabs ORDER BY created_at")


def _live_subs():
    return sorted(fake_plex.STATE.subscriptions)


def _pinned(key):
    sub = fake_plex.STATE.subscriptions[key]
    return {s["id"]: s["value"] for s in sub["Setting"]}["startTimeslot"]


# ---- the guide re-times a game that is already booked ----

def test_one_game_keeps_exactly_one_recording_when_the_guide_moves_it(plex, synced):
    """The whole story, end to end. Two subscriptions for one game is what a
    live DVR was left holding, and the second one was the only real booking."""
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    assert len(_booked()) == 1
    assert len(_live_subs()) == 1

    now_at = _guide_says(plex, was + 15 * 60)
    sync.check_bookings(plex)
    passes.run_passes()

    assert len(_live_subs()) == 1, "the game must not be booked twice"
    assert _pinned(_live_subs()[0]) == str(now_at), "and it must name the new time"
    assert len(_booked()) == 1, "one booking, not one per time the guide tried"


def test_the_recording_follows_the_game_rather_than_the_clock(plex, synced):
    """The point of the whole app is that the pin names the right broadcast.
    A pin left on the old time records nothing at all."""
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    old_key = _live_subs()[0]

    now_at = _guide_says(plex, was + 15 * 60)
    out = sync.check_bookings(plex)

    assert out["repaired"] == 1, out
    assert old_key in fake_plex.STATE.deleted, "the stale subscription must go"
    assert _pinned(_live_subs()[0]) == str(now_at)
    assert db.one("SELECT 1 FROM plex_grabs WHERE begins_at = ?", (now_at,))


def test_a_re_point_is_written_into_the_pass_history(plex, synced):
    """Say so. A correction you cannot see afterwards is not a correction."""
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    _guide_says(plex, was + 15 * 60)
    sync.check_bookings(plex)

    row = db.one("SELECT * FROM pass_actions WHERE action = 'repaired' "
                 "ORDER BY id DESC")
    assert row, "a re-point must leave a trace"
    assert "moved" in row["reason"]


def test_the_booking_it_replaced_is_not_checked_again(plex, synced):
    """A re-point books a new airing id. The row naming the old one has to go
    with it, or every sync from here on checks a broadcast that never returns."""
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    old_id = _booked()[0]["airing_id"]

    _guide_says(plex, was + 15 * 60)
    sync.check_bookings(plex)

    assert not db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (old_id,))


def test_a_settled_re_point_is_left_alone_on_every_later_sync(plex, synced):
    """The loop guard. A check that cancelled and re-booked on every sync
    would do it for ever, against a live DVR."""
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    _guide_says(plex, was + 15 * 60)
    sync.check_bookings(plex)
    settled = len(fake_plex.STATE.deleted)

    for _ in range(3):
        sync.sync_recordings(plex)
        out = sync.check_bookings(plex)
        assert out["repaired"] == 0 and out["cancelled"] == 0, out
        passes.run_passes()

    assert len(fake_plex.STATE.deleted) == settled, "nothing more was cancelled"
    assert len(_live_subs()) == 1


def test_a_move_too_close_to_kickoff_raises_a_notice_instead(plex, synced):
    """Cancelling minutes before a game risks losing it outright. A wrong pin
    that someone is told about beats a gamble nobody asked for."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    old_key = _live_subs()[0]

    # The guide moves it, and the game is now twenty minutes away.
    _guide_says(plex, int(time.time()) + 20 * 60)
    out = sync.check_bookings(plex)

    assert out["drifted"] == 1 and out["repaired"] == 0, out
    assert old_key not in fake_plex.STATE.deleted
    assert db.one("SELECT * FROM notices WHERE code = 'booking_drift' "
                  "AND resolved_at IS NULL")


# ---- an airing that leaves the guide for good ----

def test_a_subscription_for_a_game_the_guide_dropped_is_cancelled(plex, synced):
    """Otherwise it sits on the DVR for ever, pinned to a slot nothing airs in.
    Over a season that is one piece of rubbish per re-timed game."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    key = _live_subs()[0]

    _guide_drops_the_game(plex)
    out = sync.check_bookings(plex)

    assert out["cancelled"] == 1, out
    assert key in fake_plex.STATE.deleted
    assert not _booked(), "and our own record of it goes too"


def test_nothing_is_cancelled_while_plex_still_holds_the_recording(plex, synced):
    """Plex knows more about its own schedule than we do. A guide that has
    merely shrunk must never be read as permission to cancel a recording."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    key = _live_subs()[0]
    sync.sync_recordings(plex)

    # The programme leaves our copy of the guide, but Plex is still recording.
    with db.tx() as c:
        c.execute("DELETE FROM airings WHERE program_guid = ?",
                  (fake_plex.GAME_GUID,))
    out = sync.check_bookings(plex)

    assert out["cancelled"] == 0, out
    assert key not in fake_plex.STATE.deleted
    assert out["unchecked"] == 1, out


def test_a_second_booking_of_one_game_is_cancelled_rather_than_re_booked(plex,
                                                                        synced):
    """Two subscriptions for one game is the state a live DVR was found in.
    Clearing it must not book a third."""
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    stale = _booked()[0]

    # The guide moves the game and a second booking is made at the new time,
    # exactly as the live server did before any of this was checked.
    now_at = _guide_says(plex, was + 15 * 60)
    row = db.one("""SELECT a.*, p.title, p.rating_key FROM airings a
                    JOIN programs p ON p.guid = a.program_guid
                    WHERE a.begins_at = ?""", (now_at,))
    passes._schedule(plex, row, None, "pass", pass_id=1)
    sync.sync_recordings(plex)
    assert len(_live_subs()) == 2

    out = sync.check_bookings(plex)

    assert out["cancelled"] == 1, out
    assert stale["subscription"] in fake_plex.STATE.deleted
    assert len(_live_subs()) == 1
    assert _pinned(_live_subs()[0]) == str(now_at)


# ---- what a pass is allowed to treat as already handled ----

def test_a_game_whose_recording_was_cancelled_is_booked_again(plex, synced):
    """The log is not the live state. A pass that had ever scheduled a game
    would never look at it again, so a recording cancelled afterwards, by
    Plex, by the user, or by a guide change, was never replaced."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    key = _booked()[0]["subscription"]

    # Cancelled the way the Recordings tab cancels one.
    plex.delete_subscription(key)
    passes.forget(_booked()[0]["airing_id"])
    sync.sync_recordings(plex)

    passes.run_passes()

    assert len(_booked()) == 1, "the pass should have booked it again"
    assert _booked()[0]["subscription"] != key


def test_a_pass_does_not_book_a_game_it_has_just_booked(plex, synced):
    """The other half. Running twice in a row must not make two recordings,
    including in the moment before Plex has been read back."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    passes.run_passes()
    passes.run_passes()

    assert len(_live_subs()) == 1
    assert len(_booked()) == 1


def test_a_pass_leaves_a_game_plex_is_already_recording_alone(plex, synced):
    """Unchanged behaviour, and the reason the check exists at all."""
    when = _kickoff(plex)
    with db.tx() as c:
        c.execute("""INSERT INTO plex_grabs (id, subscription, status, title,
                                             channel_vcn, begins_at, updated_at)
                     VALUES ('g1','7','scheduled','Chiefs at Buccaneers',
                             '41.1',?,0)""", (when,))
    _chiefs_pass()
    passes.run_passes()
    assert not _booked(), "Plex already has it"


# ---- the red row in the schedule ----

def _failed_attempt(airing_id, n=3):
    a = db.one("SELECT * FROM airings WHERE id = ?", (airing_id,))
    at = int(time.time())
    with db.tx() as c:
        for i in range(n):
            c.execute(
                """INSERT INTO pass_actions (pass_id, program_guid, airing_id,
                                             program_title, channel_vcn, begins_at,
                                             action, reason, dry_run, created_at)
                   VALUES (1,?,?,?,?,?,'failed','PlexError: HTTP 400',0,?)""",
                (a["program_guid"], airing_id, a["title"] if "title" in a.keys()
                 else "Chiefs at Buccaneers", a["channel_vcn"], a["begins_at"],
                 at - (n - i) * 3600))


def test_a_failure_stops_showing_once_the_game_records_at_a_new_time(client, plex,
                                                                     synced):
    """The row a user actually saw. Failures are keyed on a channel and a
    start time, so moving the game fifteen minutes left NOT RECORDING sitting
    above the very booking that was recording it."""
    was = _kickoff(plex)
    _chiefs_pass()
    old_airing = db.one("SELECT id FROM airings WHERE begins_at = ?", (was,))["id"]
    _failed_attempt(old_airing)
    assert [r for r in client.get("/api/schedule").json()["rows"]
            if r["status"] == "failed"], "it should show while nothing records it"

    _guide_says(plex, was + 15 * 60)
    passes.run_passes()
    sync.sync_recordings(plex)

    rows = client.get("/api/schedule").json()["rows"]
    assert not [r for r in rows if r["status"] == "failed"], rows


def test_a_failure_still_shows_when_nothing_is_recording_the_game(client, plex,
                                                                  synced):
    """The guard on the guard. A red row that could be cleared by any other
    row would be worth nothing, and the wall of failures it exists for would
    go back to looking like a quiet week."""
    was = _kickoff(plex)
    _chiefs_pass()
    _failed_attempt(db.one("SELECT id FROM airings WHERE begins_at = ?", (was,))["id"])
    _guide_says(plex, was + 15 * 60)

    rows = client.get("/api/schedule").json()["rows"]
    fail = [r for r in rows if r["status"] == "failed"]
    assert len(fail) == 1, rows
    assert fail[0]["attempts"] == 3


# ---- our copy of what Plex holds ----

def test_a_subscription_plex_no_longer_has_leaves_our_copy_at_once(plex, synced):
    """Pruned by what the pull saw, never by a timestamp.

    Two pulls inside one second used to keep every row the first one wrote,
    because its stamp was not lower than the second one's. A subscription this
    app had just cancelled then survived in our copy, and `already_handled`
    read it as a live booking and left the game unrecorded.
    """
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    key = _booked()[0]["subscription"]
    sync.sync_recordings(plex)
    assert db.one("SELECT 1 FROM plex_subscriptions WHERE key = ?", (key,))

    plex.delete_subscription(key)
    sync.sync_recordings(plex)            # same second as the pull above

    assert not db.one("SELECT 1 FROM plex_subscriptions WHERE key = ?", (key,))
    assert not db.one("SELECT 1 FROM plex_grabs WHERE subscription = ?", (key,))


# ---- a guide refresh that only renumbers ----

def test_a_booking_survives_a_renumber_and_is_still_checked(plex, synced):
    """The quiet half of a guide refresh. Nothing about the broadcast changes,
    so nothing needs repairing, but the booking still names an id that no
    longer exists. Read only by that id, it drops out of the check for ever,
    and every later change to the pass misses it in silence.
    """
    _kickoff(plex)
    p = _chiefs_pass({"endOffsetMinutes": "0"})
    passes.run_passes()
    _guide_renumbers(plex)

    with db.tx() as c:
        c.execute("UPDATE passes SET prefs = ?", (db.js({"endOffsetMinutes": "30"}),))
    out = sync.check_bookings(plex)

    assert out["unchecked"] == 0, out
    assert out["repaired"] == 1, out
    key = _booked()[0]["subscription"]
    got = {s["id"]: s["value"] for s in (plex.subscription(key).get("Setting") or [])}
    assert str(got.get("endOffsetMinutes")) == "30", got
    assert len(_booked()) == 1, "one booking, not one per id the guide has used"
    assert p


def test_a_renumbered_booking_that_agrees_is_left_alone(plex, synced):
    """The loop guard on the same path. A renumber on its own is not drift."""
    _kickoff(plex)
    _chiefs_pass({"endOffsetMinutes": "30"})
    passes.run_passes()
    _guide_renumbers(plex)

    for _ in range(3):
        out = sync.check_bookings(plex)
        assert out["repaired"] == 0 and out["cancelled"] == 0, out
        sync.sync_recordings(plex)

    assert fake_plex.STATE.deleted == [], "nothing should have been cancelled"


# ---- clicking a recording after the guide renumbered ----

def test_a_recording_still_opens_after_the_guide_renumbers(client, plex, synced):
    """The row a person actually clicks.

    A booking stores the airing id it was made against. A guide refresh mints
    new ids for broadcasts that have not changed at all, so the stored one
    retires, and the row went on carrying it. Clicking a perfectly good
    recording then opened a panel reading "not found", which is how a correct
    recording comes to look broken.
    """
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    _guide_renumbers(plex)

    row = [r for r in client.get("/api/schedule").json()["rows"]
           if r["status"] == "scheduled"][0]
    assert row["airing_id"], row
    assert db.one("SELECT 1 FROM airings WHERE id = ?", (row["airing_id"],)), \
        "the row must name a broadcast the guide still has"
    assert client.get("/api/program",
                      params={"airing_id": row["airing_id"]}).status_code == 200


def test_a_broadcast_that_really_has_gone_says_which(client, synced):
    """And when it truly is not there, say so in words rather than "not found"."""
    r = client.get("/api/program", params={"airing_id": "plex://episode/nope#1"})
    assert r.status_code == 404
    assert "guide" in r.json()["error"].lower(), r.json()


# ---- the two silences the cleanup review found ----

def test_a_re_point_plex_refuses_raises_a_notice(plex, synced, monkeypatch):
    """A re-point that fails must shout, like every other failed repair.

    It was counted in the sync line and never added to the list the notices
    are built from, so the one outcome worse than the drift it was fixing
    happened quietly.
    """
    was = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    _guide_says(plex, was + 15 * 60)
    monkeypatch.setattr(passes, "_schedule",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Plex said no")))

    out = sync.check_bookings(plex)

    assert out["failed"] == 1, out
    assert db.one("SELECT * FROM notices WHERE code = 'booking_repair_failed' "
                  "AND resolved_at IS NULL"), "a failed re-point has to be visible"


def test_a_booking_with_no_subscription_and_no_broadcast_is_cancelled(plex, synced):
    """Both halves gone at once, which is the ordinary end of a stale booking.

    Plex is asked first, so a lost subscription used to win the branch and send
    this to the re-book path. There is nothing to re-book from, so it failed,
    and it failed again on every sync for ever while raising a notice each
    time. Whether our own guide still carries the broadcast is a question about
    our data, and it has to be asked before Plex's answer is interpreted.
    """
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    key = _booked()[0]["subscription"]
    plex.delete_subscription(key)
    _guide_drops_the_game(plex)

    out = sync.check_bookings(plex)

    assert out["cancelled"] == 1, out
    assert out["failed"] == 0, out
    assert not _booked(), "and our record of it goes with it"
    assert not db.one("SELECT 1 FROM notices WHERE code = 'booking_repair_failed' "
                      "AND resolved_at IS NULL")


# ---- the root of the whole family ----

def test_a_renumber_does_not_change_a_broadcasts_id(plex, synced):
    """An airing id is minted from the slot, so it does not move when Plex
    renumbers its guide. Every other fix in this file exists because it did."""
    before = {r["id"] for r in db.query("SELECT id FROM airings")}
    _guide_renumbers(plex)
    after = {r["id"] for r in db.query("SELECT id FROM airings")}
    assert before == after, "a renumber alone must not rename a broadcast"


def test_a_re_time_does_change_it(plex, synced):
    """The other half. A re-time is a real change, and the id says so, which
    is what sends it to the re-point path rather than leaving it silent."""
    was = _kickoff(plex)
    before = {r["id"] for r in db.query("SELECT id FROM airings")}
    _guide_says(plex, was + 15 * 60)
    assert {r["id"] for r in db.query("SELECT id FROM airings")} != before


def test_the_being_recorded_filter_survives_a_renumber(client, plex, synced):
    """A guide filter keyed on the stored id stopped matching a booked game."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    booked = _booked()[0]["airing_id"]
    _guide_renumbers(plex)

    assert db.one("SELECT 1 FROM airings WHERE id = ? AND id IN "
                  "(SELECT airing_id FROM our_grabs)", (booked,))


def test_the_guide_still_says_we_booked_it_after_a_renumber(client, plex, synced):
    """The grid re-labelled a booking of ours as one of Plex's own."""
    when = _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    sync.sync_recordings(plex)
    _guide_renumbers(plex)

    rows = client.get("/api/schedule").json()["rows"]
    mine = [r for r in rows if r["b"] == when]
    assert mine and mine[0]["who"] == "ce", mine


def test_cancelling_by_hand_works_after_a_renumber(client, plex, synced):
    """It answered "CouchElephant did not schedule this" for a recording
    CouchElephant had scheduled."""
    _kickoff(plex)
    _chiefs_pass()
    passes.run_passes()
    _guide_renumbers(plex)
    aid = db.one("SELECT id FROM airings WHERE program_guid = ? AND premiere = 1",
                 (fake_plex.GAME_GUID,))["id"]

    r = client.post("/api/record/cancel", data={"airing_id": aid})
    assert r.json()["ok"], r.text
    assert not _booked()
