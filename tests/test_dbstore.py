"""Durable stores, the three-way merge, and export/import."""
import io
import json
import os
import zipfile

import pytest

from app import auth, backingstore, backups, db, dbstore, passes, portable, web
from tests import fake_plex


def _fp(**kw):
    return dbstore.fingerprint(kw)


# ---- the merge, on its own ----

def test_nothing_changed_is_nothing_to_do():
    rec = {"k": "a", "v": "1"}
    local = remote = {"a": rec}
    shadow = {"a": dbstore.fingerprint(rec)}
    to_l, to_r, dl, dr, con = dbstore.merge(local, remote, shadow)
    assert (to_l, to_r, dl, dr, con) == ({}, {}, [], [], [])


def test_a_local_edit_goes_out():
    was = {"k": "a", "v": "1"}
    now = {"k": "a", "v": "2"}
    to_l, to_r, dl, dr, _ = dbstore.merge(
        {"a": now}, {"a": was}, {"a": dbstore.fingerprint(was)})
    assert to_r == {"a": now} and to_l == {} and not dl and not dr


def test_a_remote_edit_comes_in():
    was = {"k": "a", "v": "1"}
    now = {"k": "a", "v": "2"}
    to_l, to_r, dl, dr, _ = dbstore.merge(
        {"a": was}, {"a": now}, {"a": dbstore.fingerprint(was)})
    assert to_l == {"a": now} and to_r == {}


def test_a_new_local_row_goes_out_and_a_new_remote_row_comes_in():
    rec = {"k": "a", "v": "1"}
    _, to_r, _, _, _ = dbstore.merge({"a": rec}, {}, {})
    assert to_r == {"a": rec}
    to_l, _, _, _, _ = dbstore.merge({}, {"a": rec}, {})
    assert to_l == {"a": rec}


def test_a_delete_travels_rather_than_looking_like_a_missing_row():
    """Without the shadow there is no way to tell "deleted here" from
    "never arrived from there", and a delete would resurrect every time."""
    rec = {"k": "a", "v": "1"}
    shadow = {"a": dbstore.fingerprint(rec)}
    _, _, dl, dr, _ = dbstore.merge({}, {"a": rec}, shadow)
    assert dr == ["a"] and not dl
    _, _, dl, dr, _ = dbstore.merge({"a": rec}, {}, shadow)
    assert dl == ["a"] and not dr


def test_an_edit_beats_a_delete():
    """Losing an edit loses work. Losing a delete costs one more click."""
    was = {"k": "a", "v": "1"}
    edited = {"k": "a", "v": "2"}
    shadow = {"a": dbstore.fingerprint(was)}
    to_l, to_r, dl, dr, conflicts = dbstore.merge({}, {"a": edited}, shadow)
    assert to_l == {"a": edited}
    assert not dl and not dr
    assert conflicts == ["a"]


def test_a_two_sided_edit_is_settled_by_the_later_timestamp():
    was = {"k": "a", "v": "1", "created_at": "10"}
    mine = {"k": "a", "v": "mine", "created_at": "20"}
    theirs = {"k": "a", "v": "theirs", "created_at": "30"}
    shadow = {"a": dbstore.fingerprint(was)}
    to_l, to_r, _, _, conflicts = dbstore.merge({"a": mine}, {"a": theirs}, shadow)
    assert to_l == {"a": theirs} and not to_r
    assert conflicts == ["a"]

    theirs["created_at"] = "15"
    to_l, to_r, _, _, _ = dbstore.merge({"a": mine}, {"a": theirs}, shadow)
    assert to_r == {"a": mine} and not to_l


def test_a_reread_is_not_a_change():
    """The fingerprint canonicalises, so a text round-trip does not look like
    an edit and re-push the same rows forever."""
    assert dbstore.fingerprint({"a": 1, "b": None}) == \
           dbstore.fingerprint({"b": "", "a": "1"})


# ---- reading and writing real stores ----

def test_the_guide_is_not_a_durable_store():
    """It rebuilds from Plex in seconds. Copying it would be large and stale."""
    tables = {s["table"] for s in dbstore.STORES.values()}
    for cache in dbstore.CACHE_TABLES:
        if cache == "channels":
            continue        # only its artwork columns travel
        assert cache not in tables


def test_every_store_declares_its_own_primary_key():
    """A store keyed on anything but its real PK upserts onto the wrong row."""
    for name, spec in dbstore.STORES.items():
        assert spec.get("pk"), name
        con = dbstore._con(spec["db"])
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({spec['table']})")}
        for col in spec["pk"]:
            assert col in cols, f"{name}.{col}"


def test_a_pass_is_named_by_something_two_machines_agree_on(client, synced):
    """`id` is an autoincrement and means a different pass elsewhere."""
    client.post("/api/pass", data={"team_id": "236"})
    rows = dbstore.read("passes")
    assert rows
    key = list(rows)[0]
    assert len(key) == 32, "a uid, not a row number"
    assert db.one("SELECT uid FROM passes")["uid"] == key


