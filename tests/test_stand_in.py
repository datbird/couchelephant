"""A game the guide lists as a bare "NFL Football" is still booked.

A team pass matches games by the teams a programme carries. Gracenote lists a
regional game as a generic "NFL Football" until the broadcaster assigns it,
and on 2026-09-27 the CBS slots for the next Chiefs game were still generic a
week out. Nothing matched, so nothing booked, and a game that stayed generic
until kickoff would never have recorded.

The league's schedule says when the game kicks off. A generic slot of the same
league at that time is the game, and it is booked as a STAND-IN. When the
guide names the game, the booking moves onto the named listing, so Plex shows
the real title as well.
"""
import time

from app import db, expectations, passes, sync
from tests import fake_plex

HOUR = 3600
GENERIC = "plex://episode/generic-cbs-late"
NAMED = "plex://episode/raiders-chiefs"
MATCHUP = "Las Vegas Raiders vs Kansas City Chiefs"


def _kickoff(days=5):
    """A kickoff well outside the repair lead, on a half hour like a real one."""
    return (int(time.time()) // 1800) * 1800 + days * 86400 + 1500


def _chiefs_pass(networks=(), channels=()):
    team = db.one("SELECT * FROM teams WHERE name LIKE '%Chiefs%'")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, networks, channels, "
                  "prefs, uid, enabled, created_at) "
                  "VALUES ('team',?,?,?,?,'{}','u1',1,?)",
                  (team["id"], team["name"], db.js(list(networks)),
                   db.js(list(channels)), int(time.time())))
    return db.one("SELECT * FROM passes")


def _expect(pass_id, when, precision="time", network=None, source_id="2475433"):
    with db.tx() as c:
        c.execute(
            "INSERT INTO expectations (pass_id, source, source_id, title, subtitle, "
            "network, expected_at, precision, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (pass_id, "thesportsdb", source_id, "Kansas City Chiefs", MATCHUP,
             network, when, precision, int(time.time())))
    return db.one("SELECT * FROM expectations WHERE source_id = ?", (source_id,))


def _read_the_guide(plex):
    """Pull the guide and Plex's schedule in, and bind what it now names."""
    provider, shows, sports, movies = sync.discover(plex)
    sync.sync_channels(plex)
    sync.sync_guide(plex, provider, shows, sports, movies)
    sync.enrich_sports(plex, provider)
    sync.sync_recordings(plex)
    expectations.promote()


def _ours():
    """Our booking for this game. The fake guide's own Chiefs game is booked
    by the same pass, and is not what these tests are about."""
    return db.one("SELECT * FROM our_grabs WHERE program_guid != ?",
                  (fake_plex.GAME_GUID,))


def _booked_now():
    passes.run_passes()
    return db.one("SELECT * FROM our_grabs WHERE expectation_id IS NOT NULL")


def test_a_generic_slot_at_the_kickoff_is_booked(synced, plex):
    when = _kickoff()
    p = _chiefs_pass()
    item = _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)

    row = _booked_now()
    assert row, "the generic slot should have been booked"
    assert row["expectation_id"] == item["id"]
    assert row["program_guid"] == GENERIC
    made = fake_plex.STATE.created[-1]
    assert made["guid"] == GENERIC
    assert made["prefs"]["startTimeslot"] == str(when)
    assert made["prefs"]["lineupChannel"] == "id-5-1"
    log = db.one("SELECT * FROM pass_actions WHERE action = 'scheduled' "
                 "ORDER BY id DESC LIMIT 1")
    assert "stand-in for " + MATCHUP in log["reason"]


def test_a_second_run_does_not_book_it_twice(synced, plex):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)
    passes.run_passes()
    sync.sync_recordings(plex)
    passes.run_passes()
    assert len([c for c in fake_plex.STATE.created if c["guid"] == GENERIC]) == 1


def test_the_schedule_shows_the_matchup_and_why(synced, plex, client):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)
    _booked_now()
    sync.sync_recordings(plex)

    rows = client.get("/api/schedule?limit=100").json()["rows"]
    row = next(r for r in rows if r["b"] == when)
    assert row["title"] == MATCHUP
    assert row["listed_as"] == "NFL Football"
    assert "has not named the teams yet" in row["stand_in_note"]
    assert row["who"] == "ce"
    # Booked, so no longer waiting. Listing it there too would draw it twice.
    waiting = client.get("/api/expectations").json()["rows"]
    assert not [e for e in waiting if e["subtitle"] == MATCHUP]


