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


# ---- smart passes ----

def test_the_field_list_says_what_can_be_asked_and_with_what(client, synced):
    d = client.get("/api/filter/fields").json()
    ids = [f["id"] for f in d["fields"]]
    for expect in ("genre", "rating", "title", "year", "duration", "channel", "hd"):
        assert expect in ids
    assert "Football" in d["values"]["genres"]
    assert "TV-PG" in d["values"]["ratings"]
    assert d["coverage"]["rated"] < d["coverage"]["programs"], \
        "the panel needs to know the guide does not rate everything"


def test_every_comparison_offered_is_one_the_server_accepts(client, synced):
    """The panel builds itself from this, so a label with no implementation is
    a dead control the user can pick."""
    from app import smartfilter
    d = client.get("/api/filter/fields").json()
    for kind, offered in d["comparisons"].items():
        for c in offered:
            assert c["value"] in {x for x, _ in smartfilter.COMPARISONS[kind]}


def test_the_preview_counts_before_anything_is_created(client, synced):
    d = client.post("/api/filter/preview", data={
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    assert d["ok"] and d["count"] == 1
    assert d["sample"][0]["title"] == "Chiefs at Buccaneers"
    assert fake_plex.STATE.created == [], "a preview books nothing"


def test_the_preview_warns_when_a_filter_narrows_by_nothing(client, synced):
    d = client.post("/api/filter/preview",
                    data={"filter": '{"field":"hd","cmp":"yes"}'}).json()
    assert d["loose"] is True
    assert "most of the guide" in d["warning"]


def test_the_preview_reports_a_broken_filter_in_words(client, synced):
    d = client.post("/api/filter/preview",
                    data={"filter": '{"field":"year","cmp":"gt","value":"soon"}'})
    assert d.status_code == 400
    assert "not a number" in d.json()["error"]


def test_a_smart_pass_is_always_couchelephants(client, synced):
    r = client.post("/api/rules", data={
        "kind": "smart", "name": "Comedy nights",
        "filter": '{"field":"genre","cmp":"is","value":"Comedy"}',
        "networks": "[]", "channels": "[]"})
    d = r.json()
    assert d["ok"] and d["ce_rule"] is True
    row = db.one("SELECT * FROM passes WHERE kind='smart'")
    assert row["label"] == "Comedy nights"
    assert db.unjs(row["filter"])["field"] == "genre"


def test_a_smart_pass_books_the_live_broadcast_straight_away(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]"})
    grab = db.one("SELECT * FROM our_grabs")
    assert grab, "it books now, not at the next sync"
    assert grab["channel_vcn"] == "41.1", "the premiere, not the repeat"


def test_a_loose_filter_is_questioned_before_it_is_obeyed(client, synced):
    r = client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"hd","cmp":"yes"}',
        "networks": "[]", "channels": "[]"})
    assert r.status_code == 409
    d = r.json()
    assert d["needs_confirm"] is True
    assert "Press Create again" in d["error"]
    assert not db.query("SELECT 1 FROM passes"), "nothing was created"
    assert fake_plex.STATE.created == [], "and nothing was booked"


def test_saying_yes_creates_it(client, synced):
    data = {"kind": "smart", "filter": '{"field":"hd","cmp":"yes"}',
            "networks": "[]", "channels": "[]", "confirm": "1"}
    assert client.post("/api/rules", data=data).json()["ok"] is True
    assert db.one("SELECT 1 FROM passes WHERE kind='smart'")


def test_a_named_filter_keeps_its_name_and_an_unnamed_one_describes_itself(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Comedy"}',
        "networks": "[]", "channels": "[]"})
    rules = [r for r in client.get("/api/rules").json()["rules"] if r["kind"] == "smart"]
    assert rules[0]["title"] == "genre is Comedy"
    assert rules[0]["icon"] == "smart"


def test_a_smart_pass_explains_what_it_will_record(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]"})
    rid = db.one("SELECT id FROM passes WHERE kind='smart'")["id"]
    d = client.get(f"/api/rules/{rid}/upcoming").json()
    assert d["ok"]
    assert d["upcoming"][0]["vcn"] == "41.1"
    assert "premiere" in d["upcoming"][0]["reason"]


def test_the_schedule_names_the_smart_pass_that_booked_it(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "name": "Chiefs football",
        "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]"})
    sync.sync_recordings(_plex())
    rows = [r for r in client.get("/api/schedule").json()["rows"] if r["who"] == "ce"]
    assert rows and "Chiefs football pass" in rows[0]["reason"]
    assert rows[0]["kind"] == "smart"


