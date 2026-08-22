"""The Plex client, against a server that reproduces the real one's quirks."""
import pytest

from app.plex import Plex, PlexError, discover
from tests import fake_plex


def test_server_info(plex):
    info = plex.server_info()
    assert info["friendlyName"] == "fakeplex"


def test_discover_finds_all_three_sections(plex):
    provider, shows, sports, movies = discover(plex)
    assert provider == fake_plex.PROVIDER
    assert (shows, sports, movies) == ("1", "3", "2")


def test_a_section_queried_with_the_wrong_type_returns_nothing(plex):
    """Not an error. This is how sixteen channels once came to look empty."""
    provider, shows, sports, _ = discover(plex)
    assert plex.section_all(provider, sports, type=4)
    assert plex.section_all(provider, sports, type=1) == []


def test_bulk_listing_carries_genres_but_not_teams(plex):
    """The quirk B1 depended on. If the fake ever grows a Team here, B1's
    regression test stops proving anything."""
    provider, _, sports, _ = discover(plex)
    item = plex.section_all(provider, sports, type=4)[0]
    assert item.get("Genre")
    assert "Team" not in item


def test_metadata_carries_teams(plex):
    provider, _, sports, _ = discover(plex)
    item = plex.section_all(provider, sports, type=4)[0]
    full = plex.metadata(provider, item["ratingKey"])
    assert [t["tag"] for t in full["Team"]] == ["Kansas City Chiefs",
                                                "Tampa Bay Buccaneers"]


def test_template_accepts_an_already_encoded_rating_key(plex):
    """B-fix: the stored rating_key is percent-encoded, and httpx encodes
    params again. Encoded twice, the real server answers 400."""
    encoded = "plex%3A%2F%2Fepisode%2Fgame1"
    opts = plex.template(encoded)
    assert opts, "the client must unquote before sending"
    titles = [s["title"] for t in opts for s in t["MediaSubscription"]]
    assert "This Episode" in titles


def test_template_rejects_a_genuinely_bad_guid(plex):
    with pytest.raises(PlexError):
        plex.template("not-a-guid")


def test_create_returns_the_new_key_from_the_reply(plex):
    opts = plex.template(fake_plex.GAME_GUID)
    one = [s for t in opts for s in t["MediaSubscription"]
           if s["title"].startswith("This")][0]
    key = plex.create_recording(one["parameters"], 2, 4, {"oneShot": "1"})
    assert key and key.isdigit()
    assert plex.subscription_exists(key) is True


def test_prefs_are_url_encoded(plex):
    """B4: a value with a space or an & used to go into the URL raw."""
    opts = plex.template(fake_plex.GAME_GUID)
    one = [s for t in opts for s in t["MediaSubscription"]][0]
    plex.create_recording(one["parameters"], 2, 4,
                          {"oneShot": "1", "startOffsetMinutes": "2 &x=9"})
    got = fake_plex.STATE.created[-1]["prefs"]
    assert got["startOffsetMinutes"] == "2 &x=9", "the value must survive intact"
    assert "x" not in got, "an & in a value must not become a second parameter"


def test_subscription_exists_is_tri_state(plex):
    """B9: None means unknown. Only a 404 proves it is gone. A caller that
    reads None as False reports a working booking as discarded."""
    opts = plex.template(fake_plex.GAME_GUID)
    one = [s for t in opts for s in t["MediaSubscription"]][0]
    key = plex.create_recording(one["parameters"], 2, 4, {"oneShot": "1"})
    assert plex.subscription_exists(key) is True
    plex.delete_subscription(key)
    assert plex.subscription_exists(key) is False

    dead = Plex("http://127.0.0.1:1", "t")
    try:
        assert dead.subscription_exists("5") is None
    finally:
        dead.close()


def test_a_create_can_be_discarded_by_the_server(plex):
    """The server answers 200 with a key, then drops it."""
    fake_plex.STATE.drop_next_create = True
    opts = plex.template(fake_plex.GAME_GUID)
    one = [s for t in opts for s in t["MediaSubscription"]][0]
    key = plex.create_recording(one["parameters"], 2, 4, {"oneShot": "1"})
    assert key
    assert plex.subscription_exists(key) is False


def test_the_client_reuses_one_connection(plex):
    """E1: a fresh client per call meant a handshake per request."""
    first = plex._client()
    plex.server_info()
    assert plex._client() is first
