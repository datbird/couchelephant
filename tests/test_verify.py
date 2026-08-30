"""Does the recording Plex is holding still match the pass that booked it?

The failure this exists for already happened here. A pass booked a game, the
pass's settings were changed hours later, and the booking kept the old ones for
ever. Nothing errored. The pass ran, the recording existed, and the game was
cut off at the end.

The hard part is not noticing a difference. It is refusing to invent one. Plex
answers `oneShot` as the string 'true' when we asked for '1', and returns
numbers as strings on one payload and ints on another. A comparison that
counted those as drift would cancel and re-book the same recording on every
sync, for ever, against a live DVR. Half the tests here are that guard.
"""
import time

from app import db, health, passes, sync, verify
from tests import fake_plex

MIN = 60
HOUR = 3600


# ---- comparing one value, which is where the danger is ----

def test_plex_saying_true_to_our_one_means_the_same_thing():
    """`oneShot` goes out as '1' and comes back as 'true'. Not drift."""
    assert verify.same("1", "true")
    assert verify.same("0", "false")


def test_a_number_as_a_string_matches_the_same_number_as_an_int():
    assert verify.same("30", 30)
    assert verify.same(30, "30")


def test_values_that_really_differ_are_not_folded_together():
    assert not verify.same("30", "0")
    assert not verify.same("1", "false")
    assert not verify.same("id-41-1", "id-38-1")


def test_padding_of_zero_is_not_confused_with_padding_of_thirty():
    """The bug that started this. Zero and thirty must never compare equal."""
    assert not verify.same("0", "30")
    assert not verify.same(0, "30")


def test_a_value_nobody_reported_is_not_equal_to_one_that_was():
    assert not verify.same("30", None)
    assert verify.same(None, None)


# ---- comparing a whole booking ----

def _want(**over):
    w = {"endOffsetMinutes": "30", "startOffsetMinutes": "1", "oneShot": "1",
         "lineupChannel": "id-41-1", "startTimeslot": "1000"}
    w.update(over)
    return w


def test_a_booking_that_matches_reports_nothing():
    have = {"endOffsetMinutes": "30", "startOffsetMinutes": "1", "oneShot": "true",
            "lineupChannel": "id-41-1", "startTimeslot": 1000}
    assert verify.compare(want=_want(), have=have) == []


def test_padding_that_no_longer_matches_the_pass_is_drift():
    diffs = verify.compare(want=_want(), have=dict(_want(), endOffsetMinutes="0"))
    assert [d["field"] for d in diffs] == ["endOffsetMinutes"]
    assert diffs[0]["kind"] == "differs"
    assert diffs[0]["want"] == "30" and diffs[0]["have"] == "0"


def test_a_recording_plex_no_longer_holds_is_the_worst_kind_of_drift():
    assert [d["kind"] for d in verify.compare(want=_want(), have=None)] == ["missing"]


def test_a_pin_pointing_at_the_wrong_channel_is_drift():
    """The pin is the mechanism. A wrong one hands the choice back to Plex."""
    diffs = verify.compare(want=_want(), have=dict(_want(), lineupChannel="id-38-1"))
    assert [d["field"] for d in diffs] == ["lineupChannel"]


def test_a_pin_pointing_at_the_wrong_start_time_is_drift():
    diffs = verify.compare(want=_want(), have=dict(_want(), startTimeslot="2000"))
    assert [d["field"] for d in diffs] == ["startTimeslot"]


def test_a_setting_plex_does_not_report_is_not_counted_as_drift():
    """Not knowing is not the same as disagreeing.

    Calling an absent field drift would repair, find it absent again, and
    repair for ever. The honest answer is that it could not be checked.
    """
    have = {k: v for k, v in _want().items() if k != "endOffsetMinutes"}
    diffs = verify.compare(want=_want(), have=have)
    assert [d["kind"] for d in diffs] == ["unchecked"]
    assert not verify.needs_repair(diffs)


def test_only_a_real_difference_asks_for_a_repair():
    assert verify.needs_repair(verify.compare(want=_want(), have=None))
    assert verify.needs_repair(
        verify.compare(want=_want(), have=dict(_want(), endOffsetMinutes="0")))
    assert not verify.needs_repair(verify.compare(want=_want(), have=_want()))