def test_editing_a_smart_pass_changes_its_conditions(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]"})
    rid = db.one("SELECT id FROM passes WHERE kind='smart'")["id"]
    r = client.post(f"/api/rules/{rid}", data={
        "networks": "[]", "channels": "[]", "enabled": "1",
        "filter": '{"field":"genre","cmp":"is","value":"Comedy"}'})
    assert r.json()["ok"]
    assert db.unjs(db.one("SELECT filter FROM passes WHERE id=?", (rid,))["filter"]) \
        == {"field": "genre", "cmp": "is", "value": "Comedy"}


def test_a_filter_that_cannot_run_is_never_stored(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]"})
    rid = db.one("SELECT id FROM passes WHERE kind='smart'")["id"]
    before = db.one("SELECT filter FROM passes WHERE id=?", (rid,))["filter"]
    r = client.post(f"/api/rules/{rid}", data={
        "networks": "[]", "channels": "[]", "enabled": "1",
        "filter": '{"field":"nonsense","cmp":"is","value":"x"}'})
    assert r.status_code == 400
    assert db.one("SELECT filter FROM passes WHERE id=?", (rid,))["filter"] == before


def test_a_broken_filter_does_not_stop_the_other_passes_running(client, synced):
    """One bad row in the passes table used to take the whole run down."""
    client.post("/api/pass", data={"team_id": "236"})
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, filter, label, enabled, created_at) "
                  "VALUES ('smart', ?, 'broken', 1, 0)", ('{"field":"nope"}',))
    done = passes.run_passes()
    assert any(d["action"] == "failed" for d in done)
    assert any(d["action"] in ("scheduled", "skipped") and d["pass"] != "broken"
               for d in done)


def test_the_same_filter_twice_is_refused(client, synced):
    data = {"kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Comedy"}',
            "networks": "[]", "channels": "[]"}
    assert client.post("/api/rules", data=data).json()["ok"] is True
    assert client.post("/api/rules", data=data).status_code == 409


def test_a_smart_pass_obeys_the_source_limit_like_any_other(client, synced):
    r = client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": '["ABC"]', "channels": "[]"})
    assert r.json()["ok"]
    assert db.one("SELECT 1 FROM our_grabs") is None, \
        "the game is only on NBC, and the pass was told ABC"


# ---- Plex's own settings on a pass ----

def test_a_smart_pass_is_offered_plexs_own_settings(client, synced):
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    assert d["ok"]
    ids = [s["id"] for t in d["templates"] for s in t["settings"]]
    assert "startOffsetMinutes" in ids, "padding before"
    assert "endOffsetMinutes" in ids, "padding after"
    assert "minVideoQuality" in ids


def test_a_pass_is_offered_the_settings_it_actually_uses(client, synced):
    """A pass books a pinned one-shot for every airing, so the recurring-only
    choices would be stored and never honoured."""
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    ids = [s["id"] for t in d["templates"] for s in t["settings"]]
    assert "onlyNewAirings" not in ids
    assert "autoDeletionItemPolicyWatchedLibrary" not in ids
    # And nothing CouchElephant sets for itself. A control that is silently
    # dropped on save is worse than no control.
    for pinned in ("lineupChannel", "startTimeslot", "oneShot"):
        assert pinned not in ids


def test_a_plex_rule_is_still_offered_the_recurring_choices(client, synced):
    d = client.get("/api/rules/options",
                   params={"kind": "team", "team_id": "236"}).json()
    assert d["ok"]
    assert all(not t["one_shot"] for t in d["templates"])


def test_a_sports_pass_is_told_that_sport_overruns(client, synced):
    d = client.get("/api/rules/options",
                   params={"kind": "team", "team_id": "236"}).json()
    assert d["sporty"] is True
    assert d["sports_padding"]["endOffsetMinutes"] == "30"


def test_padding_set_on_a_smart_pass_reaches_every_booking(client, synced):
    """This is the whole point: a game that runs long is not cut off."""
    r = client.post("/api/rules", data={
        "kind": "smart", "name": "Football",
        "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]",
        "settings": '{"startOffsetMinutes":"1","endOffsetMinutes":"60"}'})
    assert r.json()["ok"]
    prefs = fake_plex.STATE.created[0]["prefs"]
    assert prefs["endOffsetMinutes"] == "60"
    assert prefs["startOffsetMinutes"] == "1"


