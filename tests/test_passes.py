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


def test_a_pass_that_already_scheduled_a_game_says_so():
    g = "plex://episode/g"
    _airing("a1", g, "41.1", 1000, premiere=1)
    with db.tx() as c:
        c.execute("""INSERT INTO pass_actions (pass_id, program_guid, action,
                                               dry_run, created_at)
                     VALUES (1, ?, 'scheduled', 0, 0)""", (g,))
    assert "already scheduled by a pass" == passes.already_handled(g)


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
                  "VALUES ('team', 236, 'Chiefs', 1, 0)")
    out = passes.run_passes()
    assert [d["action"] for d in out] == ["would schedule"]
    assert fake_plex.STATE.created == []
    assert not db.query("SELECT 1 FROM our_grabs")


def test_a_pass_limited_to_a_network_with_no_airing_says_which(plex, synced):
    with db.tx() as c:
        c.execute("""INSERT INTO passes (kind, team_id, team_name, networks, channels,
                                         enabled, created_at)
                     VALUES ('team', 236, 'Chiefs', '["ABC","CBS"]', '[]', 1, 0)""")
    out = passes.run_passes()
    assert out[0]["action"] == "skipped"
    assert out[0]["reason"] == "no airing is on ABC or CBS"
    assert fake_plex.STATE.created == []