def test_a_difference_is_described_in_words_a_person_can_act_on():
    diffs = verify.compare(want=_want(), have=dict(_want(), endOffsetMinutes="0"))
    said = verify.describe(diffs)
    assert "endOffsetMinutes" in said and "30" in said and "0" in said


# ---- what a pass wants for one airing ----

def test_what_a_pass_wants_pins_the_airing_it_chose():
    airing = {"channel_identifier": "id-41-1", "begins_at": 1000}
    want = verify.wanted({"endOffsetMinutes": "30"}, airing)
    assert want["oneShot"] == "1"
    assert want["lineupChannel"] == "id-41-1"
    assert want["startTimeslot"] == "1000"
    assert want["endOffsetMinutes"] == "30"


def test_the_pin_wins_over_anything_the_pass_stored():
    """A pass carrying a stale pin must not be able to assert it, and the
    guide can move a game after it was booked."""
    airing = {"channel_identifier": "id-41-1", "begins_at": 1000}
    want = verify.wanted({"lineupChannel": "id-38-1", "startTimeslot": "5"}, airing)
    assert want["lineupChannel"] == "id-41-1"
    assert want["startTimeslot"] == "1000"


# ---- the timing guard ----

def test_a_recording_about_to_start_is_left_alone():
    """Repair cancels and re-books. Doing that minutes before kickoff risks
    losing the recording outright, which is worse than the padding."""
    now = int(time.time())
    assert not verify.can_repair(begins_at=now + 5 * MIN, now=now)


def test_a_recording_far_enough_out_may_be_repaired():
    now = int(time.time())
    assert verify.can_repair(begins_at=now + 6 * HOUR, now=now)


def test_a_recording_already_in_the_past_is_never_touched():
    now = int(time.time())
    assert not verify.can_repair(begins_at=now - HOUR, now=now)


def test_the_guard_leaves_room_for_a_second_attempt():
    """Two sync intervals, so a failed re-book still has a sync left to fix
    it. An hourly sync must not repair ninety minutes before kickoff."""
    assert verify.repair_lead(60) == 2 * HOUR
    assert verify.repair_lead(5) == verify.MINIMUM_LEAD


# ---- against the fake server ----

def _chiefs_pass(prefs):
    team = db.one("SELECT * FROM teams WHERE name LIKE '%Chiefs%'")
    assert team, "the fake guide should carry the Chiefs"
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, networks, channels, "
                  "prefs, uid, enabled, created_at) "
                  "VALUES ('team',?,?,'[]','[]',?,'u1',1,?)",
                  (team["id"], team["name"], db.js(prefs), int(time.time())))
    return db.one("SELECT * FROM passes")


def _push_kickoff(hours=6):
    """Move the live game far enough out that a repair is allowed at all.

    The fake guide sits at most half an hour ahead, which is inside the lead
    `verify.can_repair` insists on. Moving the game is the honest way to reach
    the repair path; lowering the guard would test something the product does
    not do. Done before the pass runs, so the booking pins the moved time.
    """
    when = int(time.time()) + hours * HOUR
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ?, ends_at = ? "
                  "WHERE program_guid = ? AND premiere = 1",
                  (when, when + 2 * HOUR, fake_plex.GAME_GUID))
    return when


def _set_prefs(prefs):
    with db.tx() as c:
        c.execute("UPDATE passes SET prefs = ?", (db.js(prefs),))


def test_a_pass_whose_padding_changed_repairs_its_own_booking(plex, synced):
    """End to end, and exactly the sequence that clipped a real game."""
    _push_kickoff()
    _chiefs_pass({"endOffsetMinutes": "0"})
    passes.run_passes()
    assert db.one("SELECT * FROM our_grabs"), "the pass should have booked the game"

    # The user now sets the padding the pass should have had all along.
    _set_prefs({"endOffsetMinutes": "30"})
    sync.sync_recordings(plex)
    out = sync.check_bookings(plex)
    assert out["repaired"] == 1, out

    key = db.one("SELECT subscription FROM our_grabs")["subscription"]
    got = {s["id"]: s["value"] for s in (plex.subscription(key).get("Setting") or [])}
    assert str(got.get("endOffsetMinutes")) == "30", got


