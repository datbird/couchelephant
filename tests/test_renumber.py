"""Plex renumbers its team ids, and a pass must survive it.

Measured on a live server: one guide refresh moved the Kansas City Chiefs from
236 to 245 and the Seattle Seahawks from 132 to 244, on the same game, with the
same programme guid. A pass that follows the old number matches nothing, and
"nothing matched" is a normal quiet outcome rather than an error, so it says
nothing about it either. You find out on a Sunday.

The id is a handle into whatever guide Plex is holding. The name is the
identity.
"""
import json
import time

from app import db, health, passes, sync


def _team_pass(team_id, name="Kansas City Chiefs"):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at, "
                  "networks, channels, uid) VALUES ('team',?,?,1,?,'[]','[]',?)",
                  (team_id, name, int(time.time()), f"uid-{team_id}-{name}"))
    return db.one("SELECT * FROM passes ORDER BY id DESC LIMIT 1")


def _game(team_id, name="Kansas City Chiefs", guid="plex://episode/g1"):
    """A cached programme carrying whatever ids Plex used at the time."""
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  (guid, "Seahawks at Chiefs", "rk",
                   json.dumps([{"id": team_id, "name": name}]),
                   "NFL Football", "sports"))
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, channel_vcn, "
                  "channel_identifier, begins_at, ends_at, premiere) "
                  "VALUES (?,?,?,?,?,?,1)",
                  ("a1", guid, "41.1", "id-41.1", now + 3600, now + 7200))


# ---- matching an airing ----

def test_a_pass_still_finds_its_team_after_plex_renumbers(clean_db):
    """The bug, at its smallest. The pass was made when the Chiefs were 236.
    Plex now calls them 245, on the very same game."""
    _team_pass(236)
    _game(245)
    assert passes.candidate_airings(236, team_name="Kansas City Chiefs")


def test_a_stale_cached_programme_still_matches(clean_db):
    """The other direction. `programs.teams` is only enriched once, so a cached
    row keeps the old ids long after Plex has moved on."""
    _team_pass(245)
    _game(236)
    assert passes.candidate_airings(245, team_name="Kansas City Chiefs")


def test_an_accent_is_not_a_different_team(clean_db):
    """The folding that airing matching does allow: spelling, not identity."""
    _game(999, name="FC Bayern München")
    assert passes.candidate_airings(1, team_name="FC Bayern Munchen")


def test_a_different_team_is_not_matched(clean_db):
    """The point of the id was precision. Falling back to the name must not
    turn the Chiefs into the Kansas City Current."""
    _game(245, name="Kansas City Current")
    assert not passes.candidate_airings(236, team_name="Kansas City Chiefs")


def test_an_id_match_still_works_without_a_name(clean_db):
    """Nothing that worked before may stop working."""
    _game(236)
    assert passes.candidate_airings(236)


# ---- correcting the stored id ----

def test_the_stored_id_is_corrected_when_plex_renumbers(clean_db):
    """`resolve_team_passes` only ever filled a NULL id, so a pass that already
    had one kept a dead number for ever."""
    p = _team_pass(236)
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name, in_guide, last_seen, updated_at) "
                  "VALUES (245,'Kansas City Chiefs',1,?,?)", (now, now))
    assert sync.resolve_team_passes() == 1
    assert db.one("SELECT team_id FROM passes WHERE id = ?", (p["id"],))["team_id"] == 245


def test_a_team_that_left_the_guide_does_not_steal_the_id(clean_db):
    """After a renumber both rows exist: the old id and the new one, same name.
    Only the one Plex currently knows may win."""
    p = _team_pass(None)
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name, in_guide, last_seen, updated_at) "
                  "VALUES (236,'Kansas City Chiefs',0,?,?)", (now, now))
        c.execute("INSERT INTO teams (id, name, in_guide, last_seen, updated_at) "
                  "VALUES (245,'Kansas City Chiefs',1,?,?)", (now, now))
    sync.resolve_team_passes()
    assert db.one("SELECT team_id FROM passes WHERE id = ?", (p["id"],))["team_id"] == 245


