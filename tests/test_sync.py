"""Pulling the guide in, and keeping what was already learned."""
import json

from app import db, sync
from tests import fake_plex


def test_full_sync_reports_what_it_pulled(synced):
    assert "programs" in synced and "airings" in synced


def test_programmes_and_airings_are_separate_things(synced):
    """The distinction the whole project rests on: one game, two broadcasts."""
    n = db.one("SELECT COUNT(*) c FROM airings WHERE program_guid = ?",
               (fake_plex.GAME_GUID,))["c"]
    assert n == 2
    assert db.one("SELECT COUNT(*) c FROM programs WHERE guid = ?",
                  (fake_plex.GAME_GUID,))["c"] == 1


def test_the_premiere_flag_survives_the_pull(synced):
    live = db.one("SELECT * FROM airings WHERE program_guid = ? AND premiere = 1",
                  (fake_plex.GAME_GUID,))
    assert live["channel_vcn"] == "41.1"
    assert live["begins_at"] == fake_plex.LIVE_AT


def test_drm_is_recorded(synced):
    row = db.one("SELECT * FROM airings WHERE program_guid = ?", (fake_plex.DRM_GUID,))
    assert row["drm"] == 1


def test_teams_are_enriched_because_a_listing_does_not_carry_them(synced):
    teams = db.unjs(db.one("SELECT teams FROM programs WHERE guid = ?",
                           (fake_plex.GAME_GUID,))["teams"])
    assert [t["name"] for t in teams] == ["Kansas City Chiefs", "Tampa Bay Buccaneers"]


def test_a_second_sync_does_not_re_enrich_anything(plex, synced):
    """B1, the one worth measuring. The upsert used to write the listing's
    empty Team array back over what enrichment had fetched, so every sync
    re-fetched every sports programme, forever."""
    before = fake_plex.STATE.metadata_calls
    assert before > 0, "the first sync must enrich"
    sync.full_sync()
    assert fake_plex.STATE.metadata_calls == before, "the second must enrich nothing"
    teams = db.unjs(db.one("SELECT teams FROM programs WHERE guid = ?",
                           (fake_plex.GAME_GUID,))["teams"])
    assert len(teams) == 2, "and the tags must survive it"


def test_network_is_read_from_the_channel_title(synced):
    assert db.one("SELECT network FROM channels WHERE vcn = '41.1'")["network"] == "NBC"
    assert db.one("SELECT network FROM channels WHERE vcn = '9.1'")["network"] == "ABC"


def test_network_of_handles_what_the_guide_actually_sends():
    assert sync.network_of("41.1 KQGGDT (NBC)") == "NBC"
    assert sync.network_of("50.1 KPXEDT (ION Television)") == "ION Television"
    assert sync.network_of("12.3 WXYZ") is None
    assert sync.network_of("") is None
    assert sync.network_of(None) is None
    # Only the trailing parenthetical is the network. What precedes it is the
    # channel number and call sign, and the number carries a dot of its own.
    assert sync.network_of("2.1 KTVI-DT (FOX)") == "FOX"


def test_airings_that_leave_the_guide_are_dropped(plex, synced):
    with db.tx() as c:
        c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, begins_at,
                                          ends_at, updated_at)
                     VALUES ('stale', ?, '41.1', 1, 2, 0)""", (fake_plex.GAME_GUID,))
    sync.full_sync()
    assert db.one("SELECT 1 FROM airings WHERE id = 'stale'") is None


def test_a_subscription_is_named_by_what_it_follows(plex, synced):
    """Plex titles a rule "All Episodes", which says nothing."""
    opts = plex.template(fake_plex.EPISODE_GUID)
    rule = [s for t in opts for s in t["MediaSubscription"]
            if s["title"] == "All Episodes"][0]
    plex.create_recording(rule["parameters"], 2, 2, {"oneShot": "0"})
    sync.sync_recordings(plex)
    row = db.one("SELECT title, target FROM plex_subscriptions")
    assert row["title"] == "All Episodes"
    assert row["target"] == "Quiz Show"


def test_media_index_arrives_as_a_string_and_is_coerced(plex, synced):
    opts = plex.template(fake_plex.GAME_GUID)
    one = [s for t in opts for s in t["MediaSubscription"]][0]
    plex.create_recording(one["parameters"], 2, 4,
                          {"oneShot": "1", "startTimeslot": str(fake_plex.LIVE_AT)})
    sync.sync_recordings(plex)
    grab = db.one("SELECT * FROM plex_grabs")
    assert grab["begins_at"] == fake_plex.LIVE_AT


def test_our_own_recordings_are_attributed_to_us(plex, synced):
    from app import passes
    rows = passes.candidate_airings(236)
    pick, _ = passes.choose_airing(rows)
    passes._schedule(plex, pick, None, "manual")
    sync.sync_recordings(plex)
    assert db.one("SELECT owned_by_us FROM plex_subscriptions")["owned_by_us"] == 1
