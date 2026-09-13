"""The selection logic. This is the product, so it gets the most tests."""
import json
import time

from app import db, passes
from tests import fake_plex


def _airing(aid, guid, vcn, begins, premiere=0, drm=0, title="Game"):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  (guid, title, "rk", json.dumps([{"id": 236, "name": "Chiefs"}]),
                   "NFL Football", "sports"))
        c.execute("INSERT OR IGNORE INTO channels (vcn, network, call_sign) VALUES (?,?,?)",
                  (vcn, {"41.1": "NBC", "38.1": "Independent",
                         "9.1": "ABC", "5.1": "CBS"}.get(vcn), vcn))
        c.execute("""INSERT OR REPLACE INTO airings
                     (id, program_guid, channel_vcn, channel_identifier, begins_at,
                      ends_at, premiere, drm) VALUES (?,?,?,?,?,?,?,?)""",
                  (aid, guid, vcn, f"id-{vcn}", begins, begins + 3600, premiere, drm))


def _rows(guid):
    return db.query("""SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams,
                              p.summary, c.network AS channel_network
                       FROM airings a JOIN programs p ON p.guid = a.program_guid
                       LEFT JOIN channels c ON c.vcn = a.channel_vcn
                       WHERE a.program_guid = ? ORDER BY a.begins_at""", (guid,))


def test_prefers_the_premiere_over_an_earlier_repeat():
    """The whole point. Plex breaks the tie on channel number and gets this
    wrong; a repeat that airs FIRST must still lose to the flagged live one."""
    g = "plex://episode/g"
    _airing("a1", g, "38.1", 1000, premiere=0)     # earlier, but a repeat
    _airing("a2", g, "41.1", 2000, premiere=1)     # later, and live
    pick, why = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2"
    assert "premiere" in why


def test_takes_the_earliest_premiere_when_several_are_flagged():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 3000, premiere=1)
    _airing("a2", g, "38.1", 2000, premiere=1)
    pick, _ = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2"


def test_falls_back_to_the_earliest_and_says_so():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 3000)
    _airing("a2", g, "38.1", 2000)
    pick, why = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2"
    assert "no airing is flagged premiere" in why


def test_drm_airings_are_never_chosen():
    g = "plex://episode/g"
    _airing("a1", g, "5.1", 1000, premiere=1, drm=1)
    _airing("a2", g, "41.1", 2000)
    pick, _ = passes.choose_airing(_rows(g))
    assert pick["id"] == "a2", "a DRM premiere must lose to a recordable repeat"


def test_all_drm_is_refused_with_a_reason():
    g = "plex://episode/g"
    _airing("a1", g, "5.1", 1000, premiere=1, drm=1)
    pick, why = passes.choose_airing(_rows(g))
    assert pick is None
    assert "DRM" in why


# ---- source limits ----

def _rule(networks=None, channels=None):
    return {"networks": json.dumps(networks or []),
            "channels": json.dumps(channels or []),
            "kind": "team", "team_id": 236, "series_guid": None,
            "series_title": None}


def test_no_limit_allows_anywhere():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000)
    nets, chans = passes.allowed_sources(_rule())
    assert passes.in_sources(_rows(g)[0], nets, chans)


def test_a_network_limit_admits_only_that_network():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000)      # NBC
    _airing("a2", g, "9.1", 2000)       # ABC
    nets, chans = passes.allowed_sources(_rule(networks=["ABC"]))
    rows = _rows(g)
    assert not passes.in_sources(rows[0], nets, chans)
    assert passes.in_sources(rows[1], nets, chans)


def test_networks_and_channels_are_one_allowlist_not_two_filters():
    """Naming a network and a channel means either of them."""
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000)      # NBC, named by channel
    _airing("a2", g, "9.1", 2000)       # ABC, named by network
    _airing("a3", g, "5.1", 3000)       # CBS, named by neither
    nets, chans = passes.allowed_sources(_rule(networks=["ABC"], channels=["41.1"]))
    got = [r["id"] for r in _rows(g) if passes.in_sources(r, nets, chans)]
    assert got == ["a1", "a2"]


def test_the_limit_is_applied_before_the_choice():
    """Filtering after choosing would pick the live airing and then find it
    disallowed, and book nothing. The best ALLOWED airing must win."""
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)    # live, but on NBC
    _airing("a2", g, "9.1", 5000)                 # a repeat, on ABC
    nets, chans = passes.allowed_sources(_rule(networks=["ABC"]))
    allowed = [a for a in _rows(g) if passes.in_sources(a, nets, chans)]
    pick, _ = passes.choose_airing(allowed)
    assert pick["id"] == "a2"


# ---- already handled ----

def test_a_repeated_title_does_not_block_a_later_broadcast():
    """B6. A daily programme keeps its title, so matching on title alone made
    every future airing read as already recorded."""
    g1, g2 = "plex://episode/day1", "plex://episode/day2"
    _airing("a1", g1, "9.1", 1000, title="Quiz Night")
    _airing("a2", g2, "9.1", 90000, title="Quiz Night")
    with db.tx() as c:
        c.execute("""INSERT INTO plex_grabs (id, status, title, channel_vcn,
                                             begins_at, updated_at)
                     VALUES ('g1','complete','Quiz Night','9.1',1000,0)""")
    assert passes.already_handled(g1), "the broadcast that was recorded is covered"
    assert passes.already_handled(g2) is None, "tomorrow's is not"