def test_a_booking_that_already_agrees_is_not_touched_again(plex, synced):
    """The loop guard, and the reason `same` is careful.

    A comparison that invented drift would cancel and re-book every recording
    on the server on every sync. Three passes over settled bookings must
    repair nothing.
    """
    _push_kickoff()
    _chiefs_pass({"endOffsetMinutes": "30"})
    passes.run_passes()
    sync.sync_recordings(plex)

    first = sync.check_bookings(plex)
    sync.sync_recordings(plex)
    second = sync.check_bookings(plex)
    third = sync.check_bookings(plex)
    assert first["checked"] == 1, first
    assert second["repaired"] == 0 and second["drifted"] == 0, (first, second)
    assert third["repaired"] == 0 and third["drifted"] == 0, third
    assert fake_plex.STATE.deleted == [], "nothing should have been cancelled"


def test_a_repair_is_written_into_the_pass_history(plex, synced):
    """Say so. A correction you cannot see afterwards is not a correction."""
    _push_kickoff()
    _chiefs_pass({"endOffsetMinutes": "0"})
    passes.run_passes()
    _set_prefs({"endOffsetMinutes": "30"})
    sync.sync_recordings(plex)
    sync.check_bookings(plex)

    row = db.one("SELECT * FROM pass_actions WHERE action = 'repaired'")
    assert row, "a repair must leave a trace"
    assert "endOffsetMinutes" in row["reason"]
    assert row["dry_run"] == 0


def test_a_recording_plex_has_lost_is_booked_again(plex, synced):
    """Plex accepts a subscription and sometimes drops it on its own. That is
    a silent cancellation, and it is what "check the recording exists" means."""
    _push_kickoff()
    _chiefs_pass({})
    passes.run_passes()
    key = db.one("SELECT subscription FROM our_grabs")["subscription"]
    assert key

    plex.delete_subscription(key)          # Plex loses it behind our back
    sync.sync_recordings(plex)

    out = sync.check_bookings(plex)
    assert out["repaired"] == 1, out
    again = db.one("SELECT subscription FROM our_grabs")["subscription"]
    assert again and again != key, "it should now hold a new subscription"


def test_a_subscription_with_nothing_scheduled_against_it_is_repaired(plex, synced):
    """Settings can all agree while Plex has scheduled no recording at all.
    A check that stopped at the settings would call that healthy."""
    _push_kickoff()
    _chiefs_pass({"endOffsetMinutes": "30"})
    passes.run_passes()
    sync.sync_recordings(plex)
    assert db.one("SELECT * FROM plex_grabs")

    with db.tx() as c:                     # Plex is holding the rule and no grab
        c.execute("DELETE FROM plex_grabs")

    out = sync.check_bookings(plex)
    assert out["repaired"] == 1, out


def test_plex_being_unreachable_never_cancels_anything(plex, synced):
    """The fail-open trap. A read that could not be made is not proof the
    recording is gone, and treating it as proof would cancel the lot."""
    _push_kickoff()
    _chiefs_pass({"endOffsetMinutes": "30"})
    passes.run_passes()
    sync.sync_recordings(plex)

    plex.subscription_state = lambda key: ("unknown", None)
    out = sync.check_bookings(plex)
    assert out["unchecked"] == 1, out
    assert out["repaired"] == 0 and out["drifted"] == 0, out
    assert fake_plex.STATE.deleted == []


def test_drift_too_close_to_kickoff_raises_a_notice_instead(plex, synced):
    """Nothing is cancelled minutes before a game. The user is told instead."""
    _chiefs_pass({"endOffsetMinutes": "0"})
    passes.run_passes()
    _set_prefs({"endOffsetMinutes": "30"})
    sync.sync_recordings(plex)
    soon = int(time.time()) + 5 * MIN
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ? WHERE id = "
                  "(SELECT airing_id FROM our_grabs)", (soon,))
        c.execute("UPDATE our_grabs SET begins_at = ?", (soon,))

    out = sync.check_bookings(plex)
    assert out["repaired"] == 0 and out["drifted"] == 1, out
    assert fake_plex.STATE.deleted == [], "a game about to start is not cancelled"
    assert health.BOOKING_DRIFT in {n["code"] for n in health.open_notices()}


def test_the_check_clears_its_own_notice_once_everything_agrees(plex, synced):
    """A notice raised by a check is cleared by the same check passing."""
    now = int(time.time())
    health.record([{"code": health.BOOKING_DRIFT, "severity": "bad", "title": "x",
                    "detail": "y", "hint": "z"}], now, owns=health.BOOKING_CODES)
    assert health.BOOKING_DRIFT in {n["code"] for n in health.open_notices()}

    sync.check_bookings(plex)              # nothing booked, so nothing is wrong
    assert health.BOOKING_DRIFT not in {n["code"] for n in health.open_notices()}


