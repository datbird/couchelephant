"""The shipped team catalogue, and how it meets Plex's own list."""
import importlib.util
import os

from app import db, sync, teamcat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _builder():
    spec = importlib.util.spec_from_file_location(
        "build_teams", os.path.join(ROOT, "scripts", "build_teams.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ---- the catalogue itself ----

def test_the_catalogue_is_big_enough_to_be_worth_shipping():
    teams = teamcat.all_teams()
    assert len(teams) > 1000, "the point of it is that Plex's 76 is not enough"


def test_it_covers_the_leagues_a_person_would_expect():
    by_league = {}
    for t in teamcat.all_teams():
        by_league.setdefault(t["league"], []).append(t)
    for league, least in (("NFL", 32), ("NBA", 30), ("MLB", 30), ("NHL", 32),
                          ("NCAA", 800), ("MLS", 29), ("WNBA", 12)):
        assert len(by_league.get(league, [])) >= least, f"{league} looks short"


def test_named_teams_are_all_there():
    for name in ("Kansas City Chiefs", "Kansas City Royals", "Kansas", "Kansas State",
                 "Purdue", "Alabama A&M", "USC", "UNLV", "Ferris State",
                 "Liverpool", "Real Madrid", "Toronto Maple Leafs"):
        assert teamcat.find(name), f"{name} is missing from the catalogue"


def test_every_entry_has_a_name_a_league_and_a_sport():
    for t in teamcat.all_teams():
        assert t["name"].strip()
        assert t["league"].strip()
        assert t["sports"], t["name"]


def test_the_leagues_people_mean_are_offered_first():
    order = teamcat.leagues()
    assert order[:5] == ["NFL", "NBA", "MLB", "NHL", "NCAA"]


# ---- normalising ----

def test_the_app_and_the_builder_normalise_identically():
    """Two copies of one rule. If they drift, nothing ever matches, and the
    symptom is a silent empty list rather than an error."""
    build = _builder()
    for name in ("FC Bayern Munchen", "San Jose State", "Club Tijuana",
                 "St. Louis CITY SC", "Alabama A&M", "AC Milan", "Real Madrid",
                 "North Carolina Central", ""):
        assert teamcat.norm(name) == build.norm(name), name


def test_accents_do_not_make_two_teams():
    assert teamcat.norm("San Jose State") == teamcat.norm("San José State")


def test_club_words_do_not_make_two_teams():
    assert teamcat.norm("Club Tijuana") == teamcat.norm("Tijuana")
    assert teamcat.norm("FC Barcelona") == teamcat.norm("Barcelona")


def test_two_different_teams_do_not_normalise_together():
    assert teamcat.norm("Kansas") != teamcat.norm("Kansas State")
    assert teamcat.norm("New York Jets") != teamcat.norm("New York Giants")


def test_a_name_the_catalogue_has_never_heard_of_is_simply_missing():
    assert teamcat.find("Sheffield Wombats") is None


# ---- meeting Plex ----

def test_plexs_own_teams_are_recognised(synced):
    """The fake guide names the Chiefs the way a real one does."""
    hit = teamcat.find("Kansas City Chiefs")
    assert hit and hit["league"] == "NFL"


def test_a_synced_team_is_marked_as_playing(synced):
    row = db.one("SELECT * FROM teams WHERE name = 'Kansas City Chiefs'")
    assert row["in_guide"] == 1
    assert row["league"] == "NFL", "the league comes from the catalogue"
    assert row["last_seen"]


def test_a_team_that_leaves_the_guide_is_kept_not_deleted(synced, plex):
    """It used to be deleted, so the list shrank to whoever played this week
    and a pass lost the name of what it followed."""
    with db.tx() as c:
        c.execute("INSERT INTO teams (id, name, league, in_guide, last_seen, "
                  "updated_at) VALUES (9001, 'Gone Team', 'NFL', 1, 1, 1)")
    prov = plex.dvrs()[0]["epgIdentifier"]
    sync.sync_teams(plex, prov, "3")
    row = db.one("SELECT * FROM teams WHERE id = 9001")
    assert row is not None, "the row survives"
    assert row["in_guide"] == 0, "but it is no longer marked as playing"
