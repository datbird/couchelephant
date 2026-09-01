#!/usr/bin/env python3
"""Remake every screenshot in the README.

    scripts/test.sh --shots

Runs the app against the invented guide in `tests/demo_guide.py`, drives it in
a browser, and writes `docs/images/*.png`. Nothing here touches a real Plex
server, so the pictures can be remade whenever the interface changes and they
never show one person's television.

The alternative was photographing a live install, which is what produced the
first set. Those went stale within a day, and nobody could remake them.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "images")

# Everything the app writes goes to scratch, checked by the same guard the
# test suite uses. A screenshot run must not be able to reach real data.
SCRATCH = tempfile.mkdtemp(prefix="ce-test-shots-")
os.environ["COUCHELEPHANT_DB"] = os.path.join(SCRATCH, "couchelephant.db")
os.environ["COUCHELEPHANT_AUTH_DB"] = os.path.join(SCRATCH, "auth.db")
os.environ["COUCHELEPHANT_LOGOS"] = os.path.join(SCRATCH, "logos")
os.environ["COUCHELEPHANT_NO_SYNC_LOOP"] = "1"
os.environ["COUCHELEPHANT_DEMO_GUIDE"] = "1"
# Two hours from now, so the grid opens on a full afternoon rather than on
# whatever happens to be left of today.
os.environ.setdefault("COUCHELEPHANT_FAKE_ANCHOR",
                      str((int(time.time()) // 3600) * 3600 + 7200))

sys.path.insert(0, ROOT)
from tests import fake_plex, isolation  # noqa: E402

isolation.assert_isolated()

from app import db, passes, sync, web  # noqa: E402
from app.routes import record as record_routes


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_app(plex_url):
    import uvicorn
    db.init()
    db.set_setting("plex_url", plex_url)
    db.set_setting("plex_token", "demo")
    db.set_setting("dry_run", "0")
    db.set_setting("auth_mode", "none")
    db.set_setting("timezone", "UTC")

    ok, detail = sync.full_sync()
    if not ok:
        raise SystemExit(f"the demo guide would not sync: {detail}")
    print(f"  guide: {detail}")

    # A pass, so the schedule and the pass list have something in them.
    team = db.one("SELECT * FROM teams WHERE name = 'Kansas City Chiefs'")
    record_routes._make_pass("team", team=dict(team),
                   prefs={"startOffsetMinutes": "1", "endOffsetMinutes": "30"})
    record_routes._make_pass("smart", name="Sunday football",
                   smart={"op": "all", "nodes": [
                       {"field": "genre", "cmp": "is", "value": "Football"},
                       {"field": "hd", "cmp": "yes"}]},
                   prefs={"endOffsetMinutes": "30"})
    passes.run_passes()
    plex = __import__("app.plex", fromlist=["Plex"]).Plex(plex_url, "demo")
    sync.sync_recordings(plex)
    plex.close()
    _seed_expectations()

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(
        web.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise SystemExit("the app never came up")
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", server


def _seed_expectations():
    """Two announced games the demo guide has not reached yet.

    Both halves of the feature, because they are drawn in different places on
    purpose. A dated one goes in its calendar cell; a month-only one goes in the
    band under the grid, since a cell IS a day and putting a month in one states
    a broadcast date nobody published.

    Fixed days rather than offsets from today, so the docs image is the same
    picture every time it is remade. Both sit ~3 weeks out, which is past where
    any guide reaches, which is exactly why an expectation exists at all.
    """
    import datetime

    now = datetime.datetime.now(datetime.UTC)

    def day_of_this_month(day, hour):
        return int(datetime.datetime(now.year, now.month, day, hour, 0,
                                     tzinfo=datetime.UTC).timestamp())

    pid = db.one("SELECT id FROM passes WHERE kind = 'team'")["id"]
    # Invented call signs, same rule as the rest of the demo guide: Q is the
    # second letter, so nothing here is a real station.
    rows = [
        ("demo-exp-1", "Chiefs at Chargers", "KQAADT",
         day_of_this_month(22, 20), "day"),
        ("demo-exp-2", "Chiefs at Bills", "WQBBDT",
         day_of_this_month(26, 12), "month"),
    ]
    with db.tx() as c:
        for sid, title, network, when, precision in rows:
            c.execute(
                "INSERT INTO expectations (pass_id, source, source_id, title, "
                "network, expected_at, precision, updated_at) "
                "VALUES (?, 'demo', ?, ?, ?, ?, ?, ?)",
                (pid, sid, title, network, when, precision, int(time.time())))


def shot(page, name):
    path = os.path.join(OUT, f"{name}.png")
    page.screenshot(path=path)
    print(f"  {name}.png")


def main():
    from playwright.sync_api import sync_playwright

    plex_url, stop_plex = fake_plex.start()
    os.environ["COUCHELEPHANT_TEST_PLEX"] = plex_url
    isolation.assert_isolated()
    base, server = start_app(plex_url)
    os.makedirs(OUT, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        desk = browser.new_context(base_url=base, viewport={"width": 1440, "height": 900},
                                   timezone_id="UTC", color_scheme="dark")
        page = desk.new_page()

        # ---- the guide ----
        page.goto("/")
        page.wait_for_selector(".gprog", timeout=20000)
        page.wait_for_timeout(700)
        shot(page, "guide-dark")

        light = browser.new_context(base_url=base, viewport={"width": 1440, "height": 900},
                                    timezone_id="UTC", color_scheme="light")
        lp = light.new_page()
        lp.goto("/")
        lp.wait_for_selector(".gprog", timeout=20000)
        lp.wait_for_timeout(700)
        shot(lp, "guide-light")
        light.close()

        # ---- a programme, and the record options ----
        # A game, because it has a live airing and a repeat, which is what the
        # panel is for.
        page.click('.gprog.live[title*="Chiefs at Buccaneers"]')
        page.wait_for_selector("#ovlbox .air", timeout=20000)
        shot(page, "program-panel")
        page.click("#ovlx")

        # A programme nothing has booked yet, or the button says Cancel.
        page.click('.gprog[title*="The Long Way Home"]')
        page.wait_for_selector("#ovlbox .air [data-rec]", timeout=20000)
        page.click("#ovlbox .air [data-rec]")
        page.wait_for_selector("#optgo", timeout=20000)
        page.wait_for_timeout(400)
        shot(page, "record-options")
        page.click("#ovlx")

        # ---- the schedule ----
        page.goto("/recordings")
        page.wait_for_selector(".agrow", timeout=20000)
        shot(page, "schedule-agenda")
        page.click('[data-view="calendar"]')
        page.wait_for_selector("#calgrid .calday", timeout=20000)
        # The one shot that needs a taller frame. It has to show three things at
        # once: the legend that names the third colour, the grid cell holding a
        # dated expectation, and the band under the grid holding a month-only
        # one. At 900 the grid was cut off around the third week, which is above
        # every date an expectation can plausibly carry, so the shot cropped out
        # its own subject. Scrolling alone cannot fix it either, because legend
        # to band is taller than 900 and something always falls off an end.
        #
        # The sticky header offset is read from the DOM rather than guessed, so
        # changing the header cannot silently hide the legend behind it again.
        page.set_viewport_size({"width": 1440, "height": 1120})
        page.wait_for_timeout(200)
        page.evaluate("""() => {
            const nav = document.querySelector('.pt-nav');
            const legend = document.querySelector('.legend');
            const pad = nav ? nav.getBoundingClientRect().bottom + 14 : 130;
            const top = legend.getBoundingClientRect().top + window.scrollY;
            window.scrollTo(0, Math.max(0, top - pad));
        }""")
        page.wait_for_timeout(300)
        shot(page, "schedule-calendar")
        page.set_viewport_size({"width": 1440, "height": 900})
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)

        # ---- passes, with one open ----
        page.click('.subtab[data-sub="passes"]')
        page.wait_for_selector(".passrow", timeout=20000)
        page.click(".passrow .pmeta")
        page.wait_for_selector(".passdetail table", timeout=20000)
        shot(page, "passes")

        # ---- adding one ----
        page.click("#addrule")
        page.wait_for_selector("#rlist > *", timeout=20000)
        page.wait_for_timeout(400)
        shot(page, "add-schedule")

        # ---- the smart filter ----
        page.check('input[name=rsub][value="filter"]')
        page.wait_for_selector("#sfroot .sfrow", timeout=20000)
        page.select_option("#sfroot .sffield", "genre")
        page.wait_for_selector("select.sfval", timeout=20000)
        page.select_option("select.sfval", "Football")
        page.wait_for_function(
            "() => /programme/.test(document.getElementById('sfcount').textContent)",
            timeout=25000)
        page.wait_for_timeout(600)
        shot(page, "smart-filter")

        page.wait_for_selector('[data-set="comskipMethod"]', timeout=25000)
        page.locator('label.optrow:has([data-set="comskipMethod"]) .opthelp').hover()
        page.wait_for_selector(".tipbox.on", timeout=10000)
        page.wait_for_timeout(300)
        shot(page, "record-tooltip")
        page.keyboard.press("Escape")

        # ---- settings ----
        page.goto("/settings")
        page.wait_for_selector("#setnav .nav-item", timeout=20000)
        # The tab underline sparkles for 700ms after a change of tab. A shot
        # inside that window shows dots over "Guide".
        page.wait_for_timeout(900)
        shot(page, "settings-plex")
        page.click('.nav-item[data-sec="accounts"]')
        page.wait_for_timeout(300)
        shot(page, "settings-accounts")
        page.click('.nav-item[data-sec="channels"]')
        page.click('#settabs .tab[data-tab="art"]')
        page.wait_for_selector("#chlist .chrow", timeout=20000)
        shot(page, "settings-artwork")
        page.click('.nav-item[data-sec="data"]')
        page.wait_for_timeout(400)
        shot(page, "settings-data")
        page.click('#settabs .tab[data-tab="database"]')
        page.wait_for_selector("#bsbackend option[value='sqlite']", state="attached",
                               timeout=20000)
        page.wait_for_timeout(400)
        shot(page, "settings-backingstore")

        # ---- first run ----
        db.set_setting("plex_url", "")
        db.set_setting("plex_token", "")
        page.goto("/")
        page.wait_for_selector("#wform", timeout=20000)
        shot(page, "first-run")
        db.set_setting("plex_url", plex_url)
        db.set_setting("plex_token", "demo")

        # ---- on a phone ----
        phone = browser.new_context(base_url=base, viewport={"width": 390, "height": 844},
                                    is_mobile=True, has_touch=True,
                                    device_scale_factor=2, timezone_id="UTC",
                                    color_scheme="dark")
        pp = phone.new_page()
        pp.goto("/")
        pp.wait_for_selector(".gprog", timeout=20000)
        pp.wait_for_timeout(700)
        shot(pp, "mobile-guide")
        pp.goto("/recordings")
        pp.wait_for_selector(".agrow", timeout=20000)
        shot(pp, "mobile-recordings")
        phone.close()

        desk.close()
        browser.close()

    server.should_exit = True
    stop_plex()
    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("done")


if __name__ == "__main__":
    main()
