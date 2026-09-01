"""Finding a thing the guide has never heard of, and following it.

Every source is pointed at the local fake. No test may reach a third party.
"""
import pytest

from app import db, expectations
from app.sources import tmdb, tvmaze
from tests import fake_sources


@pytest.fixture(scope="module")
def sources_base():
    url = fake_sources.start()
    yield url
    fake_sources.stop()


@pytest.fixture(autouse=True)
def _point_at_the_fake(sources_base, monkeypatch):
    monkeypatch.setattr(tvmaze, "BASE", sources_base)
    monkeypatch.setattr(tmdb, "BASE", sources_base)


def test_search_finds_a_show_that_is_not_in_the_guide(client):
    body = client.get("/api/announced?q=Gobiligook").json()
    assert body["ok"] is True
    assert "Gobiligook" in [a["title"] for a in body["announced"]]


def test_a_month_only_result_is_never_given_a_time(client):
    found = [a for a in client.get("/api/announced?q=Gobiligook").json()["announced"]
             if a["title"] == "Gobiligook"][0]
    assert found["precision"] == "month"
    assert "12:00" not in found["when"]
    assert "00:00" not in found["when"]


def test_an_empty_query_asks_nobody_anything(client):
    assert client.get("/api/announced?q=  ").json()["announced"] == []


def test_tmdb_is_skipped_with_no_key(client):
    db.set_setting("tmdb_key", "")
    body = client.get("/api/announced?q=Quorbis").json()
    assert body["ok"] is True
    assert not [a for a in body["announced"] if a["source"] == "tmdb"]


def test_tmdb_answers_once_a_key_is_set(client):
    db.set_setting("tmdb_key", "demo-key")
    body = client.get("/api/announced?q=Quorbis").json()
    assert "Quorbis Rising" in [a["title"] for a in body["announced"]]


def test_one_source_failing_does_not_lose_the_others(client, monkeypatch):
    """A third party being down is not something the user can act on, and the
    others may still hold the answer."""
    def boom(*args, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(tmdb, "search", boom)
    db.set_setting("tmdb_key", "demo-key")
    body = client.get("/api/announced?q=Gobiligook").json()
    assert body["ok"] is True
    assert "Gobiligook" in [a["title"] for a in body["announced"]]


def test_following_a_result_makes_a_pass_and_an_expectation(client):
    body = client.post("/api/announced/follow",
                       data={"source": "tvmaze", "source_id": "99999",
                             "title": "Gobiligook"}).json()
    assert body["ok"] is True
    waiting = expectations.waiting(body["pass_id"])
    assert [e["title"] for e in waiting] == ["Gobiligook"]
    assert db.one("SELECT 1 FROM passes WHERE id = ?", (body["pass_id"],))


def test_following_the_same_thing_twice_does_not_pile_up(client):
    first = client.post("/api/announced/follow",
                        data={"source": "tvmaze", "source_id": "99999",
                              "title": "Gobiligook"}).json()["pass_id"]
    second = client.post("/api/announced/follow",
                         data={"source": "tvmaze", "source_id": "99999",
                               "title": "Gobiligook"}).json()["pass_id"]
    assert first == second
    assert len(expectations.waiting(first)) == 1


def test_a_team_is_not_followed_through_here(client):
    """Teams come from the team picker, and `fill_team_passes` gives them
    their games. This route only ever sees a series or a film, and the branch
    that pretended otherwise was unreachable."""
    body = client.post("/api/announced/follow",
                       data={"source": "thesportsdb", "source_id": "4391",
                             "title": "Ravens"}).json()
    assert body["ok"] is True
    assert expectations.waiting(body["pass_id"]) == []


def test_a_blank_title_is_refused(client):
    r = client.post("/api/announced/follow",
                    data={"source": "tvmaze", "source_id": "1", "title": "  "})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_nothing_is_booked_by_following(client):
    """An expectation is an intention. Only a guide airing carries a channel,
    so only a guide airing can be recorded."""
    before = db.one("SELECT COUNT(*) c FROM our_grabs")["c"]
    client.post("/api/announced/follow",
                data={"source": "tvmaze", "source_id": "99999",
                      "title": "Gobiligook"})
    assert db.one("SELECT COUNT(*) c FROM our_grabs")["c"] == before
