"""Snapshot backups: jobs, archives, retention, encryption, restore."""
import io
import os
import time
import zipfile

import pytest

from app import backups, db, portable
from app.routes import record as record_routes


@pytest.fixture
def dest(tmp_path):
    return str(tmp_path / "backups")


def _job(dest, **kw):
    kw.setdefault("name", "Nightly")
    kw.setdefault("dest_path", dest)
    kw.setdefault("enabled", True)
    kw.setdefault("retention", 3)
    kw.setdefault("every_hours", 24)
    return backups.save_job(None, **kw)


def test_a_job_can_be_made_changed_and_removed(dest):
    jid = _job(dest)
    jobs = backups.jobs()
    assert len(jobs) == 1 and jobs[0]["name"] == "Nightly"
    backups.save_job(jid, name="Weekly", dest_path=dest, every_hours=168,
                     retention=4, enabled=True)
    assert backups.jobs()[0]["name"] == "Weekly"
    backups.delete_job(jid)
    assert backups.jobs() == []


def test_a_passphrase_is_never_handed_back(dest):
    _job(dest, passphrase="open sesame")
    job = backups.jobs()[0]
    assert "passphrase" not in job
    assert job["encrypted"] is True


def test_a_blank_passphrase_leaves_the_old_one_alone(dest):
    jid = _job(dest, passphrase="open sesame")
    backups.save_job(jid, name="Nightly", dest_path=dest, passphrase="")
    assert backups.jobs()[0]["encrypted"] is True
    backups.save_job(jid, name="Nightly", dest_path=dest, passphrase="off")
    assert backups.jobs()[0]["encrypted"] is False


def test_running_a_job_writes_an_archive(dest, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    out = backups.run_job(_job(dest))
    assert out["ok"]
    assert os.path.isfile(os.path.join(dest, out["file"]))
    job = backups.jobs()[0]
    assert job["last_ok"] == 1 and job["last_file"] == out["file"]


def test_the_archive_holds_both_the_export_and_the_database_files(dest, synced):
    out = backups.run_job(_job(dest, raw_db=True))
    with zipfile.ZipFile(os.path.join(dest, out["file"])) as z:
        names = z.namelist()
    assert portable.MANIFEST in names
    assert "sqlite/couchelephant.db" in names
    assert "sqlite/auth.db" in names


def test_the_database_copy_is_consistent_with_a_connection_open(dest, synced):
    """SQLite's own online backup, not a file copy, so a snapshot taken
    mid-write is a database rather than a torn page."""
    out = backups.run_job(_job(dest, raw_db=True))
    with zipfile.ZipFile(os.path.join(dest, out["file"])) as z:
        blob = z.read("sqlite/couchelephant.db")
    import sqlite3
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(blob)
        path = f.name
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] > 0
    con.close()
    os.unlink(path)


def test_a_job_with_nowhere_to_write_says_so_and_is_recorded(dest):
    jid = _job(dest, dest_path="")
    out = backups.run_job(jid)
    assert out["ok"] is False and "nowhere to write" in out["error"]
    assert backups.jobs()[0]["last_ok"] == 0
    assert "nowhere to write" in backups.jobs()[0]["last_error"]


def test_retention_removes_the_oldest(dest, synced):
    jid = _job(dest, retention=2)
    for _ in range(3):
        backups.run_job(jid)
        time.sleep(1.05)          # the name carries a one-second stamp
    assert len(backups.archives(dest)) == 2


def test_retention_only_touches_its_own_job(dest, synced):
    """Two jobs writing to one folder must not delete each other's work."""
    a = _job(dest, name="Alpha", retention=1)
    b = _job(dest, name="Beta", retention=1)
    backups.run_job(a)
    time.sleep(1.05)
    backups.run_job(b)
    time.sleep(1.05)
    backups.run_job(a)
    names = [x["name"] for x in backups.archives(dest)]
    assert any("beta" in n for n in names), "Beta's archive survived Alpha's prune"
    assert sum(1 for n in names if "alpha" in n) == 1


def test_an_encrypted_archive_needs_its_passphrase(dest, synced):
    pytest.importorskip("pyzipper")
    jid = _job(dest, passphrase="open sesame")
    out = backups.run_job(jid)
    assert out["ok"], out.get("error")
    inner = backups.read_archive(dest, out["file"], "open sesame")
    with zipfile.ZipFile(io.BytesIO(inner)) as z:
        assert portable.MANIFEST in z.namelist()
    with pytest.raises((RuntimeError, ValueError)):
        backups.read_archive(dest, out["file"], "wrong")


def test_restoring_an_archive_puts_the_data_back(dest, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    out = backups.run_job(_job(dest))
    with db.tx() as c:
        c.execute("DELETE FROM passes")
    report = backups.restore(dest, out["file"])
    assert report["ok"]
    assert db.one("SELECT team_name FROM passes")["team_name"] == "Kansas City Chiefs"


def test_a_restore_takes_a_safety_copy_of_what_was_there(dest, client, synced):
    client.post("/api/pass", data={"team_id": "236"})
    out = backups.run_job(_job(dest))
    record_routes._make_pass("series", series="Quiz Show")
    report = backups.restore(dest, out["file"], replace=True)
    assert report["safety_copy"], "the state before the restore was kept"
    safety = os.path.join(dest, "before-restore", report["safety_copy"])
    assert os.path.isfile(safety)
    with zipfile.ZipFile(safety) as z:
        import json
        m = json.loads(z.read(portable.MANIFEST))
    titles = {r.get("series_title") for r in m["stores"]["passes"]}
    assert "Quiz Show" in titles, "the safety copy holds what the restore replaced"


def test_an_archive_name_cannot_walk_out_of_the_folder(dest, synced):
    backups.run_job(_job(dest))
    for bad in ("../secret.zip", "sub/dir.zip", "notazip"):
        with pytest.raises(ValueError):
            backups.read_archive(dest, bad)


def test_the_archive_list_is_newest_first(dest, synced):
    jid = _job(dest, retention=0)
    backups.run_job(jid)
    time.sleep(1.05)
    backups.run_job(jid)
    got = backups.archives(dest)
    assert len(got) == 2
    assert got[0]["at"] >= got[1]["at"]


def test_a_folder_that_is_not_there_lists_nothing_rather_than_failing():
    assert backups.archives("/no/such/folder") == []
