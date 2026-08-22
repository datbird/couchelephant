"""Every endpoint, driven in-process. No port, no network, no real Plex."""
import json
import time

from app import auth, db, passes, sync
from tests import fake_plex


def test_health_needs_no_sign_in(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["configured"] is True


def test_the_pages_render(client, synced):
    for path in ("/", "/recordings", "/settings"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "CouchElephant" in r.text


def test_an_unconfigured_install_is_sent_to_the_setup_screen(client):
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/welcome"
    assert "Set up CouchElephant" in client.get("/welcome").text


def test_the_setup_screen_names_what_can_wait(client):
    db.set_setting("plex_url", "")
    body = client.get("/welcome").text
    assert "Cloudflare Access" in body
    assert "Preview mode" in body
    assert "Timezone your Plex server is in" in body


def test_first_run_refuses_a_server_it_cannot_reach_and_saves_nothing(client):
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    r = client.post("/welcome", data={"plex_url": "http://127.0.0.1:1",
                                      "plex_token": "x", "timezone": "UTC"},
                    headers={"Accept": "application/json"})
    assert r.json()["ok"] is False
    assert db.get_setting("plex_url") == "", "a failed test must save nothing"


def test_first_run_saves_a_server_that_answers(client, plex_url):
    db.set_setting("plex_url", "")
    db.set_setting("plex_token", "")
    r = client.post("/welcome", data={"plex_url": plex_url, "plex_token": "t",
                                      "timezone": "America/Chicago"},
                    headers={"Accept": "application/json"})
    assert r.json()["ok"] is True
    assert db.get_setting("plex_url") == plex_url
    assert db.get_setting("dry_run") == "1", "a new install starts in preview"


# ---- the guide ----

def test_the_grid_returns_channels_and_airings(client, synced):
    r = client.get("/api/grid", params={"start": fake_plex.LIVE_AT - 3600,
                                        "end": fake_plex.LIVE_AT + 3600})
    d = r.json()
    assert "41.1" in [c["vcn"] for c in d["channels"]]
    assert any(a["premiere"] for a in d["airings"])


def test_the_programme_panel_lists_every_airing_of_it(client, synced):
    aid = db.one("SELECT id FROM airings WHERE program_guid = ? AND premiere = 1",
                 (fake_plex.GAME_GUID,))["id"]
    d = client.get("/api/program", params={"airing_id": aid}).json()
    assert len(d["airings"]) == 2
    assert [a["vcn"] for a in d["airings"] if a["premiere"]] == ["41.1"]
    assert d["teams"][0]["name"] == "Kansas City Chiefs"


def test_a_missing_airing_is_a_404(client, synced):
    assert client.get("/api/program", params={"airing_id": "nope"}).status_code == 404


def test_record_options_come_from_plex_and_hide_its_plumbing(client, synced):
    aid = db.one("SELECT id FROM airings WHERE premiere = 1 AND drm = 0")["id"]
    d = client.get("/api/record/options", params={"airing_id": aid}).json()
    assert d["ok"]
    ids = [s["id"] for t in d["templates"] for s in t["settings"]]
    assert "minVideoQuality" in ids
    assert "oneShot" not in ids, "unlabelled settings are plumbing"
    assert "comskipEnabled" not in ids


def test_a_time_option_label_is_decoded(client, synced):
    aid = db.one("SELECT id FROM airings WHERE premiere = 1 AND drm = 0")["id"]
    d = client.get("/api/record/options", params={"airing_id": aid}).json()
    labels = [o["label"] for t in d["templates"] for s in t["settings"]
              for o in s["options"]]
    assert "07:00 PM" in labels, "Plex URL-encodes these inside the enum"


def test_a_single_broadcast_arrives_pinned(client, synced):
    row = db.one("SELECT * FROM airings WHERE premiere = 1 AND drm = 0")
    d = client.get("/api/record/options", params={"airing_id": row["id"]}).json()
    one = [t for t in d["templates"] if t["one_shot"]][0]
    vals = {s["id"]: s["value"] for s in one["settings"]}
    assert vals["startTimeslot"] == str(row["begins_at"])
    assert vals["lineupChannel"] == row["channel_identifier"]


def test_a_drm_airing_cannot_be_recorded(client, synced):
    aid = db.one("SELECT id FROM airings WHERE drm = 1")["id"]
    assert client.get("/api/record/options", params={"airing_id": aid}).status_code == 400
    r = client.post("/api/record", data={"airing_id": aid})
    assert r.status_code == 400 and "DRM" in r.json()["error"]


def test_preview_mode_refuses_to_record_and_says_why(client, synced):
    db.set_setting("dry_run", "1")
    aid = db.one("SELECT id FROM airings WHERE premiere = 1 AND drm = 0")["id"]
    r = client.post("/api/record", data={"airing_id": aid})
    assert r.status_code == 400
    assert "Preview mode" in r.json()["error"]
    assert fake_plex.STATE.created == []


def test_recording_and_cancelling_one_broadcast(client, synced):
    aid = db.one("SELECT id FROM airings WHERE premiere = 1 AND drm = 0")["id"]
    r = client.post("/api/record", data={"airing_id": aid, "template": 0,
                                         "settings": "{}"})
    assert r.json()["ok"] is True
    assert db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (aid,))
    assert len(fake_plex.STATE.subscriptions) == 1

    r = client.post("/api/record/cancel", data={"airing_id": aid})
    assert r.json()["ok"] is True
    assert db.one("SELECT 1 FROM our_grabs WHERE airing_id = ?", (aid,)) is None
    assert fake_plex.STATE.subscriptions == {}


def test_cancelling_something_we_did_not_book_is_refused(client, synced):
    aid = db.one("SELECT id FROM airings LIMIT 1")["id"]
    r = client.post("/api/record/cancel", data={"airing_id": aid})
    assert r.status_code == 404


# ---- rules ----

def test_no_source_limit_books_a_plain_plex_rule(client, synced):
    r = client.post("/api/rules", data={
        "kind": "series", "series": "Quiz Show",
        "networks": "[]", "channels": "[]", "template": 0, "settings": "{}"})
    d = r.json()
    assert d["ok"] and d["ce_rule"] is False
    assert not db.query("SELECT 1 FROM passes"), "Plex holds this one, not us"
    assert len(fake_plex.STATE.subscriptions) == 1


def test_a_source_limit_makes_couchelephant_keep_the_rule(client, synced):
    r = client.post("/api/rules", data={
        "kind": "team", "team_id": "236",
        "networks": '["NBC"]', "channels": "[]", "template": 0,
        "settings": '{"startOffsetMinutes":"2"}'})
    d = r.json()
    assert d["ok"] and d["ce_rule"] is True
    row = db.one("SELECT * FROM passes")
    assert db.unjs(row["networks"]) == ["NBC"]
    prefs = db.unjs(row["prefs"], {})
    assert prefs["startOffsetMinutes"] == "2", "the settings belong to the pass"
    assert "lineupChannel" not in prefs, "a pass pins for itself"


def test_following_the_same_team_twice_is_refused(client, synced):
    data = {"kind": "team", "team_id": "236", "networks": '["NBC"]',
            "channels": "[]", "template": 0, "settings": "{}"}
    assert client.post("/api/rules", data=data).json()["ok"] is True
    r = client.post("/api/rules", data=data)
    assert r.status_code == 409


def test_following_a_team_from_the_panel_books_straight_away(client, synced):
    """I1: it used to insert the pass and wait for the next sync."""
    r = client.post("/api/pass", data={"team_id": "236"})
    d = r.json()
    assert d["ok"] and "scheduled" in d["message"]
    assert db.one("SELECT 1 FROM our_grabs"), "the game is booked now, not in an hour"


def test_a_pass_reports_what_it_will_record_and_why(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    rid = db.one("SELECT id FROM passes")["id"]
    d = client.get(f"/api/rules/{rid}/upcoming").json()
    assert d["ok"]
    assert d["upcoming"][0]["vcn"] == "41.1"
    assert "premiere" in d["upcoming"][0]["reason"]


def test_editing_a_pass_changes_its_limit_and_its_settings(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    rid = db.one("SELECT id FROM passes")["id"]
    r = client.post(f"/api/rules/{rid}", data={
        "networks": '["ABC"]', "channels": '["9.1"]', "enabled": "1",
        "settings": '{"startOffsetMinutes":"4","startTimeslot":"-1"}'})
    assert r.json()["ok"]
    row = db.one("SELECT * FROM passes WHERE id = ?", (rid,))
    assert db.unjs(row["networks"]) == ["ABC"]
    prefs = db.unjs(row["prefs"], {})
    assert prefs["startOffsetMinutes"] == "4"
    assert "startTimeslot" not in prefs, "pinning keys are stripped"


def test_pausing_a_pass_stops_it_booking(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    rid = db.one("SELECT id FROM passes")["id"]
    client.post(f"/passes/{rid}/toggle", follow_redirects=False)
    assert db.one("SELECT enabled FROM passes WHERE id = ?", (rid,))["enabled"] == 0
    before = len(fake_plex.STATE.created)
    passes.run_passes()
    assert len(fake_plex.STATE.created) == before


def test_the_rules_list_shows_both_ours_and_plexs(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    client.post("/api/rules", data={"kind": "series", "series": "Quiz Show",
                                    "networks": "[]", "channels": "[]",
                                    "template": 0, "settings": "{}"})
    rules = client.get("/api/rules").json()["rules"]
    who = sorted(r["who"] for r in rules)
    assert who == ["ce", "plex"]
    plex_rule = [r for r in rules if r["who"] == "plex"][0]
    assert plex_rule["title"] == "Quiz Show", "named by what it follows"


# ---- schedule ----

def test_the_schedule_says_who_booked_each_thing_and_why(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    sync.sync_recordings(_plex())
    rows = client.get("/api/schedule").json()["rows"]
    assert rows
    mine = [r for r in rows if r["who"] == "ce"]
    assert mine and "Kansas City Chiefs pass" in mine[0]["reason"]
    assert mine[0]["airing_id"], "it links back to the broadcast in the guide"


def test_a_windowed_schedule_counts_its_own_window(client, synced):
    """I4: total used to count every grab while the rows were windowed."""
    client.post("/api/pass", data={"team_id": "236"})
    sync.sync_recordings(_plex())
    far = fake_plex.LIVE_AT + 10 * 86400
    d = client.get("/api/schedule", params={"start": far, "end": far + 86400}).json()
    assert d["total"] == 0 and d["more"] is False


def _plex():
    from app.plex import Plex
    return Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))


# ---- lists behind the panels ----

def test_teams_and_series_are_searchable(client, synced):
    assert client.get("/api/teams", params={"q": "chief"}).json()["teams"]
    assert client.get("/api/teams", params={"q": "zzz"}).json()["teams"] == []
    names = [s["name"] for s in client.get("/api/series").json()["series"]]
    assert "Quiz Show" in names


def test_sources_are_grouped_by_network(client, synced):
    d = client.get("/api/sources").json()
    nets = {n["name"]: n["channels"] for n in d["networks"]}
    assert nets["NBC"] == ["41.1"]
    assert len(d["channels"]) == 4


# ---- settings ----

def test_the_connection_test_reports_a_good_server(client, synced):
    d = client.post("/settings/test", headers={"Accept": "application/json"}).json()
    assert d["ok"] and "fakeplex" in d["detail"]


def test_the_connection_test_names_the_step_that_failed(client):
    db.set_setting("plex_url", "")
    d = client.post("/settings/test", headers={"Accept": "application/json"}).json()
    assert d["ok"] is False and "No server address" in d["detail"]

    db.set_setting("plex_url", "http://127.0.0.1:1")
    db.set_setting("plex_token", "t")
    d = client.post("/settings/test", headers={"Accept": "application/json"}).json()
    assert d["ok"] is False and "Could not reach" in d["detail"]


def test_saving_settings_keeps_a_masked_token(client, synced):
    before = db.get_setting("plex_token")
    client.post("/settings", data={"plex_url": db.get_setting("plex_url"),
                                   "plex_token": "************",
                                   "timezone": "UTC", "sync_minutes": "60"},
                follow_redirects=False)
    assert db.get_setting("plex_token") == before


def test_a_channel_logo_can_be_replaced_and_reset(client, synced):
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    r = client.post("/settings/channels/41.1/logo", files={"logo": ("x.png", png, "image/png")})
    assert r.json()["ok"]
    assert db.one("SELECT custom_logo FROM channels WHERE vcn='41.1'")["custom_logo"]
    assert client.get("/logo/41.1").content == png

    r = client.post("/settings/channels/41.1/logo/reset")
    assert r.json()["ok"]
    assert db.one("SELECT custom_logo FROM channels WHERE vcn='41.1'")["custom_logo"] is None


def test_an_upload_is_judged_by_its_bytes_not_its_name(client, synced):
    r = client.post("/settings/channels/41.1/logo",
                    files={"logo": ("x.png", b"this is not an image", "image/png")})
    assert r.status_code == 400 and "not an image" in r.json()["error"]


def test_svg_is_refused_rather_than_accepted_and_broken(client, synced):
    """B3. It cannot be served as PNG, and serving it as SVG runs its script."""
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = client.post("/settings/channels/41.1/logo",
                    files={"logo": ("x.svg", svg, "image/svg+xml")})
    assert r.status_code == 400 and "SVG" in r.json()["error"]


def test_an_oversized_upload_is_refused(client, synced):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (3 * 1024 * 1024)
    r = client.post("/settings/channels/41.1/logo",
                    files={"logo": ("x.png", big, "image/png")})
    assert r.status_code == 400 and "limit" in r.json()["error"]


def test_a_missing_logo_serves_a_blank_not_a_404(client, synced):
    r = client.get("/logo/99.9")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
