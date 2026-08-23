"""The Backup and restore settings section."""
import io
import os
import zipfile

import pytest


@pytest.fixture
def data(page):
    return _open(page)


def _open(page, tab=None):
    """Open Settings, Backup and restore. Reloading first, because these tests
    make jobs and settings from Python after the page has already drawn."""
    page.goto("/settings")
    page.wait_for_selector('#setnav .nav-item[data-sec="data"]')
    page.click('.nav-item[data-sec="data"]')
    if tab:
        page.click(f'#settabs .tab[data-tab="{tab}"]')
    return page


def _options(page, sel):
    """An <option> is never "visible", so wait on the list rather than the DOM."""
    page.wait_for_function(
        "s => document.querySelectorAll(s).length > 1", arg=sel, timeout=15000)
    return page.eval_on_selector_all(sel, "els => els.map(e => e.value)")


def test_the_section_has_the_three_ways_out(data):
    tabs = data.eval_on_selector_all(
        "#settabs .tab", "els => els.map(e => e.textContent.trim())")
    assert tabs == ["Export & import", "Snapshots", "Database"]


def test_the_export_panel_says_what_is_left_out(data):
    text = data.locator('section[data-tab="export"]').inner_text()
    assert "guide is not in it" in text
    assert "Include the Plex token" in text
    assert not data.locator("#expsecrets").is_checked(), "the token is opt in"


def test_downloading_an_export_gives_a_readable_zip(data, client):
    with data.expect_download() as got:
        data.click("#expgo")
    path = got.value.path()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
    assert "couchelephant.json" in names


def test_a_file_that_is_not_an_export_is_named_as_such(data, tmp_path):
    bad = tmp_path / "notanexport.zip"
    bad.write_bytes(b"this is not a zip")
    data.set_input_files("#impfile", str(bad))
    data.wait_for_selector("#impverdict:not([hidden])", timeout=15000)
    assert "not a zip" in data.locator("#impverdict").inner_text()
    assert data.locator("#impgo").is_disabled()


def test_an_export_is_described_before_anything_is_written(data, tmp_path, client):
    from app import portable
    client.post("/api/pass", data={"team_id": "236"})
    f = tmp_path / "export.zip"
    f.write_bytes(portable.export_bytes(version="0.90"))

    data.set_input_files("#impfile", str(f))
    data.wait_for_selector("#impverdict.ok", timeout=15000)
    assert "CouchElephant export" in data.locator("#impverdict").inner_text()
    assert "passes" in data.locator("#impwhat").inner_text()
    assert not data.locator("#impgo").is_disabled()


def test_importing_puts_the_data_back(data, tmp_path, client):
    from app import db, portable
    client.post("/api/pass", data={"team_id": "236"})
    f = tmp_path / "export.zip"
    f.write_bytes(portable.export_bytes())
    with db.tx() as c:
        c.execute("DELETE FROM passes")

    data.set_input_files("#impfile", str(f))
    data.wait_for_selector("#impverdict.ok", timeout=15000)
    data.click("#impgo")
    data.wait_for_function(
        "() => /imported/.test(document.getElementById('impmsg').textContent)",
        timeout=20000)
    assert db.one("SELECT team_name FROM passes")["team_name"] == "Kansas City Chiefs"


# ---- snapshots ----

def test_a_backup_job_can_be_added_and_saved(data, tmp_path):
    from app import backups
    data.click('#settabs .tab[data-tab="snapshot"]')
    data.click("#bkadd")
    data.fill('.bkjob [data-f="name"]', "Nightly")
    data.fill('.bkjob [data-f="dest_path"]', str(tmp_path))
    data.click('.bkjob [data-act="save"]')
    data.wait_for_selector('.bkjob [data-act="run"]', timeout=15000)
    jobs = backups.jobs()
    assert len(jobs) == 1 and jobs[0]["name"] == "Nightly"


def test_a_passphrase_field_never_shows_the_passphrase(data, tmp_path):
    from app import backups
    backups.save_job(None, name="Nightly", dest_path=str(tmp_path),
                     passphrase="open sesame", enabled=True)
    _open(data, "snapshot")
    data.wait_for_selector(".bkjob", timeout=15000)
    assert data.input_value('.bkjob [data-f="passphrase"]') == ""
    assert "set, leave blank" in data.get_attribute('.bkjob [data-f="passphrase"]',
                                                    "placeholder")
    assert "open sesame" not in data.content()