def test_a_pass_that_already_booked_a_game_says_so():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO our_grabs (airing_id, program_guid, title,
                                            channel_vcn, begins_at, source,
                                            subscription, created_at)
                     VALUES ('a1',?,'Game','41.1',1000,'pass','7',0)""", (g,))
        c.execute("""INSERT INTO plex_subscriptions (key, title, type, created_at,
                                                     updated_at, owned_by_us)
                     VALUES ('7','This Event','4',0,0,1)""")
    assert "already booked by a pass" == passes.already_handled(g)


def test_having_once_scheduled_a_game_is_not_a_reason_to_ignore_it():
    """The log is not the live state, and reading it as one loses recordings.

    A booking can be lost after it is made: Plex drops a subscription on its
    own, the user cancels one, or the guide re-times the broadcast and the
    pinned recording stops existing. This used to answer "already scheduled"
    to all three, for the life of the install, so the pass never looked at the
    game again and nothing recorded it.
    """
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO sync_log (started_at, ended_at, ok)
                     VALUES (500, 500, 1)""")
        c.execute("""INSERT INTO pass_actions (pass_id, program_guid, action,
                                               dry_run, created_at)
                     VALUES (1, ?, 'scheduled', 0, 100)""", (g,))
    assert passes.already_handled(g) is None


def test_a_booking_made_since_the_last_sync_still_counts():
    """The other half of it. Plex is only read back by a sync, so between
    booking a game and the next sync nothing else knows the recording exists.
    Without this, two runs of a pass in a row would book it twice.
    """
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO sync_log (started_at, ended_at, ok)
                     VALUES (500, 500, 1)""")
        c.execute("""INSERT INTO our_grabs (airing_id, program_guid, title,
                                            channel_vcn, begins_at, source,
                                            subscription, created_at)
                     VALUES ('a1',?,'Game','41.1',1000,'pass','7',900)""", (g,))
    assert "already booked by a pass" == passes.already_handled(g)


# ---- booking ----

def test_a_booking_is_pinned_to_the_chosen_broadcast(plex, synced):
    """Without the pin Plex re-chooses, which is the bug the app exists for."""
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "test")
    made = fake_plex.STATE.created[-1]["prefs"]
    assert made["oneShot"] == "1"
    assert made["startTimeslot"] == str(pick["begins_at"])
    assert made["lineupChannel"] == pick["channel_identifier"]


def test_a_user_cannot_unpin_a_pass_booking(plex, synced):
    """The three pinning prefs are not the caller's to override."""
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "test",
                     prefs={"oneShot": "0", "startTimeslot": "-1",
                            "lineupChannel": "", "startOffsetMinutes": "5"})
    made = fake_plex.STATE.created[-1]["prefs"]
    assert made["oneShot"] == "1"
    assert made["startTimeslot"] == str(pick["begins_at"])
    assert made["startOffsetMinutes"] == "5", "other settings still apply"


def test_a_discarded_create_is_reported_as_a_failure(plex, synced):
    """Reporting a recording Plex did not keep is worse than failing."""
    import pytest

    from app.plex import PlexError
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    fake_plex.STATE.drop_next_create = True
    with pytest.raises(PlexError, match="discarded"):
        passes._schedule(plex, pick, None, "test")
    assert not db.query("SELECT 1 FROM our_grabs"), "nothing may be remembered"


def test_run_passes_books_the_live_broadcast(plex, synced):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 236, 'Kansas City Chiefs', 1, ?)", (int(time.time()),))
    out = passes.run_passes()
    booked = [d for d in out if d["action"] == "scheduled"]
    assert len(booked) == 1
    assert booked[0]["channel"] == "41.1", "the live channel, not the repeat"
    grab = db.one("SELECT * FROM our_grabs")
    assert grab["begins_at"] == fake_plex.LIVE_AT


def test_preview_mode_writes_nothing_to_plex(plex, synced):
    db.set_setting("dry_run", "1")
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', 236, 'Kansas City Chiefs', 1, 0)")
    out = passes.run_passes()
    assert [d["action"] for d in out] == ["would schedule"]
    assert fake_plex.STATE.created == []
    assert not db.query("SELECT 1 FROM our_grabs")


def test_a_pass_limited_to_a_network_with_no_airing_says_which(plex, synced):
    with db.tx() as c:
        c.execute("""INSERT INTO passes (kind, team_id, team_name, networks, channels,
                                         enabled, created_at)
                     VALUES ('team', 236, 'Kansas City Chiefs', '["ABC","CBS"]', '[]', 1, 0)""")
    out = passes.run_passes()
    assert out[0]["action"] == "skipped"
    assert out[0]["reason"] == "no airing is on ABC or CBS"
    assert fake_plex.STATE.created == []