def test_a_grab_remembers_which_pass_by_uid(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    row = db.one("SELECT pass_id, pass_uid FROM our_grabs")
    assert row["pass_uid"]
    assert row["pass_uid"] == db.one("SELECT uid FROM passes WHERE id=?",
                                     (row["pass_id"],))["uid"]


def test_the_plex_token_stays_behind_unless_asked(synced):
    assert "plex_token" not in dbstore.read("settings")
    assert "plex_token" in dbstore.read("settings", include_secrets=True)


def test_channel_artwork_travels_but_the_rest_of_the_channel_does_not(client, synced):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    client.post("/settings/channels/41.1/logo",
                files={"logo": ("x.png", png, "image/png")})
    rows = dbstore.read("channel_art")
    assert list(rows) == ["41.1"]
    assert set(rows["41.1"]) == {"vcn", "custom_logo", "custom_logo_at"}


def test_preferences_travel_by_username_not_by_row_number():
    auth.create_user("someone", "a-long-enough-password")
    uid = auth._con().execute("SELECT id FROM users").fetchone()["id"]
    auth.set_pref(uid, "theme", "light")
    rows = dbstore.read("user_prefs")
    assert rows["someone\x1ftheme"]["username"] == "someone"
    assert "user_id" not in rows["someone\x1ftheme"]


# ---- export and import ----

def test_an_export_carries_the_decisions_and_not_the_guide(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    blob = portable.export_bytes(version="0.90")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        manifest = json.loads(z.read(portable.MANIFEST))
    assert manifest["app"] == "couchelephant"
    assert manifest["counts"]["passes"] == 1
    assert "programs" not in manifest["stores"]
    assert "airings" not in manifest["stores"]


def test_an_export_is_readable_without_this_program(client, synced):
    blob = portable.export_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        assert portable.MANIFEST in z.namelist()
        json.loads(z.read(portable.MANIFEST))     # plain JSON, no custom format


def test_an_export_carries_the_artwork_files_not_only_the_rows(client, synced):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    client.post("/settings/channels/41.1/logo",
                files={"logo": ("x.png", png, "image/png")})
    blob = portable.export_bytes()
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        logos = [n for n in z.namelist() if n.startswith("logos/")]
    assert len(logos) == 1


def test_the_token_is_left_out_by_default_and_included_on_request(synced):
    def keys(blob):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            m = json.loads(z.read(portable.MANIFEST))
        return {r["key"] for r in m["stores"]["settings"]}
    assert "plex_token" not in keys(portable.export_bytes())
    assert "plex_token" in keys(portable.export_bytes(include_secrets=True))


def test_an_import_puts_back_what_was_exported(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    blob = portable.export_bytes()
    before = db.one("SELECT uid, team_name FROM passes")

    with db.tx() as c:
        c.execute("DELETE FROM passes")
        c.execute("DELETE FROM our_grabs")
    assert not db.query("SELECT 1 FROM passes")

    report = portable.import_bytes(blob)
    assert report["ok"]
    after = db.one("SELECT uid, team_name FROM passes")
    assert after["uid"] == before["uid"]
    assert after["team_name"] == before["team_name"]


def test_an_import_relinks_a_grab_to_the_pass_it_belongs_to(client, synced):
    """The file names a pass by uid. This machine's row numbers are its own."""
    client.post("/api/pass", data={"team_id": "236"})
    blob = portable.export_bytes()
    with db.tx() as c:
        c.execute("DELETE FROM our_grabs")
        c.execute("DELETE FROM passes")
        # A different row number, deliberately.
        c.execute("INSERT INTO passes (id, kind, team_id, team_name, uid, enabled, "
                  "created_at) VALUES (77,'team',999,'Placeholder','zz',1,0)")
    portable.import_bytes(blob)
    grab = db.one("SELECT pass_id, pass_uid FROM our_grabs")
    real = db.one("SELECT id FROM passes WHERE uid = ?", (grab["pass_uid"],))
    assert grab["pass_id"] == real["id"], "linked by uid, not by the old number"


def test_merging_leaves_alone_what_the_file_does_not_carry(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    blob = portable.export_bytes()
    web._make_pass("series", series="Quiz Show")
    portable.import_bytes(blob, replace=False)
    assert db.one("SELECT COUNT(*) c FROM passes")["c"] == 2


def test_replacing_makes_the_result_exactly_what_was_exported(client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    blob = portable.export_bytes()
    web._make_pass("series", series="Quiz Show")
    portable.import_bytes(blob, replace=True)
    rows = db.query("SELECT team_name FROM passes")
    assert [r["team_name"] for r in rows] == ["Kansas City Chiefs"]


def test_a_file_that_is_not_an_export_is_refused_in_words():
    for blob, says in ((b"not a zip at all", "not a zip"),
                       (_zip({"random.txt": b"hello"}), "no CouchElephant export")):
        with pytest.raises(portable.ImportError_) as e:
            portable.import_bytes(blob)
        assert says in str(e.value)


def test_an_export_from_a_newer_version_is_refused_rather_than_half_read():
    blob = _zip({portable.MANIFEST: json.dumps(
        {"app": "couchelephant", "format": 99, "stores": {}}).encode()})
    with pytest.raises(portable.ImportError_) as e:
        portable.import_bytes(blob)
    assert "newer CouchElephant" in str(e.value)


def test_an_import_cannot_write_outside_the_logo_folder():
    """A zip can name a path that walks out of where you unpack it."""
    blob = _zip({
        portable.MANIFEST: json.dumps(
            {"app": "couchelephant", "format": 1, "stores": {}}).encode(),
        "logos/../../escaped.png": b"\x89PNG\r\n\x1a\n",
    })
    portable.import_bytes(blob)
    assert not os.path.exists("/tmp/escaped.png")


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