def test_the_pinning_keys_are_still_not_the_users_to_set(client, synced):
    client.post("/api/rules", data={
        "kind": "smart", "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]",
        "settings": '{"endOffsetMinutes":"60","startTimeslot":"-1",'
                    '"lineupChannel":"","oneShot":"0"}'})
    stored = db.unjs(db.one("SELECT prefs FROM passes")["prefs"], {})
    assert stored["endOffsetMinutes"] == "60"
    for blocked in ("startTimeslot", "lineupChannel", "oneShot"):
        assert blocked not in stored
    # And the booking is still pinned to the live broadcast.
    prefs = fake_plex.STATE.created[0]["prefs"]
    assert prefs["oneShot"] == "1"
    assert prefs["startTimeslot"] == str(fake_plex.LIVE_AT)


def test_plexs_own_explanation_travels_with_each_setting(client, synced):
    """Plex writes a summary for its settings. Repeating it here would be a
    second copy to keep true."""
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    hints = {s["id"]: s["hint"] for t in d["templates"] for s in t["settings"]}
    assert "adding minutes after" in hints["endOffsetMinutes"]


def test_padding_offers_a_big_number_without_capping_anything(client, synced):
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    presets = {s["id"]: s["presets"] for t in d["templates"] for s in t["settings"]}
    assert 120 in presets["endOffsetMinutes"]
    assert presets["endOffsetMinutes"] == sorted(presets["endOffsetMinutes"])


def test_a_padding_larger_than_any_preset_is_still_accepted(client, synced):
    """Plex sends these as a plain integer with no allowed-values list, so
    there is no ceiling to enforce and none is invented here."""
    r = client.post("/api/rules", data={
        "kind": "smart", "name": "Long games",
        "filter": '{"field":"genre","cmp":"is","value":"Football"}',
        "networks": "[]", "channels": "[]",
        "settings": '{"endOffsetMinutes":"240"}'})
    assert r.json()["ok"]
    assert fake_plex.STATE.created[0]["prefs"]["endOffsetMinutes"] == "240"


def test_the_padding_pair_is_last_and_in_that_order(client, synced):
    """Plex lists them in the middle, which splits the two fields people
    actually reach for."""
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    ids = [s["id"] for s in d["templates"][0]["settings"]]
    assert ids[-2:] == ["startOffsetMinutes", "endOffsetMinutes"]
    assert "comskipMethod" in ids[:-2]


def test_everything_else_keeps_plexs_own_order(client, synced):
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Football"}'}).json()
    ids = [s["id"] for s in d["templates"][0]["settings"]]
    assert ids[:4] == ["minVideoQuality", "replaceLowerQuality",
                       "recordPartials", "comskipMethod"]


def test_the_settings_can_be_asked_for_with_no_target(client, synced):
    """A filter with nothing in it, or a team that has not played."""
    d = client.get("/api/rules/options",
                   params={"kind": "any", "ce_pass": 1}).json()
    assert d["ok"]
    ids = [s["id"] for s in d["templates"][0]["settings"]]
    assert "startOffsetMinutes" in ids and "endOffsetMinutes" in ids


def test_an_empty_filter_still_gets_the_settings(client, synced):
    d = client.get("/api/rules/options",
                   params={"kind": "smart", "ce_pass": 1, "filter": ""}).json()
    assert d["ok"]
    assert [s["id"] for s in d["templates"][0]["settings"]]


def test_a_filter_matching_nothing_still_gets_the_settings(client, synced):
    d = client.get("/api/rules/options", params={
        "kind": "smart", "ce_pass": 1,
        "filter": '{"field":"genre","cmp":"is","value":"Curling"}'}).json()
    assert d["ok"], "the settings are Plex's, not the filter's"


def test_the_sports_route_gets_its_padding_whatever_stands_in(client, synced):
    """The panel knows it is on the sports route before a team is chosen. Left
    to the sample airing, the padding was filled in on one server and not on
    another."""
    d = client.get("/api/rules/options",
                   params={"kind": "any", "ce_pass": 1, "sporty": 1}).json()
    assert d["sporty"] is True
    assert d["sports_padding"]["endOffsetMinutes"] == "30"

    plain = client.get("/api/rules/options",
                       params={"kind": "any", "ce_pass": 1}).json()
    assert plain["ok"]