def test_the_check_does_not_close_a_finding_it_never_looked_at(plex, synced):
    """Each sweep owns its own codes. This one must not resolve the guide
    warnings, which it knows nothing about."""
    now = int(time.time())
    health.record([{"code": health.EPG_STALE, "severity": "bad", "title": "x",
                    "detail": "y", "hint": "z"}], now, owns=health.PLEX_CODES)
    sync.check_bookings(plex)
    assert health.EPG_STALE in {n["code"] for n in health.open_notices()}


def test_a_booking_whose_pass_was_deleted_is_left_alone(plex, synced):
    """A recording whose pass is gone is the user's, not ours to rewrite."""
    _chiefs_pass({"endOffsetMinutes": "30"})
    passes.run_passes()
    with db.tx() as c:
        c.execute("DELETE FROM passes")
    assert sync.check_bookings(plex)["checked"] == 0


def test_a_recording_booked_by_hand_is_never_repaired(plex, synced):
    """Booked from the panel, so its settings are the user's own choice."""
    row = db.one("SELECT a.*, p.rating_key, p.title FROM airings a "
                 "JOIN programs p ON p.guid = a.program_guid "
                 "WHERE a.begins_at > ? AND COALESCE(a.drm,0) = 0 LIMIT 1",
                 (int(time.time()),))
    passes._schedule(plex, row, None, "manual")
    assert db.one("SELECT * FROM our_grabs")["source"] == "manual"
    assert sync.check_bookings(plex)["checked"] == 0


def test_a_past_booking_is_not_checked(plex, synced):
    """Yesterday's recording cannot be put right and must not be touched."""
    _chiefs_pass({"endOffsetMinutes": "30"})
    passes.run_passes()
    past = int(time.time()) - 2 * HOUR
    with db.tx() as c:
        c.execute("UPDATE airings SET begins_at = ? WHERE id = "
                  "(SELECT airing_id FROM our_grabs)", (past,))
        c.execute("UPDATE our_grabs SET begins_at = ?", (past,))
    assert sync.check_bookings(plex)["checked"] == 0


def test_a_repair_plex_refuses_becomes_a_notice(plex, synced):
    """A failed re-book must shout. Silently leaving nothing scheduled is the
    one outcome worse than the drift it was fixing."""
    _push_kickoff()
    _chiefs_pass({"endOffsetMinutes": "0"})
    passes.run_passes()
    _set_prefs({"endOffsetMinutes": "30"})
    sync.sync_recordings(plex)

    def refuse(*a, **k):
        raise RuntimeError("Plex said no")
    passes._schedule, original = refuse, passes._schedule
    try:
        out = sync.check_bookings(plex)
    finally:
        passes._schedule = original

    assert out["failed"] == 1, out
    assert health.BOOKING_REPAIR_FAILED in {n["code"] for n in health.open_notices()}


def test_a_full_sync_runs_the_check(plex, synced):
    """It has to be on the sync loop, or it is a button nobody presses."""
    _chiefs_pass({"endOffsetMinutes": "0"})
    passes.run_passes()
    _set_prefs({"endOffsetMinutes": "30"})

    ok, detail = sync.full_sync()
    assert ok, detail
    # A full sync re-pulls the guide, which puts kickoff back inside the
    # repair guard, so the check reports rather than repairs. Either way it
    # ran, which is what this is here to prove.
    assert "adrift" in detail, detail


def test_fake_plex_still_answers_one_shot_as_the_string_true(plex, synced):
    """The quirk the comparison exists for. If the fake stops reproducing it,
    the loop guard above stops proving anything."""
    row = db.one("SELECT a.*, p.rating_key, p.title FROM airings a "
                 "JOIN programs p ON p.guid = a.program_guid "
                 "WHERE a.begins_at > ? AND COALESCE(a.drm,0) = 0 LIMIT 1",
                 (int(time.time()),))
    passes._schedule(plex, row, None, "manual")
    key = db.one("SELECT subscription FROM our_grabs")["subscription"]
    got = {s["id"]: s["value"] for s in (plex.subscription(key).get("Setting") or [])}
    assert str(got.get("oneShot")).lower() == "true", got
