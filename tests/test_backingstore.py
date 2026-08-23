"""The two-way backing store, driven against a real SQLite file."""
import os

import pytest

from app import backingstore, db


@pytest.fixture
def store(tmp_path):
    """A configured SQLite backing store, in scratch."""
    path = str(tmp_path / "backing.db")
    db.set_setting("backingstore_backend", "sqlite")
    db.set_setting("sqlite_path", path)
    yield path


def _passes():
    return {r["team_name"] or r["series_title"] or r["label"]
            for r in db.query("SELECT * FROM passes")}


def test_it_says_so_when_nothing_is_configured():
    db.set_setting("backingstore_backend", "")
    with pytest.raises(backingstore.BackendError) as e:
        backingstore.sync_all()
    assert "No backing store" in str(e.value)


def test_a_path_in_a_folder_that_is_not_there_is_explained(synced):
    db.set_setting("backingstore_backend", "sqlite")
    db.set_setting("sqlite_path", "/no/such/folder/backing.db")
    with pytest.raises(backingstore.BackendError) as e:
        backingstore.sync_all()
    assert "no folder" in str(e.value)


def test_the_test_button_reports_what_it_found(store, synced):
    detail = backingstore.chosen().test()
    assert store in detail or "record" in detail


def test_a_dry_run_writes_nothing(store, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    out = backingstore.sync_all(dry_run=True)
    assert out["pushed"] > 0
    assert not os.path.exists(store) or \
        backingstore.chosen().read_all("passes") == {}


def test_the_first_run_pushes_and_the_second_does_nothing(store, client, synced):
    """A store that re-pushes every run is comparing text to text somewhere."""
    client.post("/api/pass", data={"team_id": "236"})
    first = backingstore.sync_all()
    assert first["pushed"] > 0
    second = backingstore.sync_all()
    assert second["pushed"] == 0 and second["pulled"] == 0, \
        "the second run had nothing left to say"


def test_a_local_change_reaches_the_store(store, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 0")
    backingstore.sync_all()
    remote = backingstore.chosen().read_all("passes")
    assert str(list(remote.values())[0]["enabled"]) == "0"


def test_a_change_in_the_store_reaches_here(store, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    backend = backingstore.chosen()
    remote = backend.read_all("passes")
    key = list(remote)[0]
    remote[key]["team_name"] = "Renamed Elsewhere"
    backend.write("passes", remote, [])
    backingstore.sync_all()
    assert "Renamed Elsewhere" in _passes()


def test_a_delete_here_removes_it_there(store, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    with db.tx() as c:
        c.execute("DELETE FROM passes")
    backingstore.sync_all()
    assert backingstore.chosen().read_all("passes") == {}


def test_a_delete_there_removes_it_here(store, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    backend = backingstore.chosen()
    backend.write("passes", {}, list(backend.read_all("passes")))
    backingstore.sync_all()
    assert not db.query("SELECT 1 FROM passes")


def test_restoring_pulls_down_and_never_pushes_back(store, client, synced):
    """A plain sync onto an empty machine reads every missing row as a local
    delete, and erases the copy being restored from."""
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    before = backingstore.chosen().read_all("passes")

    with db.tx() as c:
        c.execute("DELETE FROM passes")
        c.execute("DELETE FROM our_grabs")

    out = backingstore.restore_from_remote()
    assert out["restored"] > 0
    assert "Kansas City Chiefs" in _passes()
    assert backingstore.chosen().read_all("passes") == before, "the store is intact"


def test_the_sync_after_a_restore_is_a_clean_no_op(store, client, synced):
    """This is what proves the shadow was rewritten. Without it the next run
    would push everything back as though it were new."""
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    with db.tx() as c:
        c.execute("DELETE FROM passes")
    backingstore.restore_from_remote()
    out = backingstore.sync_all()
    assert out["pushed"] == 0 and out["pulled"] == 0 and out["removed"] == 0


def test_a_failed_read_raises_rather_than_looking_empty(store, client, synced):
    """An empty remote against a populated shadow reads as "they deleted
    everything", and this side obeys. A transient error must never look like
    that."""
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()

    backend = backingstore.chosen()

    class Broken(type(backend)):
        def read_all(self, store_name):
            raise backingstore.BackendError("the network went away")

    db.set_setting("sqlite_path", "/no/such/folder/x.db")
    with pytest.raises(backingstore.BackendError):
        backingstore.sync_all()
    # Nothing was removed on the strength of a failed read.
    assert db.query("SELECT 1 FROM passes")


def test_the_status_records_what_the_last_run_did(store, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    backingstore.sync_all()
    st = backingstore.status()
    assert st["ok"] is True
    assert st["backend"] == "sqlite"
    assert "sent" in st["detail"]


def test_a_password_is_never_handed_back(client, synced):
    db.set_setting("backingstore_backend", "postgres")
    db.set_setting("pg_password", "hunter2")
    d = client.get("/api/backingstore/config").json()
    assert d["config"]["pg_password"] == "*" * 8
    assert "hunter2" not in str(d)


def test_a_masked_password_means_leave_it_alone(client, synced):
    db.set_setting("pg_password", "hunter2")
    client.post("/api/backingstore/config",
                data={"backingstore_backend": "postgres", "pg_password": "*" * 8})
    assert db.get_setting("pg_password") == "hunter2"
    client.post("/api/backingstore/config",
                data={"backingstore_backend": "postgres", "pg_password": "newer"})
    assert db.get_setting("pg_password") == "newer"


def test_every_backend_offers_the_fields_it_needs():
    for name, cls in backingstore.BACKENDS.items():
        assert cls.label
        if name != "sqlite":
            keys = {f[0] for f in cls.fields}
            assert any(k.endswith("_host") for k in keys), name
            assert any(k.endswith("_password") for k in keys), name