# ---- review regressions ----

def test_following_a_team_as_a_plex_rule_picks_the_team_not_the_league(client, synced):
    """Plex lists the league template before the team's. The panel shows the
    team first, so a position in the shown list is the wrong thing to send.
    The payload carries Plex's own index; that is what must come back."""
    opts = client.get("/api/rules/options",
                      params={"kind": "team", "team_id": "236"}).json()
    first = opts["templates"][0]
    assert "Chiefs" in first["title"], "the panel leads with the named team"
    assert first["index"] != 0, "which is not where Plex lists it"
    r = client.post("/api/rules", data={
        "kind": "team", "team_id": "236", "networks": "[]", "channels": "[]",
        "template": first["index"], "settings": "{}"}).json()
    assert r["ok"] and r["ce_rule"] is False
    titles = [s["title"] for s in fake_plex.STATE.subscriptions.values()]
    assert titles == ["All Kansas City Chiefs Events"], titles


def test_a_position_that_names_a_one_shot_falls_back_to_a_recurring_rule(client, synced):
    r = client.post("/api/rules", data={
        "kind": "team", "team_id": "236", "networks": "[]", "channels": "[]",
        "template": 0, "settings": "{}"}).json()
    assert r["ok"]
    sub = list(fake_plex.STATE.subscriptions.values())[0]
    assert sub["type"] == 2, "never a single event from the pass panel"


def test_saving_settings_does_not_resume_a_paused_pass(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    rid = db.one("SELECT id FROM passes")["id"]
    client.post(f"/passes/{rid}/toggle", follow_redirects=False)
    assert db.one("SELECT enabled FROM passes")["enabled"] == 0
    client.post(f"/api/rules/{rid}", data={"settings": '{"endOffsetMinutes":"45"}'})
    assert db.one("SELECT enabled FROM passes")["enabled"] == 0, "not sent means unchanged"
    client.post(f"/api/rules/{rid}", data={"enabled": "1"})
    assert db.one("SELECT enabled FROM passes")["enabled"] == 1


def test_a_search_with_awkward_characters_redirects_intact(client):
    r = client.get("/search", params={"q": "Tom & Jerry #2"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/?q=Tom%20%26%20Jerry%20%232"


def test_a_bad_team_id_is_a_clean_answer_not_a_crash(client, synced):
    r = client.get("/api/rules/options", params={"kind": "team", "team_id": "abc"})
    assert r.status_code == 200


def test_the_plex_token_never_appears_in_a_url(client, synced):
    """A URL ends up in logs and error pages. The token travels as a header."""
    fake_plex.STATE.seen_urls = []
    aid = db.one("SELECT id FROM airings WHERE program_guid = ?",
                 (fake_plex.GAME_GUID,))["id"]
    assert client.get("/api/record/options", params={"airing_id": aid}).json()["ok"]
    assert fake_plex.STATE.seen_urls
    assert all("X-Plex-Token" not in u for u in fake_plex.STATE.seen_urls)


def test_old_pass_history_is_pruned(client, synced):
    from app import sync as s
    old = s._now() - 120 * 86400
    with db.tx() as c:
        c.execute("INSERT INTO our_grabs (channel_vcn, begins_at) VALUES ('1.1', ?)", (old,))
        c.execute("INSERT INTO pass_actions (pass_id, program_guid, begins_at, action, "
                  "dry_run) VALUES (1, 'x', ?, 'scheduled', 0)", (old,))
    s.prune_history()
    assert not db.query("SELECT 1 FROM our_grabs WHERE begins_at = ?", (old,))
    assert not db.query("SELECT 1 FROM pass_actions WHERE begins_at = ?", (old,))


def test_a_signed_in_viewer_cannot_change_settings(client, synced):
    """Cloudflare lets the household in; only the first account administers."""
    from app import auth
    db.set_setting("auth_mode", "local")
    admin = auth.create_user("owner", "a-good-password")
    viewer = auth.create_user("kid", "a-good-password", role="user")
    for uid, code in ((viewer, 403), (admin, 200)):
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(uid))
        assert client.get("/settings").status_code == 200, "reading is fine"
        assert client.get("/api/schedule").status_code == 200
        r = client.post("/api/backingstore/config", data={"backend": ""})
        assert r.status_code == code, (uid, r.status_code)
        assert client.get("/api/export").status_code == code