def test_once_named_the_booking_moves_onto_the_named_listing(synced, plex, client):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)
    stand_in = _booked_now()
    sync.sync_recordings(plex)

    fake_plex.name_the_game(GENERIC, NAMED, "Kansas City Chiefs at Las Vegas Raiders",
                            ["Kansas City Chiefs", "Las Vegas Raiders"])
    _read_the_guide(plex)
    out = sync.check_bookings(plex)

    assert out["repaired"] == 1, out
    assert stand_in["subscription"] in fake_plex.STATE.deleted or \
        stand_in["subscription"] not in fake_plex.STATE.subscriptions
    now_booked = _ours()
    assert now_booked["program_guid"] == NAMED
    assert now_booked["expectation_id"] is None
    assert fake_plex.STATE.created[-1]["guid"] == NAMED
    assert len(db.query("SELECT 1 FROM our_grabs WHERE program_guid != ?",
                        (fake_plex.GAME_GUID,))) == 1

    # And the pass does not book the named game a second time.
    passes.run_passes()
    assert len([c for c in fake_plex.STATE.created if c["guid"] == NAMED]) == 1

    rows = client.get("/api/schedule?limit=100").json()["rows"]
    row = next(r for r in rows if r["b"] == when)
    assert row["title"] == "Kansas City Chiefs at Las Vegas Raiders"
    assert "stand_in_note" not in row


def test_named_in_another_slot_moves_there(synced, plex):
    """The guide put the game on another channel. The stand-in was some
    other game, so it goes, whatever the time."""
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)
    _booked_now()
    sync.sync_recordings(plex)

    fake_plex.name_the_game(GENERIC, NAMED, "Kansas City Chiefs at Las Vegas Raiders",
                            ["Kansas City Chiefs", "Las Vegas Raiders"], vcn="9.1")
    _read_the_guide(plex)
    sync.check_bookings(plex)

    row = _ours()
    assert row["program_guid"] == NAMED
    assert row["channel_vcn"] == "9.1"


def test_too_close_and_recording_keeps_plexs_recording(synced, plex, monkeypatch):
    """Named in the very slot, Plex recording it, kickoff too close to book
    again. The recording is kept and only our record is re-pointed."""
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)
    _booked_now()
    sync.sync_recordings(plex)
    made = len(fake_plex.STATE.created)

    fake_plex.name_the_game(GENERIC, NAMED, "Kansas City Chiefs at Las Vegas Raiders",
                            ["Kansas City Chiefs", "Las Vegas Raiders"])
    _read_the_guide(plex)
    monkeypatch.setattr(sync.verify, "can_repair", lambda **kw: False)
    monkeypatch.setattr(sync, "_has_grab", lambda *a: True)
    sync.check_bookings(plex)

    assert len(fake_plex.STATE.created) == made, "nothing may be booked again"
    assert not fake_plex.STATE.deleted
    row = _ours()
    assert row["program_guid"] == NAMED
    assert row["expectation_id"] is None


def test_two_generic_slots_at_one_kickoff_book_neither(synced, plex):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    fake_plex.generic_slot(GENERIC + "-2", "9.1", when)
    _read_the_guide(plex)
    assert _booked_now() is None


def test_the_pass_source_limit_decides_between_them(synced, plex):
    when = _kickoff()
    p = _chiefs_pass(channels=["5.1"])
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when)
    fake_plex.generic_slot(GENERIC + "-2", "9.1", when)
    _read_the_guide(plex)
    row = _booked_now()
    assert row and row["channel_vcn"] == "5.1"


def test_a_kickoff_with_no_time_is_not_a_slot(synced, plex):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when, precision="day")
    fake_plex.generic_slot(GENERIC, "5.1", when)
    _read_the_guide(plex)
    assert _booked_now() is None


def test_a_slot_far_from_the_kickoff_is_not_the_game(synced, plex):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when)
    fake_plex.generic_slot(GENERIC, "5.1", when + 3 * HOUR)
    _read_the_guide(plex)
    assert _booked_now() is None


def test_a_studio_show_at_the_kickoff_is_not_the_game(synced, plex):
    """A league with no tagged game anywhere is a shop or a phone-in."""
    when = fake_plex.NO_TEAM_AT
    p = _chiefs_pass()
    _expect(p["id"], when)
    _read_the_guide(plex)
    assert _booked_now() is None


def test_a_named_network_elsewhere_rules_the_slot_out(synced, plex):
    when = _kickoff()
    p = _chiefs_pass()
    _expect(p["id"], when, network="NBC")
    fake_plex.generic_slot(GENERIC, "5.1", when)       # a CBS channel
    _read_the_guide(plex)
    assert _booked_now() is None
