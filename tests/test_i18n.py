"""A team name that is not written in Latin script.

Both folds ended with `[^a-z0-9]`, which does not narrow a string so much as
delete it: Cyrillic, Greek, Japanese, Hebrew and Arabic all came out empty. An
empty key is not a miss, it is a key, so every team written in one of those
scripts became the same team as every other.
"""
import time

from app import db, passes, sync, teamcat


def _team_pass(team_id, name):
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at, "
                  "networks, channels, uid) VALUES ('team',?,?,1,?,'[]','[]',?)",
                  (team_id, name, int(time.time()), f"uid-{name}"))
    return db.one("SELECT * FROM passes ORDER BY id DESC LIMIT 1")


NON_LATIN = ["Зенит", "ЦСКА Москва", "Динамо Киев", "阪神タイガース",
             "読売ジャイアンツ", "浦和レッズ", "Παναθηναϊκός", "Ολυμπιακός",
             "الأهلي", "الزمالك", "베이스볼"]


# ---- the fold itself ----

def test_a_non_latin_name_does_not_fold_away():
    for name in NON_LATIN:
        assert teamcat.ident(name), f"ident({name!r}) came out empty"
        assert teamcat.norm(name), f"norm({name!r}) came out empty"


def test_non_latin_teams_stay_distinct_from_each_other():
    """The failure that mattered. Eleven teams, one key, one team."""
    assert len({teamcat.ident(n) for n in NON_LATIN}) == len(NON_LATIN)
    assert len({teamcat.norm(n) for n in NON_LATIN}) == len(NON_LATIN)


def test_a_name_that_is_only_club_words_keeps_them():
    """"Athletic Club" is the real name of a real side, and every word in it is
    on the strip list. Stripping to nothing must fall back rather than yield a
    key that everything else can collide with."""
    assert teamcat.norm("Athletic Club") == teamcat.norm("Athletic Club")
    assert teamcat.norm("Athletic Club") != ""
    assert teamcat.norm("VFB") != ""


def test_the_latin_folding_is_unchanged():
    """Nothing that worked may stop working."""
    assert teamcat.norm("Club Tijuana") == teamcat.norm("Tijuana")
    assert teamcat.norm("FC Barcelona") == teamcat.norm("Barcelona")
    assert teamcat.norm("San Jose State") == teamcat.norm("San José State")
    assert teamcat.norm("Kansas") != teamcat.norm("Kansas State")
    assert teamcat.ident("Real Madrid") != teamcat.ident("Atletico Madrid")
    assert teamcat.ident("FC Bayern München") == teamcat.ident("FC Bayern Munchen")


def test_a_turkish_dotless_i_is_kept():
    """It was silently dropped, so two sides differing only there merged."""
    assert "i" in teamcat.ident("Şanlıurfaspor") or "ı" in teamcat.ident("Şanlıurfaspor")
    assert teamcat.ident("Beşiktaş") == teamcat.ident("Besiktas")


# ---- the lookups that key on it ----

def test_the_catalogue_misses_rather_than_guessing():
    """It used to answer VfB Stuttgart for every Cyrillic and Japanese name,
    because an alias folded to the empty string and claimed that key."""
    for name in NON_LATIN:
        assert teamcat.find(name) is None, f"find({name!r}) invented a team"
    assert teamcat.find("") is None
    assert teamcat.find("   ") is None


def test_a_known_team_is_still_found():
    assert teamcat.find("Kansas City Chiefs")
    assert teamcat.find("FC Barcelona")


# ---- and what that did to a pass ----

def test_non_latin_passes_are_not_all_repointed_at_one_team(clean_db):
    """Four distinct teams in the guide used to collapse to a single key, so a
    pass for Zenit was repointed at the Hanshin Tigers and had its name
    overwritten to match."""
    now = int(time.time())
    with db.tx() as c:
        for i, name in enumerate(["Зенит", "ЦСКА Москва", "Динамо Киев",
                                  "阪神タイガース"], start=700):
            c.execute("INSERT INTO teams (id, name, in_guide, last_seen, updated_at) "
                      "VALUES (?,?,1,?,?)", (i, name, now, now))
    p = _team_pass(None, "Зенит")
    sync.resolve_team_passes()
    row = db.one("SELECT team_id, team_name FROM passes WHERE id = ?", (p["id"],))
    assert row["team_name"] == "Зенит"
    assert row["team_id"] == 700


def test_a_non_latin_pass_survives_a_renumber_like_any_other(clean_db):
    """The whole point of matching on the name. It has to work in every script,
    not only the one the author happened to test in."""
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  ("plex://episode/ru1", "Зенит vs ЦСКА", "rk",
                   '[{"id": 999, "name": "\\u0417\\u0435\\u043d\\u0438\\u0442"}]',
                   "Football", "sports"))
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, channel_vcn, "
                  "channel_identifier, begins_at, ends_at, premiere) "
                  "VALUES (?,?,?,?,?,?,1)",
                  ("ru-a1", "plex://episode/ru1", "41.1", "id-41.1",
                   now + 3600, now + 7200))
    # The pass holds the id Plex used before its last refresh.
    assert passes.candidate_airings(111, team_name="Зенит")


def test_a_pass_for_a_different_non_latin_team_is_not_matched(clean_db):
    now = int(time.time())
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO programs (guid, title, rating_key, teams, "
                  "grandparent_title, section) VALUES (?,?,?,?,?,?)",
                  ("plex://episode/ru1", "Game", "rk",
                   '[{"id": 999, "name": "\\u0417\\u0435\\u043d\\u0438\\u0442"}]',
                   "Football", "sports"))
        c.execute("INSERT OR REPLACE INTO airings (id, program_guid, channel_vcn, "
                  "channel_identifier, begins_at, ends_at, premiere) "
                  "VALUES (?,?,?,?,?,?,1)",
                  ("ru-a1", "plex://episode/ru1", "41.1", "id-41.1",
                   now + 3600, now + 7200))
    assert not passes.candidate_airings(111, team_name="ЦСКА Москва")


def test_a_dakuten_is_not_an_accent(clean_db):
    """Stripping combining marks is right for Latin and wrong beyond it. In
    Japanese the dakuten changes the consonant: strip it and ガ becomes カ, so
    "tigers" and a word that is not "tigers" fold together."""
    assert teamcat.ident("ガ") != teamcat.ident("カ")
    assert teamcat.ident("阪神タイガース") != teamcat.ident("阪神タイカース")
    # And the Latin case it exists for still works.
    assert teamcat.ident("San José State") == teamcat.ident("San Jose State")
    assert teamcat.ident("Beşiktaş") == teamcat.ident("Besiktas")
    assert teamcat.ident("FC Bayern München") == teamcat.ident("FC Bayern Munchen")