def test_a_pass_setting_the_one_shot_template_does_not_offer_is_dropped(plex, synced):
    """The live fault, 2026-09-10: 364 failed bookings and no Chiefs game.

    A pass created before the UI hid them kept `onlyNewAirings` in its stored
    settings. A pass always books the one-shot template, which does not offer
    that setting, so Plex answered 400 to every attempt for three weeks. The
    booking must survive a stored setting this template will not take.
    """
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "test",
                     prefs={"onlyNewAirings": "1", "startOffsetMinutes": "1",
                            "autoDeletionItemPolicyWatchedLibrary": "0"})
    made = fake_plex.STATE.created[-1]["prefs"]
    assert "onlyNewAirings" not in made
    assert "autoDeletionItemPolicyWatchedLibrary" not in made
    assert made["startOffsetMinutes"] == "1", "a setting it does offer still applies"
    assert made["oneShot"] == "1", "the pin survives the filter"


def test_the_pin_survives_a_template_that_declares_nothing(plex, synced):
    """No Setting list means an unknown server, not an empty allowlist.

    Filtering against nothing would drop the three pinning settings, and the
    pin is the mechanism the whole app exists for.
    """
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    bare = dict(passes.single_template(passes.templates(plex, pick)))
    bare.pop("Setting", None)
    passes._schedule(plex, pick, None, "test", template=bare,
                     prefs={"startOffsetMinutes": "1"})
    made = fake_plex.STATE.created[-1]["prefs"]
    assert made["oneShot"] == "1"
    assert made["startTimeslot"] == str(pick["begins_at"])
    assert made["startOffsetMinutes"] == "1"


def _airing_with_teams(aid, guid, vcn, begins, teams, title, league):
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  (guid, title, "rk", json.dumps(teams), league, "sports"))
        c.execute("INSERT OR IGNORE INTO channels (vcn, network, call_sign) VALUES (?,?,?)",
                  (vcn, "NBC", vcn))
        c.execute("""INSERT OR REPLACE INTO airings
                     (id, program_guid, channel_vcn, channel_identifier, begins_at,
                      ends_at, premiere, drm) VALUES (?,?,?,?,?,?,1,0)""",
                  (aid, guid, vcn, f"id-{vcn}", begins, begins + 3600))


def test_a_team_id_is_only_meaningful_inside_its_own_programme():
    """The live fault, 2026-09-10: a Kansas City Chiefs pass matched Borussia
    Dortmund, Arizona State, Washburn and a Chicago Bears game.

    A pass holds an id from Plex's section-level team list. A programme holds
    its own array, numbered per programme. Of the 89 ids that appeared in both
    on the live guide, 71 named a different team in each. Inside the programme
    arrays alone, id 343 was Kansas City Chiefs, Borussia Dortmund, Los Angeles
    Rams AND San Diego State. Matching on the number books whatever sport
    happens to share it.
    """
    soon = int(time.time()) + 3600
    _airing_with_teams("chiefs", "plex://episode/chiefs", "41.1", soon,
                       [{"id": 343, "name": "Kansas City Chiefs"},
                        {"id": 349, "name": "Denver Broncos"}],
                       "Denver Broncos at Kansas City Chiefs", "NFL Football")
    _airing_with_teams("bvb", "plex://episode/bvb", "38.1", soon + 60,
                       [{"id": 343, "name": "Borussia Dortmund"},
                        {"id": 344, "name": "SC Paderborn"}],
                       "Borussia Dortmund vs. Paderborn 07", "Bundesliga")

    got = passes.candidate_airings(343, team_name="Kansas City Chiefs")
    titles = {r["title"] for r in got}
    assert "Denver Broncos at Kansas City Chiefs" in titles
    assert "Borussia Dortmund vs. Paderborn 07" not in titles, \
        "a shared id is not a shared team"


def test_a_team_pass_still_matches_after_the_guide_renumbers_it():
    """The reason the id was in the match at all. The name has to carry it,
    because the id moves: one refresh took the Chiefs from 236 to 245 on the
    same game with the same guid."""
    soon = int(time.time()) + 3600
    _airing_with_teams("renum", "plex://episode/renum", "41.1", soon,
                       [{"id": 999, "name": "Kansas City Chiefs"},
                        {"id": 998, "name": "Denver Broncos"}],
                       "Denver Broncos at Kansas City Chiefs", "NFL Football")
    got = passes.candidate_airings(236, team_name="Kansas City Chiefs")
    assert [r["id"] for r in got] == ["renum"], "found by name, whatever the id"


def test_a_pass_with_no_name_at_all_still_falls_back_to_the_id():
    """An old pass made before the name was stored has nothing else to go on."""
    soon = int(time.time()) + 3600
    _airing_with_teams("byid", "plex://episode/byid", "41.1", soon,
                       [{"id": 4242, "name": "Kansas City Chiefs"}],
                       "Chiefs at Somebody", "NFL Football")
    assert [r["id"] for r in passes.candidate_airings(4242)] == ["byid"]