def test_a_correct_id_is_left_alone(clean_db):
    _team_pass(245)
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name, in_guide, last_seen, updated_at) "
                  "VALUES (245,'Kansas City Chiefs',1,?,?)", (now, now))
    assert sync.resolve_team_passes() == 0


def test_a_team_out_of_season_keeps_its_pass_waiting(clean_db):
    """Not in the guide is not the same as renumbered. The pass waits, and its
    id is not blanked on the way past."""
    p = _team_pass(236)
    sync.resolve_team_passes()
    assert db.one("SELECT team_id FROM passes WHERE id = ?", (p["id"],))["team_id"] == 236


# ---- and it says so out loud ----

def test_a_pass_that_can_find_no_game_is_not_silent(clean_db):
    """The failure this whole class of bug hides behind. A pass matching
    nothing looks exactly like a team with no games this week."""
    _team_pass(236, name="Kansas City Chiefs")
    _game(245, name="Kansas City Current")      # someone else entirely
    sync.check_team_passes()
    assert health.TEAM_PASS_UNMATCHED in {n["code"] for n in health.open_notices()}


def test_the_notice_clears_once_the_team_is_back(clean_db):
    _team_pass(236, name="Kansas City Chiefs")
    _game(245, name="Kansas City Current")
    sync.check_team_passes()
    assert health.open_notices()
    _game(245, name="Kansas City Chiefs", guid="plex://episode/g2")
    sync.check_team_passes()
    assert health.TEAM_PASS_UNMATCHED not in {n["code"] for n in health.open_notices()}


def test_the_plex_sweep_does_not_close_the_team_finding(clean_db):
    """Two sweeps run each sync. The Plex one used to resolve every notice it
    was not handed, so it closed the team finding, the team sweep reopened it,
    and the age of the problem reset every hour."""
    now = int(time.time())
    health.record([{"code": health.TEAM_PASS_UNMATCHED, "severity": "warn",
                    "title": "t", "detail": "d", "hint": None}],
                  now - 3 * 86400, owns=health.TEAM_CODES)
    # A clean bill of health from the Plex checks, several syncs running.
    for i in range(3):
        health.record([], now + i, owns=health.PLEX_CODES)
    open_ = health.open_notices()
    assert [n["code"] for n in open_] == [health.TEAM_PASS_UNMATCHED]
    assert open_[0]["first_seen"] == now - 3 * 86400, "and it keeps its age"


# ---- the name must not be looser than the team ----

def test_real_madrid_does_not_match_atletico_madrid(clean_db):
    """`teamcat.norm` strips club words, so "Real Madrid" and "Atletico Madrid"
    both fold to "madrid". That folding is right for finding a team in the
    catalogue and wrong for deciding what to record."""
    _game(500, name="Atletico Madrid")
    assert not passes.candidate_airings(1, team_name="Real Madrid")


def test_a_college_side_does_not_match_the_club_of_the_same_city(clean_db):
    """Five real pairs in the shipped catalogue fold together: Cincinnati and
    FC Cincinnati, Charlotte and Charlotte FC, and three more."""
    _game(501, name="FC Cincinnati")
    assert not passes.candidate_airings(1, team_name="Cincinnati")


def test_a_pass_adopts_plex_spelling_so_the_names_can_be_compared_strictly(clean_db):
    """Where the loose fold belongs: finding the team once, then writing down
    what Plex calls it. After this the pass and the guide agree word for word,
    and matching never has to guess again."""
    p = _team_pass(None, name="Bayern Munchen")
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name, in_guide, last_seen, updated_at) "
                  "VALUES (243,'FC Bayern Munchen',1,?,?)", (now, now))
    sync.resolve_team_passes()
    row = db.one("SELECT team_id, team_name FROM passes WHERE id = ?", (p["id"],))
    assert row["team_id"] == 243
    assert row["team_name"] == "FC Bayern Munchen"

    _game(243, name="FC Bayern Munchen")
    assert passes.candidate_airings(row["team_id"], team_name=row["team_name"])