def test_backing_up_now_writes_a_file_and_says_so(data, tmp_path, synced):
    from app import backups
    backups.save_job(None, name="Nightly", dest_path=str(tmp_path), enabled=True,
                     retention=3, every_hours=0)
    _open(data, "snapshot")
    data.wait_for_selector('.bkjob [data-act="run"]', timeout=15000)
    data.click('.bkjob [data-act="run"]')
    data.wait_for_function(
        "() => /Wrote /.test(document.querySelector('.bksay').textContent)",
        timeout=30000)
    assert [f for f in os.listdir(tmp_path) if f.endswith(".zip")]


def test_the_archive_list_reads_a_folder(data, tmp_path, synced):
    from app import backups
    jid = backups.save_job(None, name="Nightly", dest_path=str(tmp_path),
                           enabled=True, every_hours=0)
    backups.run_job(jid)
    _open(data, "snapshot")
    data.fill("#bkdest", str(tmp_path))
    data.click("#bklist")
    data.wait_for_selector("#bkarch .rrow", timeout=15000)
    assert "couchelephant-nightly-" in data.locator("#bkarch").inner_text()


# ---- the backing store ----

def test_the_database_panel_offers_every_backend(data):
    data.click('#settabs .tab[data-tab="database"]')
    assert _options(data, "#bsbackend option") == ["", "sqlite", "postgres", "mysql"]


def test_choosing_a_backend_shows_the_fields_it_needs(data):
    data.click('#settabs .tab[data-tab="database"]')
    _options(data, "#bsbackend option")
    data.select_option("#bsbackend", "sqlite")
    keys = data.eval_on_selector_all("#bsfields [data-k]", "els => els.map(e => e.dataset.k)")
    assert keys == ["sqlite_path"]
    data.select_option("#bsbackend", "postgres")
    keys = data.eval_on_selector_all("#bsfields [data-k]", "els => els.map(e => e.dataset.k)")
    assert "pg_host" in keys and "pg_password" in keys


def test_a_password_field_is_masked(data):
    data.click('#settabs .tab[data-tab="database"]')
    _options(data, "#bsbackend option")
    data.select_option("#bsbackend", "postgres")
    assert data.get_attribute("#bs_pg_password", "type") == "password"


def test_testing_a_good_store_gets_a_tick(data, tmp_path):
    data.click('#settabs .tab[data-tab="database"]')
    _options(data, "#bsbackend option")
    data.select_option("#bsbackend", "sqlite")
    data.fill("#bs_sqlite_path", str(tmp_path / "backing.db"))
    data.click("#bstest")
    data.wait_for_selector("#bsverdict.ok, #bsverdict.bad", timeout=30000)
    assert "ok" in (data.get_attribute("#bsverdict", "class") or "")


def test_testing_a_bad_path_explains_it(data):
    data.click('#settabs .tab[data-tab="database"]')
    _options(data, "#bsbackend option")
    data.select_option("#bsbackend", "sqlite")
    data.fill("#bs_sqlite_path", "/no/such/folder/backing.db")
    data.click("#bstest")
    data.wait_for_selector("#bsverdict.bad", timeout=30000)
    assert "no folder" in data.locator("#bsverdict").inner_text()


def test_a_preview_says_what_would_move_and_moves_nothing(data, tmp_path, client, synced):
    from app import backingstore, db
    client.post("/api/pass", data={"team_id": "236"})
    db.set_setting("backingstore_backend", "sqlite")
    db.set_setting("sqlite_path", str(tmp_path / "backing.db"))
    _open(data, "database")
    data.wait_for_selector("#bsdry", timeout=15000)
    data.click("#bsdry")
    data.wait_for_function(
        "() => /would send/.test(document.getElementById('bsstatus').textContent)",
        timeout=30000)
    assert backingstore.chosen().read_all("passes") == {}


def test_reconciling_sends_the_passes(data, tmp_path, client, synced):
    from app import backingstore, db
    client.post("/api/pass", data={"team_id": "236"})
    db.set_setting("backingstore_backend", "sqlite")
    db.set_setting("sqlite_path", str(tmp_path / "backing.db"))
    _open(data, "database")
    data.wait_for_selector("#bsrun", timeout=15000)
    data.click("#bsrun")
    data.wait_for_function(
        "() => /Sent /.test(document.getElementById('bsstatus').textContent)",
        timeout=30000)
    assert backingstore.chosen().read_all("passes")
